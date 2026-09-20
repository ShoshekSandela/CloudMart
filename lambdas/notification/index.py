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
sns = boto3.client("sns")

DB_HOST_PARAMETER_NAME = os.environ["DB_HOST_PARAMETER_NAME"]
DB_PORT_PARAMETER_NAME = os.environ["DB_PORT_PARAMETER_NAME"]
DB_NAME_PARAMETER_NAME = os.environ["DB_NAME_PARAMETER_NAME"]
DB_USERNAME_PARAMETER_NAME = os.environ["DB_USERNAME_PARAMETER_NAME"]
DB_PASSWORD_PARAMETER_NAME = os.environ["DB_PASSWORD_PARAMETER_NAME"]
ORDER_NOTIFICATION_TOPIC_ARN = os.environ["ORDER_NOTIFICATION_TOPIC_ARN"]


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
        cursor.execute(
            """
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
            """,
            (customer_id,),
        )
        customer = cursor.fetchone()

        if not customer:
            return None

        if customer["subscription_id"] is None:
            cursor.execute(
                """
                INSERT INTO email_subscriptions (customer_id, email, status)
                VALUES (%s, %s, 'ACTIVE')
                """,
                (customer_id, customer["customer_email"]),
            )
            connection.commit()
            customer["notification_email"] = customer["customer_email"]
            customer["subscription_status"] = "ACTIVE"

        return customer


def render_items(items):
    rows = []
    for item in items or []:
        name = html.unescape(safe(item.get("product_name"), f"Product {item.get('product_id', '')}"))
        quantity = safe(item.get("quantity"), "0")
        unit_price = money(item.get("unit_price"))
        subtotal = money(item.get("subtotal"))
        rows.append(
            f"{name} | Qty: {quantity} | Unit Price: ₹{unit_price} | Subtotal: ₹{subtotal}"
        )
    return "\n".join(rows) if rows else "No item details were provided."


def build_message(detail_type, detail):
    customer_name = safe(detail.get("customer_name"), "Customer")
    order_id = safe(detail.get("order_id"), "N/A")
    status = order_status(detail_type, detail)
    order_date = safe(detail.get("order_date") or detail.get("created_at"))
    if not order_date:
        order_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    total = money(detail.get("total_amount"))
    reason = safe(detail.get("failure_reason"))

    intro = {
        "OrderPlaced": "Your order has been successfully placed.",
        "OrderConfirmed": "Your order has been confirmed and inventory has been reserved.",
        "OrderCanceled": "Your order has been canceled.",
        "OrderFailed": "We were unable to complete your order.",
        "OrderCompleted": "Your order has been completed successfully.",
    }.get(detail_type, "There is an update regarding your order.")

    subject = f"CloudMart Order {status.title()} - #{order_id}"

    message = f"""CloudMart Order Notification

Hello {customer_name},

{intro}

Order Details
-------------
Order ID: {order_id}
Order Status: {status.title()}
Customer: {customer_name}
Order Date: {order_date}
Total Amount: ₹{total}

Items
-----
{render_items(detail.get("items"))}
"""

    if reason:
        message += f"\nReason: {reason}\n"

    message += "\nThank you,\nCloudMart\n"
    return subject[:100], message


def _is_valid_subscription_arn(subscription_arn):
    """Return True only for a real SNS subscription ARN.

    ListSubscriptionsByTopic can expose non-ARN lifecycle values such as
    PendingConfirmation (and, during deletion/reconciliation, Deleted).
    Those values must never be sent to GetSubscriptionAttributes or
    SetSubscriptionAttributes.
    """
    if not subscription_arn:
        return False

    parts = str(subscription_arn).split(":")
    return (
        len(parts) >= 7
        and parts[0] == "arn"
        and parts[2] == "sns"
        and bool(parts[3])
        and bool(parts[4])
        and bool(parts[5])
        and bool(parts[6])
    )


def _get_subscription_attributes(subscription_arn):
    if not _is_valid_subscription_arn(subscription_arn):
        logger.warning(
            "Skipping SNS subscription attribute lookup for non-ARN state: %s",
            subscription_arn,
        )
        return {}

    try:
        return sns.get_subscription_attributes(
            SubscriptionArn=subscription_arn
        ).get("Attributes", {})
    except Exception:
        logger.warning(
            "Unable to read SNS subscription attributes; treating subscription as stale: subscription_arn=%s",
            subscription_arn,
            exc_info=True,
        )
        return None


