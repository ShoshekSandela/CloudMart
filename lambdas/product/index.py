import json
import os

import pymysql


def get_db_connection():
    return pymysql.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ["DB_USERNAME"],
        password=os.environ["DB_PASSWORD"],
        database=os.environ["DB_NAME"],
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10
    )


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*"
        },
        "body": json.dumps(body, default=str)
    }


def lambda_handler(event, context):

    method = event.get("httpMethod", "GET")
    path_parameters = event.get("pathParameters") or {}

    product_id = path_parameters.get("product_id")

    try:

        connection = get_db_connection()

        try:
            with connection.cursor() as cursor:

                # GET /products/{product_id}
                if method == "GET" and product_id:

                    cursor.execute(
                        """
                        SELECT
                            product_id,
                            name,
                            description,
                            price,
                            stock_quantity,
                            status
                        FROM products
                        WHERE product_id = %s
                        """,
                        (product_id,)
                    )

                    product = cursor.fetchone()

                    if not product:
                        return response(
                            404,
                            {"message": "Product not found"}
                        )

                    return response(
                        200,
                        {"product": product}
                    )

                # GET /products
                if method == "GET":

                    cursor.execute(
                        """
                        SELECT
                            product_id,
                            name,
                            description,
                            price,
                            stock_quantity,
                            status
                        FROM products
                        ORDER BY product_id
                        """
                    )

                    products = cursor.fetchall()

                    return response(
                        200,
                        {"products": products}
                    )

                # POST /products
                if method == "POST":

                    body = event.get("body") or "{}"

                    if event.get("isBase64Encoded"):
                        import base64
                        body = base64.b64decode(body).decode("utf-8")

                    data = json.loads(body)

                    name = data.get("name")
                    description = data.get("description")
                    price = data.get("price")
                    stock_quantity = data.get("stock_quantity", 0)
                    status = data.get("status", "ACTIVE")

                    if not name or price is None:
                        return response(
                            400,
                            {
                                "message": "name and price are required"
                            }
                        )

                    cursor.execute(
                        """
                        INSERT INTO products
                        (
                            name,
                            description,
                            price,
                            stock_quantity,
                            status
                        )
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (
                            name,
                            description,
                            price,
                            stock_quantity,
                            status
                        )
                    )

                    connection.commit()

                    new_product_id = cursor.lastrowid

                    return response(
                        201,
                        {
                            "message": "Product created",
                            "product_id": new_product_id
                        }
                    )

                return response(
                    405,
                    {"message": "Method not allowed"}
                )

        finally:
            connection.close()

    except Exception as exc:

        print(f"Product Lambda error: {exc}")

        return response(
            500,
            {"message": "Internal server error"}
        )