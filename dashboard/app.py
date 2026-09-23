import json
import os
import re
import secrets
from datetime import date, datetime, timezone
from functools import wraps

import boto3
import pymysql
from flask import Flask, redirect, render_template, request, session, url_for

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)

AWS_REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
_boto3_kwargs = {"region_name": AWS_REGION} if AWS_REGION else {}

ssm = boto3.client("ssm", **_boto3_kwargs)
s3 = boto3.client("s3", **_boto3_kwargs)

PAGE_SIZE = 10
REPORT_KEY_PATTERN = re.compile(r"(?:^|/)daily-report-(\d{8})-(\d{6})\.csv$")


def get_parameter(name, decrypt=True):
    return ssm.get_parameter(Name=name, WithDecryption=decrypt)["Parameter"]["Value"]


def get_connection():
    """Create a private VPC RDS connection using SSM-managed parameters."""
    required = (
        "DB_HOST_PARAMETER_NAME",
        "DB_PORT_PARAMETER_NAME",
        "DB_USERNAME_PARAMETER_NAME",
        "DB_PASSWORD_PARAMETER_NAME",
        "DB_NAME_PARAMETER_NAME",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError(
            "Missing CloudMart database configuration: " + ", ".join(missing)
        )

    return pymysql.connect(
        host=get_parameter(os.environ["DB_HOST_PARAMETER_NAME"]),
        port=int(get_parameter(os.environ["DB_PORT_PARAMETER_NAME"])),
        user=get_parameter(os.environ["DB_USERNAME_PARAMETER_NAME"]),
        password=get_parameter(os.environ["DB_PASSWORD_PARAMETER_NAME"]),
        database=get_parameter(os.environ["DB_NAME_PARAMETER_NAME"]),
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=20,
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


def safe_page(value):
    try:
        page = int(value)
    except (TypeError, ValueError):
        page = 1
    return max(page, 1)


def pagination(page, total, page_size=PAGE_SIZE):
    total_pages = max((total + page_size - 1) // page_size, 1)
    page = min(max(page, 1), total_pages)
    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
        "offset": (page - 1) * page_size,
        "has_previous": page > 1,
        "has_next": page < total_pages,
    }


def page_window(current, total_pages):
    """Small pagination window so large datasets do not create huge controls."""
    if total_pages <= 7:
        return list(range(1, total_pages + 1))
    if current <= 4:
        return [1, 2, 3, 4, 5, None, total_pages]
    if current >= total_pages - 3:
        return [1, None, total_pages - 4, total_pages - 3, total_pages - 2, total_pages - 1, total_pages]
    return [1, None, current - 1, current, current + 1, None, total_pages]


def query_value(name, default=""):
    return request.args.get(name, default).strip()


def load_overview_data():
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
                LIMIT 10
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
    return products, orders, low_stock_products, metrics, order_statuses


def load_products(page, search=""):
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            where = ["p.status = 'ACTIVE'"]
            params = []
            if search:
                where.append("(p.name LIKE %s OR c.category_name LIKE %s OR CAST(p.product_id AS CHAR) LIKE %s)")
                like = f"%{search}%"
                params.extend([like, like, like])
            where_sql = " AND ".join(where)

            cursor.execute(
                f"SELECT COUNT(*) AS total FROM products p JOIN categories c ON c.category_id = p.category_id WHERE {where_sql}",
                params,
            )
            total = int(cursor.fetchone()["total"] or 0)
            pager = pagination(page, total)

            cursor.execute(
                f"""
                SELECT p.product_id, p.name, c.category_name, p.price,
                       p.stock_quantity, p.low_stock_threshold, p.status,
                       p.updated_at
                FROM products p
                JOIN categories c ON c.category_id = p.category_id
                WHERE {where_sql}
                ORDER BY p.product_id
                LIMIT %s OFFSET %s
                """,
                params + [pager["page_size"], pager["offset"]],
            )
            rows = cursor.fetchall()
    finally:
        connection.close()
    pager["window"] = page_window(pager["page"], pager["total_pages"])
    return rows, pager


def load_customers(page, search=""):
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            where = []
            params = []
            if search:
                where.append("(c.customer_name LIKE %s OR c.customer_email LIKE %s OR CAST(c.customer_id AS CHAR) LIKE %s)")
                like = f"%{search}%"
                params.extend([like, like, like])
            where_sql = f"WHERE {' AND '.join(where)}" if where else ""

            cursor.execute(
                f"SELECT COUNT(*) AS total FROM customers c {where_sql}",
                params,
            )
            total = int(cursor.fetchone()["total"] or 0)
            pager = pagination(page, total)

            cursor.execute(
                f"""
                SELECT c.customer_id, c.customer_name, c.customer_email,
                       c.created_at, COUNT(o.order_id) AS order_count
                FROM customers c
                LEFT JOIN orders o ON o.customer_id = c.customer_id
                {where_sql}
                GROUP BY c.customer_id, c.customer_name, c.customer_email, c.created_at
                ORDER BY c.customer_id
                LIMIT %s OFFSET %s
                """,
                params + [pager["page_size"], pager["offset"]],
            )
            rows = cursor.fetchall()
    finally:
        connection.close()
    pager["window"] = page_window(pager["page"], pager["total_pages"])
    return rows, pager


def load_orders(page, search="", status=""):
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            where = []
            params = []
            if search:
                where.append("(CAST(o.order_id AS CHAR) LIKE %s OR c.customer_name LIKE %s OR c.customer_email LIKE %s)")
                like = f"%{search}%"
                params.extend([like, like, like])
            if status:
                where.append("o.status = %s")
                params.append(status)
            where_sql = f"WHERE {' AND '.join(where)}" if where else ""

            cursor.execute(
                f"""
                SELECT COUNT(*) AS total
                FROM orders o
                JOIN customers c ON c.customer_id = o.customer_id
                {where_sql}
                """,
                params,
            )
            total = int(cursor.fetchone()["total"] or 0)
            pager = pagination(page, total)

            cursor.execute(
                f"""
                SELECT o.order_id, c.customer_name, o.status,
                       o.total_amount, o.created_at,
                       COUNT(oi.order_item_id) AS item_count
                FROM orders o
                JOIN customers c ON c.customer_id = o.customer_id
                LEFT JOIN order_items oi ON oi.order_id = o.order_id
                {where_sql}
                GROUP BY o.order_id, c.customer_name, o.status,
                         o.total_amount, o.created_at
                ORDER BY o.created_at DESC
                LIMIT %s OFFSET %s
                """,
                params + [pager["page_size"], pager["offset"]],
            )
            rows = cursor.fetchall()

            cursor.execute("SELECT DISTINCT status FROM orders ORDER BY status")
            statuses = [row["status"] for row in cursor.fetchall()]
    finally:
        connection.close()
    pager["window"] = page_window(pager["page"], pager["total_pages"])
    return rows, pager, statuses


def list_report_objects():
    bucket = os.environ["REPORT_BUCKET_NAME"]
    prefix = os.environ.get("REPORT_PREFIX", "reports").rstrip("/") + "/"
    paginator = s3.get_paginator("list_objects_v2")
    reports = []

    for result in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for item in result.get("Contents", []):
            key = item["Key"]
            if not key.endswith(".csv"):
                continue

            match = REPORT_KEY_PATTERN.search(key)
            if match:
                try:
                    report_date = datetime.strptime(match.group(1), "%Y%m%d").date()
                    report_time = datetime.strptime(match.group(2), "%H%M%S").time()
                    generated_at = datetime.combine(report_date, report_time, tzinfo=timezone.utc)
                except ValueError:
                    report_date = item["LastModified"].date()
                    generated_at = item["LastModified"]
            else:
                report_date = item["LastModified"].date()
                generated_at = item["LastModified"]

            reports.append(
                {
                    "key": key,
                    "date": report_date,
                    "date_text": report_date.isoformat(),
                    "generated_at": generated_at,
                    "generated_text": generated_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                    "size_kb": max(1, round(item.get("Size", 0) / 1024)),
                }
            )

    reports.sort(key=lambda item: item["generated_at"], reverse=True)
    return reports


def report_download(report):
    return s3.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": os.environ["REPORT_BUCKET_NAME"],
            "Key": report["key"],
        },
        ExpiresIn=3600,
    )


