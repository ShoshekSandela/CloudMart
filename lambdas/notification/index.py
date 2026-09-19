import html
import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal

import boto3
import pymysql

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")
ses = boto3.client("ses")

DB_HOST_PARAMETER_NAME = os.environ["DB_HOST_PARAMETER_NAME"]
DB_PORT_PARAMETER_NAME = os.environ["DB_PORT_PARAMETER_NAME"]
DB_NAME_PARAMETER_NAME = os.environ["DB_NAME_PARAMETER_NAME"]
DB_USERNAME_PARAMETER_NAME = os.environ["DB_USERNAME_PARAMETER_NAME"]
DB_PASSWORD_PARAMETER_NAME = os.environ["DB_PASSWORD_PARAMETER_NAME"]
FROM_EMAIL = os.environ["NOTIFICATION_FROM_EMAIL"]


def get_parameter(name, decrypt=False):
    return ssm.get_parameter(Name=name, WithDecryption=decrypt)["Parameter"]["Value"].strip()


def get_db_connection():
    return pymysql.connect(
        host=get_parameter(DB_HOST_PARAMETER_NAME),
        port=int(get_parameter(DB_PORT_PARAMETER_NAME)),
        database=get_parameter(DB_NAME_PARAMETER_NAME),
        user=get_parameter(DB_USERNAME_PARAMETER_NAME, decrypt=True),
        password=get_parameter(DB_PASSWORD_PARAMETER_NAME, decrypt=True),
        connect_timeout=5,
        read_timeout=5,
        write_timeout=5,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def safe(value, default=""):
    return default if value is None else str(value)


def money(value):
    try:
        return f"{Decimal(str(value)):.2f}"
    except Exception:
        return "0.00"


def order_status(detail_type, detail):
    return safe(detail.get("status") or detail_type.replace("Order", "")).upper()


def get_recipient(connection, customer_id):
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT
                c.customer_id,
                c.customer_name,
                c.customer_email,
                es.subscription_id,
                COALESCE(es.email, c.customer_email) AS notification_email,
                COALESCE(es.status, 'ACTIVE') AS subscription_status
            FROM customers c
            LEFT JOIN email_subscriptions es
                ON es.customer_id = c.customer_id
            WHERE c.customer_id = %s
            LIMIT 1
        """, (customer_id,))
        customer = cursor.fetchone()

        if not customer:
            return None

        # Existing customers are active by default. A missing subscription
        # record is created as ACTIVE. An existing UNSUBSCRIBED record is
        # never changed by notification processing.
        if customer["subscription_id"] is None:
            cursor.execute("""
                INSERT INTO email_subscriptions (customer_id, email, status)
                VALUES (%s, %s, 'ACTIVE')
            """, (customer_id, customer["customer_email"]))
            connection.commit()
            customer["notification_email"] = customer["customer_email"]
            customer["subscription_status"] = "ACTIVE"

        return customer


def render_items(items):
    items = items or []
    rows = []
    text_rows = []
    for item in items:
        name = html.escape(safe(item.get("product_name"), f"Product {item.get('product_id', '')}"))
        quantity = safe(item.get("quantity"), "0")
        unit_price = money(item.get("unit_price"))
        subtotal = money(item.get("subtotal"))
        rows.append(
            f"<tr><td>{name}</td><td>{quantity}</td>"
            f"<td>₹{unit_price}</td><td>₹{subtotal}</td></tr>"
        )
        text_rows.append(
            f"{safe(item.get('product_name'), 'Product ' + safe(item.get('product_id'), ''))} | "
            f"Qty: {quantity} | Unit: ₹{unit_price} | Subtotal: ₹{subtotal}"
        )
    if not rows:
        rows.append("<tr><td colspan='4'>No item details were provided.</td></tr>")
        text_rows.append("No item details were provided.")
    return "".join(rows), "\n".join(text_rows)


def build_message(detail_type, detail):
    customer_name = safe(detail.get("customer_name"), "Customer")
    order_id = safe(detail.get("order_id"), "N/A")
    status = order_status(detail_type, detail)
    order_date = safe(detail.get("order_date") or detail.get("created_at"))
    if not order_date:
        order_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    total = money(detail.get("total_amount"))
    reason = safe(detail.get("failure_reason"))

    item_rows, item_text = render_items(detail.get("items"))
    greeting_name = html.escape(customer_name)
    safe_order_id = html.escape(order_id)
    safe_status = html.escape(status.title())
    safe_date = html.escape(order_date)
    reason_html = (
        f"<p><strong>Reason:</strong> {html.escape(reason)}</p>"
        if reason else ""
    )
    intro = {
        "OrderPlaced": "Your order has been successfully placed.",
        "OrderConfirmed": "Your order has been confirmed and inventory has been reserved.",
        "OrderCanceled": "Your order has been canceled.",
        "OrderFailed": "We were unable to complete your order.",
        "OrderCompleted": "Your order has been completed successfully.",
    }.get(detail_type, "There is an update regarding your order.")

    subject = f"Order {status.title()} - Order #{order_id}"

    html_body = f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>{html.escape(subject)}</title></head>
<body style="margin:0;padding:24px;background:#f4f6f8;font-family:Arial,sans-serif;color:#222;">
  <div style="max-width:720px;margin:auto;background:#fff;padding:32px;border-radius:8px;">
    <h2 style="margin-top:0;">CloudMart Order Notification</h2>
    <p>Hello {greeting_name},</p>
    <p>{html.escape(intro)}</p>
    <h3>Order Details</h3>
    <table style="width:100%;border-collapse:collapse;">
      <tr><td style="padding:8px;border:1px solid #ddd;"><strong>Order ID</strong></td>
          <td style="padding:8px;border:1px solid #ddd;">{safe_order_id}</td></tr>
      <tr><td style="padding:8px;border:1px solid #ddd;"><strong>Status</strong></td>
          <td style="padding:8px;border:1px solid #ddd;">{safe_status}</td></tr>
      <tr><td style="padding:8px;border:1px solid #ddd;"><strong>Customer</strong></td>
          <td style="padding:8px;border:1px solid #ddd;">{greeting_name}</td></tr>
      <tr><td style="padding:8px;border:1px solid #ddd;"><strong>Order Date</strong></td>
          <td style="padding:8px;border:1px solid #ddd;">{safe_date}</td></tr>
      <tr><td style="padding:8px;border:1px solid #ddd;"><strong>Total</strong></td>
          <td style="padding:8px;border:1px solid #ddd;">₹{html.escape(total)}</td></tr>
    </table>
    {reason_html}
    <h3>Items</h3>
    <table style="width:100%;border-collapse:collapse;">
      <thead><tr>
        <th style="text-align:left;padding:8px;border:1px solid #ddd;">Product</th>
        <th style="text-align:left;padding:8px;border:1px solid #ddd;">Quantity</th>
        <th style="text-align:left;padding:8px;border:1px solid #ddd;">Unit Price</th>
        <th style="text-align:left;padding:8px;border:1px solid #ddd;">Subtotal</th>
      </tr></thead>
      <tbody>{item_rows}</tbody>
    </table>
    <p style="margin-top:24px;">Thank you for shopping with CloudMart.</p>
    <p style="font-size:12px;color:#666;">
      You can unsubscribe from order email notifications through your authenticated
      CloudMart customer notification settings.
    </p>
  </div>
</body>
</html>"""

    text_body = f"""CloudMart Order Notification

Hello {customer_name},

{intro}

Order Details
------------
Order ID: {order_id}
Order Status: {status.title()}
Customer: {customer_name}
Order Date: {order_date}
Total Amount: ₹{total}
{f"Reason: {reason}" if reason else ""}

Items
-----
{item_text}

Thank you,
CloudMart

You can unsubscribe from order email notifications through your authenticated
CloudMart customer notification settings.
"""

    return subject, html_body, text_body


def lambda_handler(event, context):
    detail = event.get("detail") or {}
    detail_type = str(event.get("detail-type") or "").strip()
    customer_id = detail.get("customer_id")

    logger.info(
        "Order notification event received: type=%s customer_id=%s order_id=%s",
        detail_type, customer_id, detail.get("order_id"),
    )

    if detail_type not in {
        "OrderPlaced", "OrderConfirmed", "OrderCanceled",
        "OrderFailed", "OrderCompleted",
    }:
        logger.info("Notification type ignored: %s", detail_type)
        return {"statusCode": 200, "status": "IGNORED"}

    if customer_id in (None, ""):
        logger.error("Order notification missing customer_id")
        raise ValueError("customer_id is required")

    connection = None
    try:
        connection = get_db_connection()
        recipient = get_recipient(connection, int(customer_id))
        if not recipient:
            logger.error("Recipient lookup failed: customer_id=%s", customer_id)
            raise LookupError("Customer not found")

        if str(recipient["subscription_status"]).upper() == "UNSUBSCRIBED":
            logger.info(
                "Notification skipped: customer_id=%s reason=USER_UNSUBSCRIBE",
                customer_id,
            )
            return {"statusCode": 200, "status": "UNSUBSCRIBED"}

        email = recipient["notification_email"]
        subject, html_body, text_body = build_message(detail_type, detail)

        logger.info(
            "Order notification prepared: type=%s customer_id=%s template=%s recipient_present=%s",
            detail_type, customer_id, detail_type, bool(email),
        )
        logger.info("Email send attempted: type=%s customer_id=%s", detail_type, customer_id)

        result = ses.send_email(
            Source=FROM_EMAIL,
            Destination={"ToAddresses": [email]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {
                    "Html": {"Data": html_body, "Charset": "UTF-8"},
                    "Text": {"Data": text_body, "Charset": "UTF-8"},
                },
            },
        )

        logger.info(
            "Email send succeeded: type=%s customer_id=%s message_id=%s",
            detail_type, customer_id, result.get("MessageId"),
        )
        return {"statusCode": 200, "status": "SENT"}

    except Exception:
        logger.exception(
            "Email notification failed: type=%s customer_id=%s; "
            "subscription state is not modified",
            detail_type, customer_id,
        )
        # Do not update/delete/deactivate the subscription on delivery failure.
        raise
    finally:
        if connection:
            connection.close()
