import csv
import io
import logging
import os
from datetime import datetime, timezone

import boto3
import pymysql

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")
s3 = boto3.client("s3")


def get_parameter(name):
    return ssm.get_parameter(
        Name=name,
        WithDecryption=True
    )["Parameter"]["Value"]


def get_connection():
    return pymysql.connect(
        host=get_parameter(os.environ["DB_HOST_PARAMETER_NAME"]),
        port=int(get_parameter(os.environ["DB_PORT_PARAMETER_NAME"])),
        user=get_parameter(os.environ["DB_USERNAME_PARAMETER_NAME"]),
        password=get_parameter(os.environ["DB_PASSWORD_PARAMETER_NAME"]),
        database=get_parameter(os.environ["DB_NAME_PARAMETER_NAME"]),
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=20,
        write_timeout=20,
    )


def seed_report_test_data():
    """
    Creates exactly 10 report-test orders.

    This function is ONLY called when the Lambda is explicitly invoked with:
        {"action": "seed_report_test_data"}

    It is never called by the normal daily report execution.

    The orders are inserted directly into ORDERS so inventory is not changed.
    """
    connection = get_connection()

    try:
        with connection.cursor() as cursor:
            # Use existing customers. No customer records are created.
            cursor.execute(
                """
                SELECT customer_id, customer_name, customer_email
                FROM customers
                ORDER BY customer_id
                LIMIT 3
                """
            )
            customers = cursor.fetchall()

            if len(customers) < 3:
                raise RuntimeError(
                    "At least 3 customers are required to generate report test data."
                )

            # Use existing active products. No product or inventory records
            # are changed by this test-data action.
            cursor.execute(
                """
                SELECT product_id, price
                FROM products
                WHERE status = 'ACTIVE'
                ORDER BY product_id
                LIMIT 2
                """
            )
            products = cursor.fetchall()

            if len(products) < 2:
                raise RuntimeError(
                    "At least 2 ACTIVE products are required to generate "
                    "report test data."
                )

            customer_1 = customers[0]
            customer_2 = customers[1]
            customer_3 = customers[2]

            product_1 = products[0]
            product_2 = products[1]

            p1 = product_1["price"]
            p2 = product_2["price"]

            # Check whether this explicit report-test set already exists.
            # The marker is stored only in the historical customer_email
            # snapshot on these test orders. Customer records are untouched.
            cursor.execute(
                """
                SELECT order_id, customer_id, status, total_amount
                FROM orders
                WHERE customer_email LIKE %s
                ORDER BY order_id
                """,
                ("%#REPORT_TEST%",),
            )
            existing_orders = cursor.fetchall()

            if len(existing_orders) >= 10:
                return {
                    "created": 0,
                    "already_exists": True,
                    "orders": existing_orders,
                }

            test_orders = [
                (customer_1, "CONFIRMED", product_1["price"] * 1),
                (customer_1, "FAILED", product_1["price"] * 100),
                (customer_1, "COMPLETED", product_2["price"] * 2),
                (customer_1, "FAILED", product_2["price"] * 100),
                (customer_2, "CANCELED", product_1["price"] * 1),
                (customer_2, "CONFIRMED", product_2["price"] * 3),
                (customer_2, "COMPLETED", product_1["price"] * 2),
                (customer_3, "CANCELED", product_2["price"] * 1),
                (customer_3, "CONFIRMED", product_1["price"] * 3),
                (customer_3, "COMPLETED", product_2["price"] * 2),
            ]

            created_orders = []

            for customer, status, amount in test_orders:
                cursor.execute(
                    """
                    INSERT INTO orders
                        (customer_id, customer_email, status, total_amount)
                    VALUES
                        (%s, %s, %s, %s)
                    """,
                    (
                        customer["customer_id"],
                        f"{customer['customer_email']}#REPORT_TEST",
                        status,
                        amount,
                    ),
                )

                created_orders.append(
                    {
                        "order_id": cursor.lastrowid,
                        "customer": customer["customer_name"],
                        "status": status,
                        "total_amount": amount,
                    }
                )

            connection.commit()

            logger.info(
                "Created %d report test orders without changing inventory.",
                len(created_orders),
            )

            return {
                "created": len(created_orders),
                "already_exists": False,
                "orders": created_orders,
            }

    except Exception:
        connection.rollback()
        logger.exception("Report test-data generation failed.")
        raise

    finally:
        connection.close()


