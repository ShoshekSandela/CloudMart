import json
import os
import logging

import boto3
import pymysql


logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json"
        },
        "body": json.dumps(body, default=str)
    }


def get_parameter(name, decrypt=False):
    result = ssm.get_parameter(
        Name=name,
        WithDecryption=decrypt
    )
    return result["Parameter"]["Value"]


def get_db_connection():
    host = get_parameter(
        os.environ["DB_HOST_PARAMETER_NAME"]
    )

    port = int(
        get_parameter(
            os.environ["DB_PORT_PARAMETER_NAME"]
        )
    )

    database = get_parameter(
        os.environ["DB_NAME_PARAMETER_NAME"]
    )

    username = get_parameter(
        os.environ["DB_USERNAME_PARAMETER_NAME"]
    )

    password = get_parameter(
        os.environ["DB_PASSWORD_PARAMETER_NAME"],
        decrypt=True
    )

    return pymysql.connect(
        host=host,
        port=port,
        user=username,
        password=password,
        database=database,
        connect_timeout=5,
        read_timeout=10,
        write_timeout=10,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True
    )


def lambda_handler(event, context):

    logger.info(
        json.dumps({
            "message": "Product request received",
            "httpMethod": event.get("httpMethod"),
            "path": event.get("path")
        })
    )

    connection = None

    try:

        method = event.get("httpMethod", "").upper()

        path_parameters = event.get("pathParameters") or {}

        product_id = path_parameters.get("id")

        body = event.get("body")

        payload = json.loads(body) if body else {}

        connection = get_db_connection()

        with connection.cursor() as cursor:

            # ==================================================
            # GET /products
            # ==================================================

            if method == "GET" and not product_id:

                cursor.execute("""
                    SELECT
                        product_id,
                        name,
                        description,
                        price,
                        stock_quantity,
                        low_stock_threshold,
                        status,
                        created_at,
                        updated_at
                    FROM products
                    ORDER BY product_id
                """)

                rows = cursor.fetchall()

                return response(200, rows)

            # ==================================================
            # GET /products/{id}
            # ==================================================

            if method == "GET" and product_id:

                cursor.execute("""
                    SELECT
                        product_id,
                        name,
                        description,
                        price,
                        stock_quantity,
                        low_stock_threshold,
                        status,
                        created_at,
                        updated_at
                    FROM products
                    WHERE product_id = %s
                """, (product_id,))

                row = cursor.fetchone()

                if not row:
                    return response(
                        404,
                        {
                            "message": "Product not found"
                        }
                    )

                return response(200, row)

            # ==================================================
            # POST /products
            # ==================================================

            if method == "POST":

                required = [
                    "name",
                    "price",
                    "stock_quantity",
                    "low_stock_threshold",
                    "status"
                ]

                missing = [
                    field
                    for field in required
                    if field not in payload
                ]

                if missing:
                    return response(
                        400,
                        {
                            "message": "Missing required fields",
                            "fields": missing
                        }
                    )

                cursor.execute("""
                    INSERT INTO products (
                        name,
                        description,
                        price,
                        stock_quantity,
                        low_stock_threshold,
                        status
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (
                    payload["name"],
                    payload.get("description"),
                    payload["price"],
                    payload["stock_quantity"],
                    payload["low_stock_threshold"],
                    payload["status"]
                ))

                new_id = cursor.lastrowid

                cursor.execute("""
                    SELECT
                        product_id,
                        name,
                        description,
                        price,
                        stock_quantity,
                        low_stock_threshold,
                        status,
                        created_at,
                        updated_at
                    FROM products
                    WHERE product_id = %s
                """, (new_id,))

                return response(
                    201,
                    cursor.fetchone()
                )

            # ==================================================
            # PUT /products/{id}
            # ==================================================

            if method == "PUT" and product_id:

                allowed = [
                    "name",
                    "description",
                    "price",
                    "stock_quantity",
                    "low_stock_threshold",
                    "status"
                ]

                fields = []
                values = []

                for field in allowed:

                    if field in payload:

                        fields.append(
                            f"{field} = %s"
                        )

                        values.append(
                            payload[field]
                        )

                if not fields:

                    return response(
                        400,
                        {
                            "message":
                                "No fields supplied for update"
                        }
                    )

                values.append(product_id)

                cursor.execute(
                    f"""
                    UPDATE products
                    SET {", ".join(fields)}
                    WHERE product_id = %s
                    """,
                    values
                )

                if cursor.rowcount == 0:

                    return response(
                        404,
                        {
                            "message":
                                "Product not found"
                        }
                    )

                cursor.execute("""
                    SELECT
                        product_id,
                        name,
                        description,
                        price,
                        stock_quantity,
                        low_stock_threshold,
                        status,
                        created_at,
                        updated_at
                    FROM products
                    WHERE product_id = %s
                """, (product_id,))

                return response(
                    200,
                    cursor.fetchone()
                )

            # ==================================================
            # DELETE /products/{id}
            # ==================================================

            if method == "DELETE" and product_id:

                cursor.execute("""
                    DELETE FROM products
                    WHERE product_id = %s
                """, (product_id,))

                if cursor.rowcount == 0:

                    return response(
                        404,
                        {
                            "message":
                                "Product not found"
                        }
                    )

                return response(
                    200,
                    {
                        "message":
                            "Product deleted",
                        "product_id":
                            product_id
                    }
                )

            # ==================================================
            # METHOD NOT ALLOWED
            # ==================================================

            return response(
                405,
                {
                    "message":
                        "Method not allowed"
                }
            )

    except json.JSONDecodeError:

        logger.exception(
            "Invalid JSON body"
        )

        return response(
            400,
            {
                "message":
                    "Invalid JSON body"
            }
        )

    except pymysql.MySQLError as exc:

        logger.exception(
            "Database operation failed"
        )

        return response(
            500,
            {
                "message":
                    "Database operation failed",
                "error":
                    str(exc)
            }
        )

    except Exception as exc:

        logger.exception(
            "Product Lambda failed"
        )

        return response(
            500,
            {
                "message":
                    "Internal server error",
                "error":
                    str(exc)
            }
        )

    finally:

        if connection:

            connection.close()