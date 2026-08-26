import json
import os


def lambda_handler(event, context):
    """
    Simple API Gateway Lambda authorizer.

    Expected Authorization header:
        Bearer <token>

    For the CloudMart project, replace the token validation
    section with your actual authentication mechanism if required.
    """

    method_arn = event.get("methodArn", "*")

    headers = event.get("headers") or {}

    authorization = (
        headers.get("Authorization")
        or headers.get("authorization")
        or ""
    )

    token = authorization.replace("Bearer ", "").strip()

    # Basic validation.
    # Set CLOUDMART_AUTH_TOKEN as a Lambda environment variable
    # if you want to enable token checking.
    expected_token = os.environ.get("CLOUDMART_AUTH_TOKEN", "")

    if expected_token and token == expected_token:
        effect = "Allow"
    elif not expected_token:
        # Development mode.
        effect = "Allow"
    else:
        effect = "Deny"

    return {
        "principalId": "cloudmart-user",
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": effect,
                    "Resource": method_arn
                }
            ]
        },
        "context": {
            "environment": os.environ.get("ENVIRONMENT", "dev")
        }
    }