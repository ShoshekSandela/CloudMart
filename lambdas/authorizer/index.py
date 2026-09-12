import hashlib
import json
import logging
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

CUSTOMER_TOKEN_COUNT = 5


def generate_token():
    return secrets.token_urlsafe(32)


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


def initialize_admin_token():
    try:
        raw_value = get_ssm_parameter(TOKEN_PARAMETER_NAME, decrypt=True)
    except ssm.exceptions.ParameterNotFound:
        raw_value = ""

    try:
        config = json.loads(raw_value) if raw_value else {}
    except json.JSONDecodeError:
        config = {}

    if not isinstance(config, dict):
        config = {}

    if not config.get("admin_token"):
        config["admin_token"] = generate_token()
        ssm.put_parameter(
            Name=TOKEN_PARAMETER_NAME,
            Value=json.dumps({"admin_token": config["admin_token"]}),
            Type="SecureString",
            Overwrite=True,
        )
        return config["admin_token"], True

    # Normalize the parameter so it contains only the Admin token.
    normalized = {"admin_token": str(config["admin_token"])}
    if config != normalized:
        ssm.put_parameter(
            Name=TOKEN_PARAMETER_NAME,
            Value=json.dumps(normalized),
            Type="SecureString",
            Overwrite=True,
        )

    return str(config["admin_token"]), False


def initialize_customer_tokens(connection, force_rotate=False):
    generated_tokens = []

    with connection.cursor() as cursor:
        for customer_id in range(1, CUSTOMER_TOKEN_COUNT + 1):
            cursor.execute(
                """
                SELECT token_id, status
                FROM customer_tokens
                WHERE customer_id = %s
                LIMIT 1
                """,
                (customer_id,),
            )
            existing = cursor.fetchone()

            if existing and str(existing["status"]).upper() == "ACTIVE" and not force_rotate:
                continue

            token = generate_token()
            token_hash = hash_token(token)

            if existing:
                cursor.execute(
                    """
                    UPDATE customer_tokens
                    SET token_hash = %s,
                        status = 'ACTIVE'
                    WHERE token_id = %s
                    """,
                    (token_hash, existing["token_id"]),
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO customer_tokens
                        (customer_id, token_hash, status)
                    VALUES
                        (%s, %s, 'ACTIVE')
                    """,
                    (customer_id, token_hash),
                )

            generated_tokens.append({
                "customer_id": customer_id,
                "token": token,
            })

    connection.commit()
    return generated_tokens


def rotate_customer_tokens():
    """
    Generate a fresh token for each of the five configured customers.

    The raw tokens are returned only in this Lambda response. RDS stores
    only SHA-256 hashes. The Admin token in SSM is not changed.
    """
    connection = None
    try:
        connection = get_db_connection()
        customer_tokens = initialize_customer_tokens(
            connection,
            force_rotate=True,
        )

        if len(customer_tokens) != CUSTOMER_TOKEN_COUNT:
            raise RuntimeError(
                f"Expected {CUSTOMER_TOKEN_COUNT} customer tokens to be rotated, "
                f"but generated {len(customer_tokens)}."
            )

        return {
            "statusCode": 200,
            "message": "Five customer authentication tokens rotated successfully",
            "customer_token_count": CUSTOMER_TOKEN_COUNT,
            "customer_tokens": customer_tokens,
        }
    except Exception:
        if connection:
            connection.rollback()
        logger.exception("Customer token rotation failed")
        raise
    finally:
        if connection:
            connection.close()


def initialize_tokens():
    """
    Initialize one persistent Admin token in SSM and five persistent
    customer tokens in RDS. Raw customer tokens are returned only when
    they are newly generated; RDS stores only SHA-256 hashes.
    """
    connection = None
    try:
        admin_token, admin_generated = initialize_admin_token()
        connection = get_db_connection()
        customer_tokens = initialize_customer_tokens(connection)

        return {
            "statusCode": 200,
            "message": "CloudMart authentication tokens initialized",
            "admin_token_generated": admin_generated,
            "customer_token_count": CUSTOMER_TOKEN_COUNT,
            "new_customer_tokens": customer_tokens,
            "admin_token": admin_token if admin_generated else None,
        }
    except Exception:
        if connection:
            connection.rollback()
        logger.exception("Authentication token initialization failed")
        raise
    finally:
        if connection:
            connection.close()


def get_authentication_config():
    value = get_ssm_parameter(TOKEN_PARAMETER_NAME, decrypt=True)

    try:
        config = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("Authorization parameter must contain JSON") from exc

    if not isinstance(config, dict) or not config.get("admin_token"):
        raise ValueError("ADMIN token is not initialized")

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
    if secrets.compare_digest(token, str(config["admin_token"])):
        return {
            "role": "ADMIN",
            "email": None,
            "customer_id": None,
        }

    return find_customer_identity(connection, token)


def build_policy(principal_id, identity, api_arn, stage):
    context = {
        "role": identity["role"],
    }

    if identity.get("email") is not None:
        context["email"] = str(identity["email"])

    if identity.get("customer_id") is not None:
        context["customer_id"] = str(identity["customer_id"])

    if identity["role"] == "ADMIN":
        resources = [f"{api_arn}/*/*/*"]
    else:
        resources = [
            f"{api_arn}/{stage}/GET/products",
            f"{api_arn}/{stage}/GET/products/*",
            f"{api_arn}/{stage}/POST/orders",
            f"{api_arn}/{stage}/GET/orders",
            f"{api_arn}/{stage}/GET/orders/*",
            f"{api_arn}/{stage}/PUT/orders/*",
            f"{api_arn}/{stage}/PATCH/orders",
            f"{api_arn}/{stage}/PATCH/orders/*/status",
            f"{api_arn}/{stage}/GET/customers/*",
        ]

    return {
        "principalId": principal_id,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": "Allow",
                    "Resource": resources,
                }
            ],
        },
        "context": context,
    }


def lambda_handler(event, context):
    action = event.get("action")

    if action == "initialize_tokens":
        return initialize_tokens()

    if action == "rotate_customer_tokens":
        return rotate_customer_tokens()

    method_arn = event.get("methodArn")
    authorization_header = event.get("authorizationToken")

    if not method_arn or not authorization_header:
        raise Exception("Unauthorized")

    token = authorization_header.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()

    if not token:
        raise Exception("Unauthorized")

    connection = None
    try:
        config = get_authentication_config()

        if secrets.compare_digest(token, str(config["admin_token"])):
            identity = {
                "role": "ADMIN",
                "email": None,
                "customer_id": None,
            }
        else:
            connection = get_db_connection()
            identity = find_customer_identity(connection, token)

        if not identity:
            raise Exception("Unauthorized")

        parts = method_arn.split("/")
        if len(parts) < 2:
            raise Exception("Unauthorized")

        api_arn = parts[0]
        stage = parts[1]

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
            api_arn=api_arn,
            stage=stage,
        )
    except Exception:
        logger.exception("Authorization failed")
        raise Exception("Unauthorized")
    finally:
        if connection:
            connection.close()
