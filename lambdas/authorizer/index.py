import os
import boto3


# ==========================================================
# AWS CLIENTS
# ==========================================================

ssm = boto3.client("ssm")


# ==========================================================
# ENVIRONMENT VARIABLES
# ==========================================================

TOKEN_PARAMETER_NAME = os.environ["TOKEN_PARAMETER_NAME"]


# ==========================================================
# TOKEN CACHE INSIDE LAMBDA CONTAINER
# ==========================================================

_cached_token = None


# ==========================================================
# LOAD TOKEN FROM SSM SECURESTRING
# ==========================================================

def get_expected_token():

    global _cached_token

    if _cached_token is not None:
        return _cached_token

    response = ssm.get_parameter(
        Name=TOKEN_PARAMETER_NAME,
        WithDecryption=True
    )

    _cached_token = response["Parameter"]["Value"]

    return _cached_token


# ==========================================================
# CREATE IAM POLICY
# ==========================================================

def generate_policy(principal_id, effect, resource):

    policy_document = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Action": "execute-api:Invoke",
                "Effect": effect,
                "Resource": resource
            }
        ]
    }

    return {
        "principalId": principal_id,
        "policyDocument": policy_document
    }


# ==========================================================
# LAMBDA AUTHORIZER
# ==========================================================

def lambda_handler(event, context):

    # ------------------------------------------------------
    # TOKEN AUTHORIZER INPUT
    # ------------------------------------------------------

    authorization_token = event.get("authorizationToken")
    method_arn = event.get("methodArn")

    # ------------------------------------------------------
    # NO TOKEN
    # ------------------------------------------------------

    if not authorization_token:
        raise Exception("Unauthorized")

    # ------------------------------------------------------
    # EXPECT:
    #
    # Authorization: Bearer <token>
    #
    # ------------------------------------------------------

    token = authorization_token.strip()

    if token.lower().startswith("bearer "):
        token = token[7:].strip()

    # ------------------------------------------------------
    # EMPTY TOKEN
    # ------------------------------------------------------

    if not token:
        raise Exception("Unauthorized")

    # ------------------------------------------------------
    # READ EXPECTED TOKEN FROM SSM
    # ------------------------------------------------------

    expected_token = get_expected_token()

    # ------------------------------------------------------
    # WRONG TOKEN
    # ------------------------------------------------------

    if token != expected_token:
        raise Exception("Unauthorized")

    # ------------------------------------------------------
    # CORRECT TOKEN
    # ------------------------------------------------------

    return generate_policy(
        principal_id="cloudmart-api-client",
        effect="Allow",
        resource=method_arn
    )