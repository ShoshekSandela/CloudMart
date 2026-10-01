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
    return ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]


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


def seed_report_test_data(cursor):
    """
    Creates 10 report-only test orders once.

    Enable with Lambda environment variable:
        SEED_REPORT_TEST_DATA=true

    The seed is idempotent. Once the 10 test orders exist, running the
    Report Lambda again will not create another set.

    This is intentionally kept behind an environment variable so normal
    daily reports never create fake orders.
    """
    if os.environ.get("SEED_REPORT_TEST_DATA", "false").lower() != "true":
        return False

    # Do not create duplicates if the test data was already seeded.
    cursor.execute(
        """
        SELECT COUNT(*) AS test_order_count
        FROM order_status_history
        WHERE changed_by = 'REPORT_TEST_DATA'
        """
    )
    existing = int(cursor.fetchone()["test_order_count"])

    if existing > 0:
        logger.info(
            "Report test data already exists. Skipping test-data generation."
        )
        return False

    # Prefer the customer names already visible in the CloudMart test data.
    # If one of those names is not present, fill the remaining slots with
    # other existing customers.
    cursor.execute(
        """
        SELECT
            customer_id,
            customer_name,
            customer_email
        FROM customers
        ORDER BY
            CASE LOWER(customer_name)
                WHEN 'manjula' THEN 1
                WHEN 'shyam' THEN 2
                WHEN 'adhithya' THEN 3
                ELSE 4
            END,
            customer_id
        LIMIT 3
        """
    )
    customers = cursor.fetchall()

    if len(customers) < 3:
        raise RuntimeError(
            "Report test data requires at least 3 customers in the customers table."
        )

    cursor.execute(
        """
        SELECT
            product_id,
            price
        FROM products
        WHERE status = 'ACTIVE'
        ORDER BY product_id
        LIMIT 2
        """
    )
    products = cursor.fetchall()

    if len(products) < 2:
        raise RuntimeError(
            "Report test data requires at least 2 ACTIVE products."
        )

    customer_1 = customers[0]
    customer_2 = customers[1]
    customer_3 = customers[2]

    product_1 = products[0]
    product_2 = products[1]

    # Ten records:
    # 3 CONFIRMED, 2 FAILED, 2 CANCELED, 3 COMPLETED.
    test_orders = [
        (customer_1, product_1, 1, "CONFIRMED"),
        (customer_1, product_2, 1000, "FAILED"),
        (customer_1, product_2, 2, "COMPLETED"),
        (customer_1, product_1, 1000, "FAILED"),
        (customer_2, product_1, 1, "CANCELED"),
        (customer_2, product_2, 3, "CONFIRMED"),
        (customer_2, product_1, 2, "COMPLETED"),
        (customer_3, product_2, 1, "CANCELED"),
        (customer_3, product_1, 3, "CONFIRMED"),
        (customer_3, product_2, 2, "COMPLETED"),
    ]

    created_order_ids = []

    for customer, product, quantity, final_status in test_orders:
        # Keep money as Decimal to avoid floating-point rounding errors.
        total_amount = product["price"] * quantity

        cursor.execute(
            """
            INSERT INTO orders (
                customer_id,
                customer_email,
                status,
                total_amount
            )
            VALUES (%s, %s, %s, %s)
            """,
            (
                customer["customer_id"],
                customer["customer_email"],
                final_status,
                total_amount,
            ),
        )

        order_id = cursor.lastrowid
        created_order_ids.append(order_id)

        # Keep order_items consistent with the generated order.
        cursor.execute(
            """
            INSERT INTO order_items (
                order_id,
                product_id,
                quantity,
                unit_price,
                subtotal
            )
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                order_id,
                product["product_id"],
                quantity,
                product["price"],
                total_amount,
            ),
        )

        # Keep the lifecycle history consistent with the final status.
        history = [
            (order_id, None, "PENDING"),
        ]

        if final_status == "CONFIRMED":
            history.append((order_id, "PENDING", "CONFIRMED"))
        elif final_status == "FAILED":
            history.append((order_id, "PENDING", "FAILED"))
        elif final_status == "CANCELED":
            history.extend(
                [
                    (order_id, "PENDING", "CONFIRMED"),
                    (order_id, "CONFIRMED", "CANCELED"),
                ]
            )
        elif final_status == "COMPLETED":
            history.extend(
                [
                    (order_id, "PENDING", "CONFIRMED"),
                    (order_id, "CONFIRMED", "COMPLETED"),
                ]
            )

        for history_order_id, old_status, new_status in history:
            cursor.execute(
                """
                INSERT INTO order_status_history (
                    order_id,
                    old_status,
                    new_status,
                    changed_by
                )
                VALUES (%s, %s, %s, 'REPORT_TEST_DATA')
                """,
                (
                    history_order_id,
                    old_status,
                    new_status,
                ),
            )

    logger.info(
        "Created %d report test orders: %s",
        len(created_order_ids),
        created_order_ids,
    )

    return True


def lambda_handler(event, context):
    connection = get_connection()
    test_data_seeded = False

    try:
        with connection.cursor() as cursor:
            # Optional test-data generation.
            #
            # Keep SEED_REPORT_TEST_DATA=false (or unset) for normal
            # production/daily-report operation.
            test_data_seeded = seed_report_test_data(cursor)

            # Commit only after the complete test dataset is created.
            # The normal report path performs no database writes.
            if test_data_seeded:
                connection.commit()

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
                JOIN customers c
                    ON c.customer_id = o.customer_id
                ORDER BY o.created_at DESC
                LIMIT 100
                """
            )
            orders = cursor.fetchall()

    except Exception:
        if test_data_seeded:
            connection.rollback()
        logger.exception("Report generation failed.")
        raise
    finally:
        connection.close()

    # Build ONE CSV file with two clearly separated sections:
    # 1. PRODUCT REPORT
    # 2. ORDER REPORT
    #
    # We intentionally keep a single S3 object/report file.

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

    # Blank rows separating the two report sections.
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
    key = f"{os.environ.get('REPORT_PREFIX', 'reports')}/daily-report-{timestamp}.csv"

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
        "test_data_seeded": test_data_seeded,
    }
