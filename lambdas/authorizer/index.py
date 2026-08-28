import json
import os
import boto3


ssm = boto3.client("ssm")


TOKEN_PARAMETER_NAME = os.environ.get(
    "TOKEN_PARAMETER_NAME",
    "/cloudmart/dev/auth/token"
)


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

    print(
        json.dumps(
            {
                "message": "Authorizer request received",
                "type": event.get("type"),
                "methodArn": event.get("methodArn")
            }
        )
    )

    authorization_header = event.get("authorizationToken")

    if not authorization_header:
        print(
            json.dumps(
                {
                    "message": "Authorization header missing"
                }
            )
        )

        raise Exception("Unauthorized")


    token = authorization_header.strip()


    try:

        expected_token = get_expected_token()

    except Exception as error:

        print(
            json.dumps(
                {
                    "message": "Failed to retrieve token from SSM",
                    "error": str(error)
                }
            )
        )

        raise


    if token != expected_token:

        print(
            json.dumps(
                {
                    "message": "Invalid authorization token"
                }
            )
        )

        raise Exception("Unauthorized")


    method_arn = event.get("methodArn")

    if not method_arn:

        print(
            json.dumps(
                {
                    "message": "methodArn missing"
                }
            )
        )

        raise Exception("Unauthorized")


    print(
        json.dumps(
            {
                "message": "Authorization successful"
            }
        )
    )

    api_arn = method_arn.split("/", 2)[0]

    return generate_policy(
        principal_id="cloudmart-user",
        effect="Allow",
        resource=f"{api_arn}/*/*"
    )