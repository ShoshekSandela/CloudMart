import hashlib
import logging
import json
import os
import secrets

import boto3
import pymysql


logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")

TOKEN_PARAMETER_NAME = os.environ["TOKEN_PARAMETER_NAME"]
DB_HOST_PARAMETER_NAME = os.environ["DB_HOST_PARAMETER_NAME"]
DB_PORT_PARAMETER_NAME = os.environ["DB_PORT_PARAMETER_NAME"]
DB_NAME_PARAMETER_NAME = os.environ["DB_NAME_PARAMETER_NAME"]
DB_USERNAME_PARAMETER_NAME = os.environ["DB_USERNAME_PARAMETER_NAME"]
DB_PASSWORD_PARAMETER_NAME = os.environ["DB_PASSWORD_PARAMETER_NAME"]
DEPLOYMENT_VERSION = os.environ.get("DEPLOYMENT_VERSION", "unknown")

def get_ssm_parameter(name, decrypt=False):
    result = ssm.get_parameter(Name=name, WithDecryption=decrypt)
    return result["Parameter"]["Value"].strip()


def get_db_connection():
    host = get_ssm_parameter(DB_HOST_PARAMETER_NAME)
    port = int(get_ssm_parameter(DB_PORT_PARAMETER_NAME))
    database = get_ssm_parameter(DB_NAME_PARAMETER_NAME)
    username = get_ssm_parameter(DB_USERNAME_PARAMETER_NAME, decrypt=True)
    password = get_ssm_parameter(DB_PASSWORD_PARAMETER_NAME, decrypt=True)

    return pymysql.connect(
        host=host,
        port=port,
        user=username,
        password=password,
        database=database,
        connect_timeout=5,
        read_timeout=5,
        write_timeout=5,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def normalize_token(value):
    """Normalize an Authorization token without changing its value semantics.

    API Gateway supplies the complete Authorization header to a TOKEN
    authorizer. Accept both the conventional `Bearer <token>` form and a
    legacy raw-token form, and remove accidental surrounding whitespace.
    """
    token = str(value or "").strip()

    if token.lower().startswith("bearer "):
        token = token[7:].strip()

    return token


def token_fingerprint(token):
    """Return a non-secret fingerprint for diagnostics.

    The raw token is never written to logs.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def get_authentication_config():
    value = get_ssm_parameter(TOKEN_PARAMETER_NAME, decrypt=True)

    try:
        config = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("Authorization parameter must contain JSON") from exc

    if not isinstance(config, dict):
        raise ValueError("Authorization parameter must contain an object")

    # Current format is {"admin_token": "<token>"}.
    # Accept the legacy {"token": "<token>"} format only for backward
    # compatibility during migration. Do not generate or rotate tokens here.
    admin_token = config.get("admin_token") or config.get("token")

    if not admin_token:
        raise ValueError("ADMIN token is not initialized")

    config["admin_token"] = normalize_token(admin_token)
    return config




def find_customer_identity(connection, token):
    token_hash = hash_token(token)

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                ct.customer_id,
                c.customer_email
            FROM customer_tokens ct
            LEFT JOIN customers c
                ON c.customer_id = ct.customer_id
            WHERE ct.token_hash = %s
              AND ct.status = 'ACTIVE'
            LIMIT 1
            """,
            (token_hash,),
        )
        customer = cursor.fetchone()

    if not customer or not customer.get("customer_email"):
        return None

    return {
        "role": "CUSTOMER",
        "email": str(customer["customer_email"]),
        "customer_id": int(customer["customer_id"]),
    }


def find_identity(config, token, connection):
    configured_admin_token = normalize_token(config["admin_token"])

    if configured_admin_token and secrets.compare_digest(
        token, configured_admin_token
    ):
        return {
            "role": "ADMIN",
            "email": None,
            "customer_id": None,
        }

    return find_customer_identity(connection, token)


