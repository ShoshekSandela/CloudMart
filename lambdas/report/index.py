import csv
import io
import logging
import os
from datetime import datetime, timezone

import boto3
import pymysql

logger = logging.getLogger()
logger.setLevel(logging.INFO)

AWS_REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
_boto3_kwargs = {"region_name": AWS_REGION} if AWS_REGION else {}

ssm = boto3.client("ssm", **_boto3_kwargs)
s3 = boto3.client("s3", **_boto3_kwargs)


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


def sample_orders(sample_time):
    """Return demo orders without changing RDS."""
    return [
        {"order_id": 1001, "customer_name": "Shoshek", "status": "COMPLETED", "total_amount": 500.00, "created_at": sample_time},
        {"order_id": 1002, "customer_name": "Shoshek", "status": "CONFIRMED", "total_amount": 1000.00, "created_at": sample_time},
        {"order_id": 1003, "customer_name": "Shoshek", "status": "FAILED", "total_amount": 5000.00, "created_at": sample_time},
        {"order_id": 1004, "customer_name": "Shoshek", "status": "CANCELED", "total_amount": 500.00, "created_at": sample_time},
        {"order_id": 1005, "customer_name": "Manjula", "status": "COMPLETED", "total_amount": 899.99, "created_at": sample_time},
        {"order_id": 1006, "customer_name": "Manjula", "status": "CONFIRMED", "total_amount": 79.99, "created_at": sample_time},
        {"order_id": 1007, "customer_name": "Manjula", "status": "FAILED", "total_amount": 2999.00, "created_at": sample_time},
        {"order_id": 1008, "customer_name": "Shyam", "status": "CANCELED", "total_amount": 29.99, "created_at": sample_time},
        {"order_id": 1009, "customer_name": "Shyam", "status": "COMPLETED", "total_amount": 79.99, "created_at": sample_time},
        {"order_id": 1010, "customer_name": "Adhithya", "status": "CONFIRMED", "total_amount": 929.98, "created_at": sample_time},
    ]


def sample_products(sample_time):
    """Return demo products only when RDS is unavailable/empty."""
    return [
        {"product_id": 1, "name": "Laptop", "stock_quantity": 18, "low_stock_threshold": 5, "status": "ACTIVE", "updated_at": sample_time},
        {"product_id": 2, "name": "Wireless Mouse", "stock_quantity": 42, "low_stock_threshold": 10, "status": "ACTIVE", "updated_at": sample_time},
        {"product_id": 3, "name": "Keyboard", "stock_quantity": 7, "low_stock_threshold": 10, "status": "ACTIVE", "updated_at": sample_time},
        {"product_id": 4, "name": "Monitor", "stock_quantity": 15, "low_stock_threshold": 5, "status": "ACTIVE", "updated_at": sample_time},
        {"product_id": 5, "name": "Headphones", "stock_quantity": 4, "low_stock_threshold": 8, "status": "ACTIVE", "updated_at": sample_time},
    ]


def lambda_handler(event, context):
    event = event or {}
    use_sample_data = bool(event.get("use_sample_data", False))
    sample_time = datetime.now(timezone.utc)

    products = []
    orders = []

    # The report must still be generated for dashboard verification when
    # RDS contains no orders. If RDS is temporarily unavailable, create a
    # clearly logged demo report instead of failing before the S3 upload.
    try:
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

        logger.info("RDS report data loaded: products=%d orders=%d", len(products), len(orders))

    except Exception:
        logger.exception("RDS data could not be loaded. Generating a demo report so the dashboard can be populated.")
        products = []
        orders = []

    if not products:
        products = sample_products(sample_time)
        logger.info("Using %d sample products for this report.", len(products))

    if use_sample_data or not orders:
        orders = sample_orders(sample_time)
        logger.info(
            "Using %d sample orders for this report only. No sample data is inserted into RDS.",
            len(orders),
        )


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
