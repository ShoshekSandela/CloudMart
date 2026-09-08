import json
import os
import secrets

import boto3


ssm = boto3.client("ssm")

TOKEN_PARAMETER_NAME = os.environ["TOKEN_PARAMETER_NAME"]
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@cloudmart.com")
CUSTOMER_EMAIL = os.environ.get("CUSTOMER_EMAIL", "customer@cloudmart.com")


def log_json(message, level="info", **fields):
    record = {"message": message, "level": level, **fields}
    print(json.dumps(record))


def _new_token():
    return secrets.token_urlsafe(32)


def get_authentication_config():
    """Read the SSM auth config and create random tokens if they do not exist."""
    try:
        response = ssm.get_parameter(
            Name=TOKEN_PARAMETER_NAME,
            WithDecryption=True,
        )
        value = response["Parameter"]["Value"].strip()
    except ssm.exceptions.ParameterNotFound:
        value = ""

    try:
        config = json.loads(value) if value else {}
    except json.JSONDecodeError:
        config = {}

    if not isinstance(config, dict):
        config = {}

    # Generate once and persist. Tokens are never regenerated when valid
    # tokens already exist, so existing Postman tokens remain usable.
    changed = False
    if not config.get("admin_token"):
        config["admin_token"] = _new_token()
        changed = True
    if not config.get("customer_token"):
        config["customer_token"] = _new_token()
        changed = True

    config.setdefault("admin_email", ADMIN_EMAIL)
    config.setdefault("customer_email", CUSTOMER_EMAIL)

    if changed or not value:
        ssm.put_parameter(
            Name=TOKEN_PARAMETER_NAME,
            Value=json.dumps(config),
            Type="SecureString",
            Overwrite=True,
        )
        log_json(
            "Authorization tokens generated and stored",
            parameter=TOKEN_PARAMETER_NAME,
        )

    return config


def find_identity(config, token):
    if secrets.compare_digest(token, str(config.get("admin_token", ""))):
        return {
            "role": "ADMIN",
            "email": config.get("admin_email", ADMIN_EMAIL),
            "customer_id": None,
        }

    if secrets.compare_digest(token, str(config.get("customer_token", ""))):
        return {
            "role": "CUSTOMER",
            "email": config.get("customer_email", CUSTOMER_EMAIL),
            "customer_id": config.get(
                "customer_customer_id",
                config.get("customer_id"),
            ),
        }

    return None


def build_policy(principal_id, identity, api_arn, stage):
    context = {"role": identity["role"]}

    if identity.get("email"):
        context["email"] = str(identity["email"])

    if identity.get("customer_id") is not None:
        context["customer_id"] = str(identity["customer_id"])

    if identity["role"] == "ADMIN":
        resources = [f"{api_arn}/*/*/*"]
    else:
        # CUSTOMER permissions:
        # GET    /products
        # GET    /products/{id}
        # POST   /orders
        # GET    /orders
        # GET    /orders/{id}
        # PUT    /orders/{id}
        # CUSTOMER cannot create/update/delete products or change order status.
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
            "Statement": [{
                "Action": "execute-api:Invoke",
                "Effect": "Allow",
                "Resource": resources,
            }],
        },
        "context": context,
    }


def lambda_handler(event, context):
    method_arn = event.get("methodArn")

    log_json(
        "Authorizer request received",
        type=event.get("type"),
        methodArn=method_arn,
        requestId=getattr(context, "aws_request_id", None),
    )

    authorization_header = event.get("authorizationToken")
    if not authorization_header:
        log_json("Authorization header missing", level="warning")
        raise Exception("Unauthorized")

    token = authorization_header.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()

    if not token or not method_arn:
        log_json("Invalid authorization request", level="warning")
        raise Exception("Unauthorized")

    try:
        config = get_authentication_config()
        identity = find_identity(config, token)
    except Exception as error:
        log_json(
            "Failed to retrieve authorization configuration",
            level="error",
            error=str(error),
        )
        raise

    if not identity:
        log_json("Invalid authorization token", level="warning")
        raise Exception("Unauthorized")

    parts = method_arn.split("/")
    if len(parts) < 2:
        raise Exception("Unauthorized")

    api_arn = parts[0]
    stage = parts[1]

    if identity["role"] == "ADMIN":
        principal_id = "cloudmart-admin"
    else:
        customer_identity = identity.get("customer_id") or identity.get("email") or "user"
        principal_id = f"cloudmart-customer-{customer_identity}"

    log_json(
        "Authorization successful",
        principalId=principal_id,
        role=identity["role"],
        email=identity.get("email"),
        customer_id=identity.get("customer_id"),
    )

    return build_policy(
        principal_id=principal_id,
        identity=identity,
        api_arn=api_arn,
        stage=stage,
    )
