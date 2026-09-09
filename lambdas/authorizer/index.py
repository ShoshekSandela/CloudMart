import json
import os
import secrets

import boto3


ssm = boto3.client("ssm")

TOKEN_PARAMETER_NAME = os.environ["TOKEN_PARAMETER_NAME"]
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@cloudmart.com")
CUSTOMER_EMAIL = os.environ.get("CUSTOMER_EMAIL", "customer@cloudmart.com")
CUSTOMER_ID = os.environ.get("CUSTOMER_ID", "1")


def generate_token():
    return secrets.token_urlsafe(32)


def initialize_tokens():
    """
    Initialize exactly two persistent random tokens.

    The deployment pipeline invokes this Lambda directly with:
    {"action": "initialize_tokens"}

    Normal API requests never generate/rotate tokens.
    """
    try:
        result = ssm.get_parameter(
            Name=TOKEN_PARAMETER_NAME,
            WithDecryption=True,
        )
        raw_value = result["Parameter"]["Value"].strip()
    except ssm.exceptions.ParameterNotFound:
        raw_value = ""

    try:
        config = json.loads(raw_value) if raw_value else {}
    except json.JSONDecodeError:
        config = {}

    if not isinstance(config, dict):
        config = {}

    changed = False

    if not config.get("admin_token"):
        config["admin_token"] = generate_token()
        changed = True

    if not config.get("customer_token"):
        config["customer_token"] = generate_token()
        changed = True

    # GitHub Actions / CloudFormation configuration is the source of truth
    # for the email associated with each persistent token. Tokens themselves
    # are only generated when missing and are never rotated by deployment.
    if config.get("admin_email") != ADMIN_EMAIL:
        config["admin_email"] = ADMIN_EMAIL
        changed = True

    if config.get("customer_email") != CUSTOMER_EMAIL:
        config["customer_email"] = CUSTOMER_EMAIL
        changed = True

    if str(config.get("customer_id", "")) != str(CUSTOMER_ID):
        try:
            configured_customer_id = int(CUSTOMER_ID)
        except (TypeError, ValueError) as exc:
            raise ValueError("Configured CUSTOMER_ID must be an integer") from exc

        if configured_customer_id <= 0:
            raise ValueError("Configured CUSTOMER_ID must be positive")

        config["customer_id"] = configured_customer_id
        changed = True

    if changed or not raw_value:
        ssm.put_parameter(
            Name=TOKEN_PARAMETER_NAME,
            Value=json.dumps(config),
            Type="SecureString",
            Overwrite=True,
        )

    return {
        "message": "ADMIN and CUSTOMER tokens initialized",
        "parameter": TOKEN_PARAMETER_NAME,
        "admin_token_generated": True,
        "customer_token_generated": True,
    }


def get_authentication_config():
    result = ssm.get_parameter(
        Name=TOKEN_PARAMETER_NAME,
        WithDecryption=True,
    )

    value = result["Parameter"]["Value"].strip()

    try:
        config = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("Authorization parameter must contain JSON") from exc

    if not isinstance(config, dict):
        raise ValueError("Authorization configuration must be a JSON object")

    if not config.get("admin_token") or not config.get("customer_token"):
        raise ValueError("Both ADMIN and CUSTOMER tokens must be initialized")

    return config


def find_identity(config, token):
    if secrets.compare_digest(token, str(config["admin_token"])):
        return {
            "role": "ADMIN",
            "email": config.get("admin_email", ADMIN_EMAIL),
            "customer_id": None,
        }

    if secrets.compare_digest(token, str(config["customer_token"])):
        return {
            "role": "CUSTOMER",
            "email": config.get("customer_email", CUSTOMER_EMAIL),
            "customer_id": config.get("customer_id"),
        }

    return None


def build_policy(principal_id, identity, api_arn, stage):
    context = {
        "role": identity["role"],
        "email": str(identity["email"]),
    }

    if identity.get("customer_id") is not None:
        context["customer_id"] = str(identity["customer_id"])

    if identity["role"] == "ADMIN":
        # ADMIN can use every API Gateway method.
        resources = [f"{api_arn}/*/*/*"]
    else:
        # CUSTOMER can:
        # GET  /products
        # GET  /products/{id}
        # POST /orders
        # GET  /orders
        # GET  /orders/{id}
        # PUT  /orders/{id}
        #
        # CUSTOMER cannot:
        # POST /products
        # PUT /products/{id}
        # DELETE /products/{id}
        # POST /orders/{id}  (order lifecycle/status)
        resources = [
            f"{api_arn}/{stage}/GET/products",
            f"{api_arn}/{stage}/GET/products/*",
            f"{api_arn}/{stage}/POST/orders",
            f"{api_arn}/{stage}/GET/orders",
            f"{api_arn}/{stage}/GET/orders/*",
            f"{api_arn}/{stage}/PUT/orders/*",
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
    # Used only by GitHub Actions to initialize the tokens before API use.
    if event.get("action") == "initialize_tokens":
        return initialize_tokens()

    method_arn = event.get("methodArn")
    authorization_header = event.get("authorizationToken")

    if not method_arn or not authorization_header:
        raise Exception("Unauthorized")

    token = authorization_header.strip()

    if token.lower().startswith("bearer "):
        token = token[7:].strip()

    if not token:
        raise Exception("Unauthorized")

    try:
        config = get_authentication_config()
        identity = find_identity(config, token)
    except Exception:
        raise Exception("Unauthorized")

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
        customer_identity = (
            identity.get("customer_id")
            or identity.get("email")
            or "user"
        )
        principal_id = f"cloudmart-customer-{customer_identity}"

    print(json.dumps({
        "message": "Authorization successful",
        "principalId": principal_id,
        "role": identity["role"],
        "email": identity.get("email"),
    }))

    return build_policy(
        principal_id=principal_id,
        identity=identity,
        api_arn=api_arn,
        stage=stage,
    )
