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

    s3.put_object(
        Bucket=os.environ["REPORT_BUCKET_NAME"],
        Key=key,
        Body=output.getvalue().encode("utf-8"),
        ContentType="text/csv",
    )

    logger.info("Daily report uploaded to s3://%s/%s", os.environ["REPORT_BUCKET_NAME"], key)

    return {
        "statusCode": 200,
        "report_bucket": os.environ["REPORT_BUCKET_NAME"],
        "report_key": key,
        "products": len(products),
        "orders": len(orders),
    }
