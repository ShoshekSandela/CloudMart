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


def lambda_handler(event, context):
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

    # ============================================================
    # SAMPLE ORDER DATA
    # ============================================================
    # If there are no real orders in RDS yet, use sample records
    # only for the report. This does NOT insert anything into RDS.
    #
    # This lets the dashboard/CSV show realistic order statuses
    # while the actual order table is still empty.
    # ============================================================

    # For a one-time sample report, invoke the Lambda with:
    # {
    #   "use_sample_data": true
    # }
    #
    # Normal EventBridge invocation remains {} and uses real RDS orders.
    use_sample_data = bool(event.get("use_sample_data", False))

    if use_sample_data or not orders:
        sample_time = datetime.now(timezone.utc)

        orders = [
            {
                "order_id": 1001,
                "customer_name": "Shoshek",
                "status": "COMPLETED",
                "total_amount": 500.00,
                "created_at": sample_time,
            },
            {
                "order_id": 1002,
                "customer_name": "Shoshek",
                "status": "CONFIRMED",
                "total_amount": 1000.00,
                "created_at": sample_time,
            },
            {
                "order_id": 1003,
                "customer_name": "Shoshek",
                "status": "FAILED",
                "total_amount": 500000.00,
                "created_at": sample_time,
            },
            {
                "order_id": 1004,
                "customer_name": "Shoshek",
                "status": "CANCELED",
                "total_amount": 500.00,
                "created_at": sample_time,
            },
            {
                "order_id": 1005,
                "customer_name": "Manjula",
                "status": "COMPLETED",
                "total_amount": 899.99,
                "created_at": sample_time,
            },
            {
                "order_id": 1006,
                "customer_name": "Manjula",
                "status": "CONFIRMED",
                "total_amount": 79.99,
                "created_at": sample_time,
            },
            {
                "order_id": 1007,
                "customer_name": "Manjula",
                "status": "FAILED",
                "total_amount": 2999.00,
                "created_at": sample_time,
            },
            {
                "order_id": 1008,
                "customer_name": "Shyam",
                "status": "CANCELED",
                "total_amount": 29.99,
                "created_at": sample_time,
            },
            {
                "order_id": 1009,
                "customer_name": "Shyam",
                "status": "COMPLETED",
                "total_amount": 79.99,
                "created_at": sample_time,
            },
            {
                "order_id": 1010,
                "customer_name": "Adhithya",
                "status": "CONFIRMED",
                "total_amount": 929.98,
                "created_at": sample_time,
            },
        ]

        if use_sample_data:
            logger.info("Sample report requested. Using 10 sample orders for this report only.")
        else:
            logger.info("No orders found in RDS. Using 10 sample orders for this report only.")


    # Build one normalized CSV file.  Every data row has a record_type so the
    # dashboard can reliably separate products and orders.
    #
    # This also avoids the old section-header format ("PRODUCT REPORT" /
    # "ORDER REPORT"), which csv.DictReader cannot interpret as normal rows.
    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow([
        "record_type",
        "id",
        "name",
        "customer",
        "status",
        "stock_quantity",
        "low_stock_threshold",
        "total_amount",
        "created_or_updated_at",
    ])

    for product in products:
        writer.writerow([
            "PRODUCT",
            product["product_id"],
            product["name"],
            "",
            product["status"],
            product["stock_quantity"],
            product["low_stock_threshold"],
            "",
            product["updated_at"],
        ])

    for order in orders:
        writer.writerow([
            "ORDER",
            order["order_id"],
            "",
            order["customer_name"],
            order["status"],
            "",
            "",
            order["total_amount"],
            order["created_at"],
        ])

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    key = f"{os.environ.get('REPORT_PREFIX', 'reports')}/daily-report-{timestamp}.csv"

    csv_body = output.getvalue().encode("utf-8")

    logger.info(
        "Uploading daily report: bucket=%s key=%s bytes=%d products=%d orders=%d",
        os.environ["REPORT_BUCKET_NAME"],
        key,
        len(csv_body),
        len(products),
        len(orders),
    )

    s3.put_object(
        Bucket=os.environ["REPORT_BUCKET_NAME"],
        Key=key,
        Body=csv_body,
        ContentType="text/csv",
    )

    logger.info(
        "Daily report uploaded successfully to s3://%s/%s",
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
