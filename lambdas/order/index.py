import json
import os
from decimal import Decimal

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
    order_id = path_parameters.get("order_id")

    try:

        connection = get_db_connection()

        try:
            with connection.cursor() as cursor:

                # GET /orders/{order_id}
                if method == "GET" and order_id:

                    cursor.execute(
                        """
                        SELECT *
                        FROM orders
                        WHERE order_id = %s
                        """,
                        (order_id,)
                    )

                    order = cursor.fetchone()

                    if not order:
                        return response(
                            404,
                            {"message": "Order not found"}
                        )

                    return response(
                        200,
                        {"order": order}
                    )

                # POST /orders
                if method == "POST":

                    body = event.get("body") or "{}"

                    if event.get("isBase64Encoded"):
                        import base64
                        body = base64.b64decode(body).decode("utf-8")

                    data = json.loads(body)

                    customer_id = data.get("customer_id")
                    items = data.get("items", [])

                    if not customer_id:
                        return response(
                            400,
                            {"message": "customer_id is required"}
                        )

                    if not items:
                        return response(
                            400,
                            {"message": "items are required"}
                        )

                    total_amount = Decimal("0")

                    # Validate products and calculate order total.
                    for item in items:

                        product_id = item.get("product_id")
                        quantity = int(item.get("quantity", 0))

                        if not product_id or quantity <= 0:
                            return response(
                                400,
                                {
                                    "message":
                                    "Each item requires product_id and quantity"
                                }
                            )

                        cursor.execute(
                            """
                            SELECT
                                product_id,
                                price,
                                stock_quantity
                            FROM products
                            WHERE product_id = %s
                            """,
                            (product_id,)
                        )

                        product = cursor.fetchone()

                        if not product:
                            connection.rollback()

                            return response(
                                404,
                                {
                                    "message":
                                    f"Product {product_id} not found"
                                }
                            )

                        if product["stock_quantity"] < quantity:
                            connection.rollback()

                            return response(
                                400,
                                {
                                    "message":
                                    f"Insufficient stock for product "
                                    f"{product_id}"
                                }
                            )

                        total_amount += (
                            Decimal(str(product["price"])) * quantity
                        )

                    # Create order.
                    cursor.execute(
                        """
                        INSERT INTO orders
                        (
                            customer_id,
                            total_amount,
                            status
                        )
                        VALUES (%s, %s, %s)
                        """,
                        (
                            customer_id,
                            total_amount,
                            "PENDING"
                        )
                    )

                    new_order_id = cursor.lastrowid

                    # Create order items and update stock.
                    for item in items:

                        product_id = item["product_id"]
                        quantity = int(item["quantity"])

                        cursor.execute(
                            """
                            SELECT price
                            FROM products
                            WHERE product_id = %s
                            """,
                            (product_id,)
                        )

                        product = cursor.fetchone()

                        unit_price = Decimal(
                            str(product["price"])
                        )

                        cursor.execute(
                            """
                            INSERT INTO order_items
                            (
                                order_id,
                                product_id,
                                quantity,
                                unit_price
                            )
                            VALUES (%s, %s, %s, %s)
                            """,
                            (
                                new_order_id,
                                product_id,
                                quantity,
                                unit_price
                            )
                        )

                        cursor.execute(
                            """
                            UPDATE products
                            SET stock_quantity =
                                stock_quantity - %s
                            WHERE product_id = %s
                            """,
                            (
                                quantity,
                                product_id
                            )
                        )

                    connection.commit()

                    return response(
                        201,
                        {
                            "message": "Order created",
                            "order_id": new_order_id,
                            "total_amount": str(total_amount),
                            "status": "PENDING"
                        }
                    )

                return response(
                    405,
                    {"message": "Method not allowed"}
                )

        finally:
            connection.close()

    except Exception as exc:

        print(f"Order Lambda error: {exc}")

        return response(
            500,
            {"message": "Internal server error"}
        )