def build_policy(principal_id, identity, method_arn):
    """Allow only the exact API Gateway method requested.

    The authorizer never returns a wildcard Resource.  Authorization is based
    on the authenticated role plus the exact HTTP method/path in method_arn.
    """
    context = {
        "role": identity["role"],
    }

    if identity.get("email") is not None:
        context["email"] = str(identity["email"])

    if identity.get("customer_id") is not None:
        context["customer_id"] = str(identity["customer_id"])

    # methodArn format:
    # arn:partition:execute-api:region:account:api-id/stage/HTTP-VERB/path
    parts = method_arn.split("/", 2)
    if len(parts) < 3:
        raise Exception("Invalid methodArn")

    method = parts[2].split("/", 1)[0].upper()
    path = "/" + parts[2].split("/", 1)[1] if "/" in parts[2] else "/"

    def is_product_path():
        return path == "/products" or (
            path.startswith("/products/") and len(path.split("/")) == 3
        )

    def is_order_path():
        pieces = path.split("/")
        return (
            path == "/orders"
            or (len(pieces) == 3 and pieces[1] == "orders" and pieces[2])
            or (len(pieces) == 4 and pieces[1] == "orders" and pieces[2] and pieces[3] == "status")
        )

    def is_customer_by_id_path():
        pieces = path.split("/")
        return len(pieces) == 3 and pieces[1] == "customers" and pieces[2]

    allowed = False

    if identity["role"] == "ADMIN":
        allowed = (
            (is_product_path() and method in {"GET", "POST", "PUT", "DELETE"})
            or (is_order_path() and method in {"GET", "POST", "PUT", "PATCH"})
            or (is_customer_by_id_path() and method in {"GET", "PUT", "DELETE"})
            or (path == "/customers" and method in {"GET", "POST"})
            or (len(path.split("/")) == 4 and path.startswith("/customers/") and path.endswith("/unsubscribe") and method == "POST")
        )
    else:
        allowed = (
            (is_product_path() and method == "GET")
            or (path == "/orders" and method in {"POST", "GET", "PATCH"})
            or (len(path.split("/")) == 3 and path.startswith("/orders/") and method in {"GET", "PUT"})
            or (is_customer_by_id_path() and method == "GET")
            or (len(path.split("/")) == 4 and path.startswith("/customers/") and path.endswith("/unsubscribe") and method == "POST")
        )

    if not allowed:
        raise Exception("Unauthorized")

    return {
        "principalId": principal_id,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": "Allow",
                    "Resource": method_arn,
                }
            ],
        },
        "context": context,
    }


def lambda_handler(event, context):
    method_arn = event.get("methodArn")
    authorization_header = event.get("authorizationToken")

    if not method_arn or not authorization_header:
        raise Exception("Unauthorized")

    token = normalize_token(authorization_header)

    if not token:
        raise Exception("Unauthorized")

    logger.info(
        "Authorization request received: deployment_version=%s token_length=%d token_fingerprint=%s method_arn=%s",
        DEPLOYMENT_VERSION,
        len(token),
        token_fingerprint(token),
        method_arn,
    )

    connection = None
    try:
        try:
            config = get_authentication_config()
        except Exception:
            # Never log the token or SSM value. The exception is logged with
            # the parameter name so IAM/SSM failures are diagnosable.
            logger.exception("Failed to load authentication configuration from SSM parameter %s", TOKEN_PARAMETER_NAME)
            raise

        configured_admin_token = normalize_token(config["admin_token"])

        logger.info(
            "Loaded authentication configuration: parameter=%s configured_token_length=%d configured_token_fingerprint=%s",
            TOKEN_PARAMETER_NAME,
            len(configured_admin_token),
            token_fingerprint(configured_admin_token),
        )

        if secrets.compare_digest(token, configured_admin_token):
            identity = {
                "role": "ADMIN",
                "email": None,
                "customer_id": None,
            }
            logger.info(
                "Admin token validation succeeded: deployment_version=%s token_length=%d token_fingerprint=%s",
                DEPLOYMENT_VERSION,
                len(token),
                token_fingerprint(token),
            )
        else:
            logger.warning(
                "Admin token validation did not match: supplied_length=%d "
                "configured_length=%d supplied_fingerprint=%s "
                "configured_fingerprint=%s",
                len(token),
                len(configured_admin_token),
                token_fingerprint(token),
                token_fingerprint(configured_admin_token),
            )
            connection = get_db_connection()
            identity = find_customer_identity(connection, token)

        if not identity:
            logger.warning(
                "Authorization failed: token was not mapped to an active identity"
            )
            raise Exception("Unauthorized")

        parts = method_arn.split("/")
        if len(parts) < 2:
            raise Exception("Unauthorized")

        if identity["role"] == "ADMIN":
            principal_id = "cloudmart-admin"
        else:
            principal_id = f"cloudmart-customer-{identity['customer_id']}"

        logger.info(
            "Authorization successful: principal=%s role=%s customer_id=%s",
            principal_id,
            identity["role"],
            identity.get("customer_id"),
        )

        return build_policy(
            principal_id=principal_id,
            identity=identity,
            method_arn=method_arn,
        )
    except Exception:
        logger.exception("Authorization failed")
        raise Exception("Unauthorized")
    finally:
        if connection:
            connection.close()