def generate_report():
    connection = get_connection()

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    product_id,
                    name,
                    stock_quantity,
                    low_stock_threshold,
                    status,
                    updated_at
                FROM products
                WHERE status = 'ACTIVE'
                ORDER BY product_id
                """
            )
            products = cursor.fetchall()

            cursor.execute(
                """
                SELECT
                    o.order_id,
                    c.customer_name,
                    o.status,
                    o.total_amount,
                    o.created_at
                FROM orders o
                JOIN customers c ON c.customer_id = o.customer_id
                ORDER BY o.created_at DESC
                LIMIT 100
                """
            )
            orders = cursor.fetchall()

    finally:
        connection.close()

    logger.info(
        "Report data loaded: %d products, %d orders.",
        len(products),
        len(orders),
    )

    output = io.StringIO()
    writer = csv.writer(output)

    # ============================================================
    # PRODUCT REPORT
    # ============================================================

    writer.writerow(["PRODUCT REPORT"])
    writer.writerow([])

    writer.writerow([
        "id",
        "name",
        "status",
        "stock_quantity",
        "low_stock_threshold",
        "created_or_updated_at",
    ])

    for product in products:
        writer.writerow([
            product["product_id"],
            product["name"],
            product["status"],
            product["stock_quantity"],
            product["low_stock_threshold"],
            product["updated_at"],
        ])

    writer.writerow([])
    writer.writerow([])
    writer.writerow([])

    # ============================================================
    # ORDER REPORT
    # ============================================================

    writer.writerow(["ORDER REPORT"])
    writer.writerow([])

    writer.writerow([
        "id",
        "customer",
        "status",
        "total_amount",
        "created_or_updated_at",
    ])

    for order in orders:
        writer.writerow([
            order["order_id"],
            order["customer_name"],
            order["status"],
            order["total_amount"],
            order["created_at"],
        ])

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    key = (
        f"{os.environ.get('REPORT_PREFIX', 'reports')}"
        f"/daily-report-{timestamp}.csv"
    )

    s3.put_object(
        Bucket=os.environ["REPORT_BUCKET_NAME"],
        Key=key,
        Body=output.getvalue().encode("utf-8"),
        ContentType="text/csv",
    )

    logger.info(
        "Daily report uploaded to s3://%s/%s",
        os.environ["REPORT_BUCKET_NAME"],
        key,
    )

    return {
        "statusCode": 200,
        "report_bucket": os.environ["REPORT_BUCKET_NAME"],
        "report_key": key,
        "products": len(products),
        "orders": len(orders),
    }


def lambda_handler(event, context):
    event = event or {}

    # ============================================================
    # SAMPLE REPORT ACTION
    # ============================================================
    # Invoke manually with:
    # {
    #   "action": "generate_sample_report"
    # }
    #
    # This creates the 10 test orders (only if they do not already
    # exist) AND immediately generates/uploads the CSV.
    #
    # Normal EventBridge execution uses {} and is NOT affected.
    # ============================================================

    if event.get("action") == "generate_sample_report":
        seed_result = seed_report_test_data()
        report_result = generate_report()

        return {
            "statusCode": 200,
            "message": "Sample orders created/verified and report CSV generated.",
            "sample_data": seed_result,
            "report": report_result,
        }

    # Optional: seed only, without generating a report.
    if event.get("action") == "seed_report_test_data":
        result = seed_report_test_data()

        return {
            "statusCode": 200,
            "message": "Report test-data action completed.",
            **result,
        }

    # Normal daily report generation.
    return generate_report()
