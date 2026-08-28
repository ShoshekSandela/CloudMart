import json
import os

import boto3


ssm = boto3.client("ssm")

TOKEN_PARAMETER_NAME = os.environ["TOKEN_PARAMETER_NAME"]


def log_json(message, level="info", **fields):
    record = {
        "message": message,
        **fields
    }
    print(json.dumps(record))
    # CloudWatch receives one JSON object per log line.


def get_expected_token():
    response = ssm.get_parameter(
        Name=TOKEN_PARAMETER_NAME,
        WithDecryption=True
    )
    return response["Parameter"]["Value"]


def generate_policy(principal_id, effect, resource):
    return {
        "principalId": principal_id,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": effect,
                    "Resource": resource
                }
            ]
        }
    }


def lambda_handler(event, context):
    method_arn = event.get("methodArn")

    log_json(
        "Authorizer request received",
        type=event.get("type"),
        methodArn=method_arn,
        requestId=getattr(context, "aws_request_id", None)
    )

    authorization_header = event.get("authorizationToken")

    if not authorization_header:
        log_json("Authorization header missing", level="warning")
        raise Exception("Unauthorized")

    token = authorization_header.strip()

    # Accept either the configured token directly or:
    # Authorization: Bearer <token>
    if token.lower().startswith("bearer "):
        token = token[7:].strip()

    if not token or not method_arn:
        log_json("Invalid authorization request", level="warning")
        raise Exception("Unauthorized")

    try:
        expected_token = get_expected_token()
    except Exception as error:
        log_json(
            "Failed to retrieve token from SSM",
            level="error",
            error=str(error)
        )
        raise

    if token != expected_token:
        log_json("Invalid authorization token", level="warning")
        raise Exception("Unauthorized")

    # Authorize the complete API represented by this execution.
    api_arn = method_arn.split("/", 2)[0]

    log_json(
        "Authorization successful",
        principalId="cloudmart-user"
    )

    return generate_policy(
        principal_id="cloudmart-user",
        effect="Allow",
        resource=f"{api_arn}/*/*"
    )