def _set_customer_subscription_filter(subscription_arn, customer_id):
    filter_policy = {"customer_id": [str(customer_id)]}
    sns.set_subscription_attributes(
        SubscriptionArn=subscription_arn,
        AttributeName="FilterPolicy",
        AttributeValue=json.dumps(filter_policy),
    )
    sns.set_subscription_attributes(
        SubscriptionArn=subscription_arn,
        AttributeName="FilterPolicyScope",
        AttributeValue="MessageAttributes",
    )


def ensure_sns_email_subscription(customer_id, email):
    """Ensure the customer's SNS email subscription exists.

    This reconciliation is deliberately non-destructive. It never calls
    Unsubscribe, so an order notification cannot delete a confirmed
    subscription. The explicit customer unsubscribe API owns deletion.
    """
    email = str(email).strip().lower()
    subscriptions = sns.list_subscriptions_by_topic(
        TopicArn=ORDER_NOTIFICATION_TOPIC_ARN
    ).get("Subscriptions", [])

    matching_confirmed = []
    matching_pending = False

    for subscription in subscriptions:
        endpoint = str(subscription.get("Endpoint") or "").strip().lower()
        if endpoint != email:
            continue

        arn = subscription.get("SubscriptionArn")
        if not _is_valid_subscription_arn(arn):
            state = str(arn or "").strip() or "UNKNOWN"
            if state.lower() == "pendingconfirmation":
                matching_pending = True
            else:
                logger.warning(
                    "Ignoring stale SNS subscription state: customer_id=%s email=%s state=%s",
                    customer_id,
                    email,
                    state,
                )
            continue

        attrs = _get_subscription_attributes(arn)
        if attrs is None:
            logger.warning(
                "Ignoring stale/unavailable SNS subscription: customer_id=%s subscription_arn=%s",
                customer_id,
                arn,
            )
            continue

        if attrs.get("TopicArn") and attrs.get("TopicArn") != ORDER_NOTIFICATION_TOPIC_ARN:
            logger.warning(
                "Ignoring SNS subscription from a different topic: customer_id=%s subscription_arn=%s topic=%s",
                customer_id,
                arn,
                attrs.get("TopicArn"),
            )
            continue

        if str(attrs.get("PendingConfirmation", "false")).lower() == "true":
            matching_pending = True
            continue

        matching_confirmed.append(subscription)

    # Reuse an existing confirmed subscription. This makes application
    # restarts and concurrent notification retries non-destructive.
    if matching_confirmed:
        arn = matching_confirmed[0]["SubscriptionArn"]
        _set_customer_subscription_filter(arn, customer_id)
        logger.info(
            "Customer SNS subscription ready: customer_id=%s subscription_arn=%s",
            customer_id,
            arn,
        )
        return arn

    if matching_pending:
        logger.info(
            "Customer SNS subscription pending confirmation: customer_id=%s",
            customer_id,
        )
        return "PendingConfirmation"

    response = sns.subscribe(
        TopicArn=ORDER_NOTIFICATION_TOPIC_ARN,
        Protocol="email",
        Endpoint=email,
        ReturnSubscriptionArn=True,
    )
    arn = response.get("SubscriptionArn") or "PendingConfirmation"

    # ReturnSubscriptionArn=True returns the subscription ARN even when the
    # email endpoint is still awaiting confirmation. Check the actual SNS
    # state before applying a filter or publishing a notification.
    if _is_valid_subscription_arn(arn):
        attrs = _get_subscription_attributes(arn)
        if attrs is None or str(attrs.get("PendingConfirmation", "false")).lower() == "true":
            logger.info(
                "Customer SNS subscription created but not yet confirmed/available: customer_id=%s subscription_arn=%s",
                customer_id,
                arn,
            )
            return "PendingConfirmation"

        if attrs.get("TopicArn") and attrs.get("TopicArn") != ORDER_NOTIFICATION_TOPIC_ARN:
            logger.error(
                "SNS subscription belongs to a different topic: customer_id=%s subscription_arn=%s topic=%s",
                customer_id,
                arn,
                attrs.get("TopicArn"),
            )
            return "PendingConfirmation"

        _set_customer_subscription_filter(arn, customer_id)
        logger.info(
            "Customer SNS subscription ready: customer_id=%s subscription_arn=%s",
            customer_id,
            arn,
        )
        return arn

    logger.info(
        "Customer SNS subscription created and awaiting confirmation: customer_id=%s state=%s",
        customer_id,
        arn,
    )
    return "PendingConfirmation"

