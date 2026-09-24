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


def get_ssm_parameter(name, decrypt=False):
    return ssm.get_parameter(Name=name, WithDecryption=decrypt)["Parameter"]["Value"].strip()


def get_db_connection():
    return pymysql.connect(
        host=get_ssm_parameter(DB_HOST_PARAMETER_NAME),
        port=int(get_ssm_parameter(DB_PORT_PARAMETER_NAME)),
        database=get_ssm_parameter(DB_NAME_PARAMETER_NAME),
        user=get_ssm_parameter(DB_USERNAME_PARAMETER_NAME, decrypt=True),
        password=get_ssm_parameter(DB_PASSWORD_PARAMETER_NAME, decrypt=True),
        connect_timeout=5,
        read_timeout=5,
        write_timeout=5,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def generate_token():
    return secrets.token_urlsafe(32)


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

    if not isinstance(config, dict) or not config.get("admin_token"):
        token = generate_token()
        ssm.put_parameter(
            Name=TOKEN_PARAMETER_NAME,
            Value=json.dumps({"admin_token": token}),
            Type="SecureString",
            Overwrite=True,
        )
        logger.info("Admin authentication token initialized")
        return token, True

    return str(config["admin_token"]), False


def initialize_customer_tokens(connection):
    generated = []
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT customer_id
            FROM customers
            ORDER BY customer_id
        """)
        customers = cursor.fetchall()

        for customer in customers:
            customer_id = int(customer["customer_id"])
            cursor.execute("""
                SELECT token_id, status
                FROM customer_tokens
                WHERE customer_id = %s
                LIMIT 1
            """, (customer_id,))
            existing = cursor.fetchone()

            if existing and str(existing["status"]).upper() == "ACTIVE":
                continue

            token = generate_token()
            token_hash = hash_token(token)
            if existing:
                cursor.execute("""
                    UPDATE customer_tokens
                    SET token_hash = %s, status = 'ACTIVE'
                    WHERE token_id = %s
                """, (token_hash, existing["token_id"]))
            else:
                cursor.execute("""
                    INSERT INTO customer_tokens (customer_id, token_hash, status)
                    VALUES (%s, %s, 'ACTIVE')
                """, (customer_id, token_hash))

            generated.append({"customer_id": customer_id, "token": token})

    connection.commit()
    return generated


def rotate_customer_tokens(connection):
    generated = []
    with connection.cursor() as cursor:
        cursor.execute("SELECT customer_id FROM customers ORDER BY customer_id")
        for customer in cursor.fetchall():
            customer_id = int(customer["customer_id"])
            token = generate_token()
            token_hash = hash_token(token)
            cursor.execute("""
                UPDATE customer_tokens
                SET token_hash = %s, status = 'ACTIVE'
                WHERE customer_id = %s
            """, (token_hash, customer_id))
            if cursor.rowcount == 0:
                cursor.execute("""
                    INSERT INTO customer_tokens (customer_id, token_hash, status)
                    VALUES (%s, %s, 'ACTIVE')
                """, (customer_id, token_hash))
            generated.append({"customer_id": customer_id, "token": token})

    connection.commit()
    return generated


def lambda_handler(event, context):
    action = str(event.get("action") or "initialize_tokens").lower()
    connection = None
    try:
        admin_token, admin_generated = initialize_admin_token()
        connection = get_db_connection()

        if action == "rotate_customer_tokens":
            tokens = rotate_customer_tokens(connection)
            return {
                "statusCode": 200,
                "message": "Customer authentication tokens rotated successfully",
                "customer_token_count": len(tokens),
                "customer_tokens": tokens,
            }

        if action == "initialize_tokens":
            tokens = initialize_customer_tokens(connection)
            return {
                "statusCode": 200,
                "message": "CloudMart authentication tokens initialized",
                "admin_token_generated": admin_generated,
                "customer_token_count": len(tokens),
                "new_customer_tokens": tokens,
                "admin_token": admin_token if admin_generated else None,
            }

        raise ValueError(f"Unsupported token management action: {action}")

    except Exception:
        if connection:
            connection.rollback()
        logger.exception("Token management operation failed")
        raise
    finally:
        if connection:
            connection.close()
