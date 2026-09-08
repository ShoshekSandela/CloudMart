import json
import os

import boto3


ssm = boto3.client("ssm")

TOKEN_PARAMETER_NAME = os.environ["TOKEN_PARAMETER_NAME"]


def log_json(message, level="info", **fields):
    record = {
        "message": message,
        "level": level,
        **fields,
    }
    print(json.dumps(record))


def get_authentication_config():
    response = ssm.get_parameter(
        Name=TOKEN_PARAMETER_NAME,
        WithDecryption=True,
    )

    value = response["Parameter"]["Value"].strip()

    # Backward compatible: a plain SSM value is treated as an ADMIN token.
    try:
        config = json.loads(value)
    except json.JSONDecodeError:
        return {
            "admin_token": value,
            "admin_email": os.environ.get(
                "ADMIN_EMAIL",
                "admin@cloudmart.com",
            ),
        }

    if not isinstance(config, dict):
        raise ValueError(
            "Authorization configuration must be a JSON object"
        )

    return config


def find_identity(config, token):
    if token == str(config.get("admin_token", "")):
        return {
            "role": "ADMIN",
            "email": config.get(
                "admin_email",
                "admin@cloudmart.com",
            ),
            "customer_id": None,
        }

    if token == str(config.get("customer_token", "")):
        return {
            "role": "CUSTOMER",
            "email": config.get("customer_email"),
            "customer_id": config.get(
                "customer_customer_id",
                config.get("customer_id"),
            ),
        }

    return None


def generate_policy(
    principal_id,
    effect,
    resource,
    identity,
):
    context = {
        "role": identity["role"],
    }

    if identity.get("email"):
        context["email"] = str(identity["email"])

    if identity.get("customer_id") is not None:
        context["customer_id"] = str(identity["customer_id"])

    return {
        "principalId": principal_id,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": effect,
                    "Resource": resource,
                }
            ],
        },
        "context": context,
    }


def lambda_handler(event, context):
    method_arn = event.get("methodArn")

    log_json(
        "Authorizer request received",
        type=event.get("type"),
        methodArn=method_arn,
        requestId=getattr(
            context,
            "aws_request_id",
            None,
        ),
    )

    authorization_header = event.get(
        "authorizationToken"
    )

    if not authorization_header:
        log_json(
            "Authorization header missing",
            level="warning",
        )
        raise Exception("Unauthorized")

    token = authorization_header.strip()

    if token.lower().startswith("bearer "):
        token = token[7:].strip()

    if not token or not method_arn:
        log_json(
            "Invalid authorization request",
            level="warning",
        )
        raise Exception("Unauthorized")

    try:
        config = get_authentication_config()
        identity = find_identity(
            config,
            token,
        )
    except Exception as error:
        log_json(
            "Failed to retrieve authorization configuration",
            level="error",
            error=str(error),
        )
        raise

    if not identity:
        log_json(
            "Invalid authorization token",
            level="warning",
        )
        raise Exception("Unauthorized")

    api_arn = method_arn.split(
        "/",
        2,
    )[0]

    if identity["role"] == "ADMIN":
        principal_id = "cloudmart-admin"
    else:
        customer_identity = (
            identity.get("customer_id")
            or identity.get("email")
            or "user"
        )
        principal_id = (
            f"cloudmart-customer-{customer_identity}"
        )

    log_json(
        "Authorization successful",
        principalId=principal_id,
        role=identity["role"],
        email=identity.get("email"),
        customer_id=identity.get("customer_id"),
    )

    return generate_policy(
        principal_id=principal_id,
        effect="Allow",
        resource=f"{api_arn}/*/*/*",
        identity=identity,
    )