def _update_subscription_status(connection, customer_id, status):
    """Record SNS reconciliation state without reactivating an unsubscribed customer."""
    status = str(status).upper()
    with connection.cursor() as cursor:
        cursor.execute("""
            UPDATE email_subscriptions
            SET status = %s,
                unsubscribed_at = CASE
                    WHEN %s = 'UNSUBSCRIBED' THEN unsubscribed_at
                    ELSE NULL
                END
            WHERE customer_id = %s
              AND status <> 'UNSUBSCRIBED'
        """, (status, status, customer_id))
    connection.commit()


def publish_order_notification(connection, customer_id, email, subject, message):
    # Reconcile the subscription before publishing. This also supports
    # existing customers created before SNS notifications were enabled.
    subscription_state = ensure_sns_email_subscription(customer_id, email)

    # Keep the application state aligned with the actual SNS state. A deleted
    # or missing subscription is recreated automatically; the resulting
    # PendingConfirmation state prevents a publish until the recipient
    # confirms the new SNS email subscription.
    if subscription_state == "PendingConfirmation":
        _update_subscription_status(connection, customer_id, "PENDING_CONFIRMATION")

    else:
        _update_subscription_status(connection, customer_id, "ACTIVE")

    # SNS email subscriptions must be confirmed by the recipient before
    # messages can be delivered to the email endpoint.
    if subscription_state == "PendingConfirmation":
        logger.warning(
            "Order notification not delivered: customer_id=%s reason=SNS_SUBSCRIPTION_PENDING_CONFIRMATION",
            customer_id,
        )
        return None, subscription_state

    response = sns.publish(
        TopicArn=ORDER_NOTIFICATION_TOPIC_ARN,
        Subject=subject,
        Message=message,
        MessageAttributes={
            "customer_id": {
                "DataType": "String",
                "StringValue": str(customer_id),
            }
        },
    )

    logger.info(
        "Order notification published to SNS: customer_id=%s subscription_state=%s message_id=%s",
        customer_id,
        subscription_state,
        response.get("MessageId"),
    )

    return response, subscription_state


def process_notification_event(event, connection):
    detail = event.get("detail") or {}
    detail_type = str(event.get("detail-type") or "").strip()
    customer_id = detail.get("customer_id")

    logger.info(
        "Order notification event received: type=%s customer_id=%s order_id=%s",
        detail_type,
        customer_id,
        detail.get("order_id"),
    )

    if detail_type not in {
        "OrderPlaced",
        "OrderConfirmed",
        "OrderCanceled",
        "OrderFailed",
        "OrderCompleted",
    }:
        logger.info("Notification type ignored: %s", detail_type)
        return {"status": "IGNORED", "detail_type": detail_type}

    if customer_id in (None, ""):
        raise ValueError("customer_id is required")

    recipient = get_recipient(connection, int(customer_id))
    if not recipient:
        raise LookupError(f"Customer {customer_id} not found")

    if str(recipient["subscription_status"]).upper() == "UNSUBSCRIBED":
        logger.info(
            "Notification skipped: customer_id=%s reason=USER_UNSUBSCRIBE",
            customer_id,
        )
        return {"status": "UNSUBSCRIBED", "customer_id": int(customer_id)}

    email = recipient["notification_email"]
    if not email:
        raise ValueError(f"Customer {customer_id} has no notification email")

    subject, message = build_message(detail_type, detail)

    _, subscription_state = publish_order_notification(
        connection,
        int(customer_id),
        email,
        subject,
        message,
    )

    status = (
        "PENDING_CONFIRMATION"
        if subscription_state == "PendingConfirmation"
        else "PUBLISHED_TO_SNS"
    )

    return {
        "status": status,
        "customer_id": int(customer_id),
        "subscription_state": subscription_state,
    }


def lambda_handler(event, context):
    """Receive EventBridge order events and publish them to customer SNS."""
    connection = None
    try:
        connection = get_db_connection()
        return process_notification_event(event, connection)
    except Exception:
        logger.exception("SNS order notification failed")
        raise
    finally:
        if connection:
            connection.close()