def selected_report(reports, selected_date):
    if not selected_date:
        return reports[0] if reports else None
    matches = [report for report in reports if report["date"] == selected_date]
    return matches[0] if matches else None


@app.get("/")
def home():
    return redirect(url_for("login"))


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
    products, orders, low_stock_products, metrics, order_statuses = load_overview_data()
    return render_template(
        "dashboard.html",
        page="overview",
        products=products,
        orders=orders,
        low_stock_count=len(low_stock_products),
        low_stock_products=low_stock_products,
        metrics=metrics,
        order_statuses=order_statuses,
    )


@app.get("/dashboard/products")
@login_required
def products_page():
    page = safe_page(request.args.get("page"))
    search = query_value("q")
    products, pager = load_products(page, search)
    return render_template(
        "dashboard.html",
        page="products",
        products=products,
        pager=pager,
        search=search,
    )


@app.get("/dashboard/customers")
@login_required
def customers_page():
    page = safe_page(request.args.get("page"))
    search = query_value("q")
    customers, pager = load_customers(page, search)
    return render_template(
        "dashboard.html",
        page="customers",
        customers=customers,
        pager=pager,
        search=search,
    )


@app.get("/dashboard/inventory")
@login_required
def inventory_page():
    page = safe_page(request.args.get("page"))
    search = query_value("q")
    products, pager = load_products(page, search)
    return render_template(
        "dashboard.html",
        page="inventory",
        products=products,
        pager=pager,
        search=search,
    )


@app.get("/dashboard/orders")
@login_required
def orders_page():
    page = safe_page(request.args.get("page"))
    search = query_value("q")
    status = query_value("status")
    orders, pager, statuses = load_orders(page, search, status)
    return render_template(
        "dashboard.html",
        page="orders",
        orders=orders,
        pager=pager,
        search=search,
        status=status,
        statuses=statuses,
    )


@app.get("/dashboard/reports")
@login_required
def reports_page():
    requested = query_value("date")
    selected_date = None
    date_error = None

    if requested:
        try:
            selected_date = date.fromisoformat(requested)
        except ValueError:
            date_error = "Please select a valid report date."

    reports = list_report_objects()
    if selected_date is None and reports:
        selected_date = reports[0]["date"]

    chosen = selected_report(reports, selected_date)
    if chosen:
        chosen = dict(chosen)
        chosen["url"] = report_download(chosen)

    page = safe_page(request.args.get("page"))
    total = len(reports)
    pager = pagination(page, total)
    reports_page_rows = reports[pager["offset"] : pager["offset"] + pager["page_size"]]
    for report in reports_page_rows:
        report["url"] = report_download(report)
    pager["window"] = page_window(pager["page"], pager["total_pages"])

    return render_template(
        "dashboard.html",
        page="reports",
        reports=reports_page_rows,
        chosen_report=chosen,
        selected_date=selected_date.isoformat() if selected_date else "",
        date_error=date_error,
        pager=pager,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "80"))
    app.run(host="0.0.0.0", port=port)
