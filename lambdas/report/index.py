import json
import os
from datetime import datetime, timezone

import boto3
import pymysql


s3 = boto3.client("s3")


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


def lambda_handler(event, context):

    bucket = os.environ["REPORT_BUCKET"]

    try:

        connection = get_db_connection()

        try:
            with connection.cursor() as cursor:

                cursor.execute(
                    """
                    SELECT
                        COUNT(*) AS total_orders,
                        COALESCE(SUM(total_amount), 0) AS total_sales
                    FROM orders
                    """
                )

                summary = cursor.fetchone()

                cursor.execute(
                    """
                    SELECT
                        COUNT(*) AS total_products
                    FROM products
                    """
                )

                products = cursor.fetchone()

        finally:
            connection.close()

        generated_at = datetime.now(
            timezone.utc
        ).isoformat()

        report = {
            "project": "CloudMart",
            "environment": os.environ.get(
                "ENVIRONMENT",
                "dev"
            ),
            "generated_at": generated_at,
            "summary": {
                "total_orders": summary["total_orders"],
                "total_sales": str(summary["total_sales"]),
                "total_products": products["total_products"]
            }
        }

        timestamp = datetime.now(
            timezone.utc
        ).strftime("%Y%m%d-%H%M%S")

        key = f"reports/cloudmart-report-{timestamp}.json"

        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(
                report,
                indent=2,
                default=str
            ).encode("utf-8"),
            ContentType="application/json"
        )

        print(
            f"Report uploaded to s3://{bucket}/{key}"
        )

        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "message": "Report generated successfully",
                    "bucket": bucket,
                    "key": key
                }
            )
        }

    except Exception as exc:

        print(f"Report Lambda error: {exc}")

        raise