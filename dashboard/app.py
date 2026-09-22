import json
import os
import secrets
from functools import wraps

import boto3
import pymysql
from flask import Flask, redirect, render_template, request, session, url_for

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)

ssm = boto3.client("ssm")
s3 = boto3.client("s3")


def get_parameter(name, decrypt=True):
    return ssm.get_parameter(Name=name, WithDecryption=decrypt)["Parameter"]["Value"]


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


def admin_token():
    value = get_parameter(os.environ["AUTH_TOKEN_PARAMETER_NAME"])
    data = json.loads(value)
    return data.get("admin_token", "")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


@app.get("/")
def home():
    return redirect(url_for("dashboard"))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        supplied_token = request.form.get("token", "").strip()
        if supplied_token and secrets.compare_digest(supplied_token, admin_token()):
            session["authenticated"] = True
            return redirect(url_for("dashboard"))
        error = "Invalid admin token."
    return render_template("login.html", error=error)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/dashboard")
@login_required
def dashboard():
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT product_id, name, price, stock_quantity,
                       low_stock_threshold, status, updated_at
                FROM products
                WHERE status = 'ACTIVE'
                ORDER BY product_id
                """
            )
            products = cursor.fetchall()

            cursor.execute(
                """
                SELECT o.order_id, c.customer_name, o.status,
                       o.total_amount, o.created_at
                FROM orders o
                JOIN customers c ON c.customer_id = o.customer_id
                ORDER BY o.created_at DESC
                LIMIT 20
                """
            )
            orders = cursor.fetchall()

            cursor.execute(
                """
                SELECT
                    COUNT(*) AS total_orders,
                    COALESCE(SUM(total_amount), 0) AS total_revenue,
                    COUNT(DISTINCT customer_id) AS total_customers
                FROM orders
                """
            )
            metrics = cursor.fetchone() or {}

            cursor.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM orders
                GROUP BY status
                ORDER BY count DESC, status
                """
            )
            order_statuses = cursor.fetchall()
    finally:
        connection.close()

    low_stock_products = [
        product
        for product in products
        if int(product["stock_quantity"]) <= int(product["low_stock_threshold"])
    ]

    metrics["total_products"] = len(products)
    metrics["low_stock_count"] = len(low_stock_products)

    report = latest_report()

    return render_template(
        "dashboard.html",
        products=products,
        orders=orders,
        low_stock_count=len(low_stock_products),
        low_stock_products=low_stock_products,
        metrics=metrics,
        order_statuses=order_statuses,
        report=report,
    )


def latest_report():
    result = s3.list_objects_v2(
        Bucket=os.environ["REPORT_BUCKET_NAME"],
        Prefix=os.environ.get("REPORT_PREFIX", "reports") + "/",
    )
    objects = [item for item in result.get("Contents", []) if item["Key"].endswith(".csv")]
    if not objects:
        return None

    latest = max(objects, key=lambda item: item["LastModified"])
    url = s3.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": os.environ["REPORT_BUCKET_NAME"],
            "Key": latest["Key"],
        },
        ExpiresIn=3600,
    )
    return {
        "key": latest["Key"],
        "url": url,
        "last_modified": latest["LastModified"],
    }


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80)
