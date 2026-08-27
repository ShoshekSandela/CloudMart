import json
import os
import boto3

ssm = boto3.client("ssm")

PARAMETER_NAME = os.environ["AUTH_TOKEN_PARAMETER"]


def get_auth_token():
    response = ssm.get_parameter(
        Name=PARAMETER_NAME,
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

    try:
        print(json.dumps({
            "level": "INFO",
            "message": "Authorizer invoked"
        }))

        authorization_token = event.get("authorizationToken", "")

        if not authorization_token:
            print(json.dumps({
                "level": "WARN",
                "message": "Authorization token missing"
            }))

            return generate_policy(
                "anonymous",
                "Deny",
                event["methodArn"]
            )

        token = authorization_token

        if token.startswith("Bearer "):
            token = token[7:]

        expected_token = get_auth_token()

        if token == expected_token:
            print(json.dumps({
                "level": "INFO",
                "message": "Authorization successful"
            }))

            return generate_policy(
                "cloudmart-user",
                "Allow",
                event["methodArn"]
            )

        print(json.dumps({
            "level": "WARN",
            "message": "Invalid authorization token"
        }))

        return generate_policy(
            "anonymous",
            "Deny",
            event["methodArn"]
        )

    except Exception as error:

        print(json.dumps({
            "level": "ERROR",
            "message": "Authorization failed",
            "error": str(error)
        }))

        return generate_policy(
            "anonymous",
            "Deny",
            event["methodArn"]
        )