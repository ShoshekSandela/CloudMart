import hashlib
import json
import logging
import os
import secrets

import pymysql
import boto3

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



def _subscription_filter_policy(customer_id):
    return json.dumps({"customer_id": [str(customer_id)]})


def _list_sns_subscriptions():
    subscriptions = []
    token = None
    while True:
        kwargs = {"TopicArn": ORDER_NOTIFICATION_TOPIC_ARN}
        if token:
            kwargs["NextToken"] = token
        page = sns.list_subscriptions_by_topic(**kwargs)
        subscriptions.extend(page.get("Subscriptions", []))
        token = page.get("NextToken")
        if not token:
            return subscriptions


def _is_valid_subscription_arn(subscription_arn):
    """Return True only for a real SNS subscription ARN."""
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


def _get_filter_policy(subscription_arn):
    attrs = _get_subscription_attributes(subscription_arn)
    raw = attrs.get("FilterPolicy")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid SNS filter policy: subscription_arn=%s",
            subscription_arn,
        )
        return {}


def _subscription_is_confirmed(subscription):
    arn = subscription.get("SubscriptionArn")
    if not _is_valid_subscription_arn(arn):
        return False
    attrs = _get_subscription_attributes(arn)
    if not attrs:
        return False
    if attrs.get("TopicArn") and attrs.get("TopicArn") != ORDER_NOTIFICATION_TOPIC_ARN:
        return False
    return str(attrs.get("PendingConfirmation", "false")).lower() != "true"


def _set_customer_subscription_filter(subscription_arn, customer_id):
    sns.set_subscription_attributes(
        SubscriptionArn=subscription_arn,
        AttributeName="FilterPolicy",
        AttributeValue=_subscription_filter_policy(customer_id),
    )
    sns.set_subscription_attributes(
        SubscriptionArn=subscription_arn,
        AttributeName="FilterPolicyScope",
        AttributeValue="MessageAttributes",
    )


def ensure_customer_sns_subscription(customer_id, email):
    """Ensure one confirmed/pending SNS email subscription for a customer.

    This function is intentionally non-destructive. It never calls
    Unsubscribe. The only code path allowed to remove an SNS subscription is
    the explicit customer unsubscribe API.
    """
    email = str(email).strip().lower()
    subscriptions = _list_sns_subscriptions()

    matching_pending = False
    matching_confirmed = []

    for sub in subscriptions:
        endpoint = str(sub.get("Endpoint") or "").strip().lower()
        if endpoint != email:
            continue

        arn = sub.get("SubscriptionArn")
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

        matching_confirmed.append(sub)

    # Reuse an existing confirmed subscription whenever possible.
    for sub in matching_confirmed:
        arn = sub["SubscriptionArn"]
        policy = _get_filter_policy(arn)
        ids = policy.get("customer_id", []) if isinstance(policy, dict) else []
        if str(customer_id) in [str(value) for value in ids]:
            return "CONFIRMED"

    # A confirmed endpoint subscription can be safely associated with this
    # customer without creating another subscription.
    if matching_confirmed:
        arn = matching_confirmed[0]["SubscriptionArn"]
        _set_customer_subscription_filter(arn, customer_id)
        logger.info(
            "Customer SNS subscription reused: customer_id=%s subscription_arn=%s",
            customer_id,
            arn,
        )
        return "CONFIRMED"

    if matching_pending:
        logger.info(
            "Customer SNS subscription pending confirmation: customer_id=%s",
            customer_id,
        )
        return "PENDING_CONFIRMATION"

    # No subscription exists for this endpoint, so create exactly one.
    result = sns.subscribe(
        TopicArn=ORDER_NOTIFICATION_TOPIC_ARN,
        Protocol="email",
        Endpoint=email,
        ReturnSubscriptionArn=True,
    )
    arn = result.get("SubscriptionArn") or "PendingConfirmation"

    if _is_valid_subscription_arn(arn):
        attrs = _get_subscription_attributes(arn)
        if attrs is None or str(attrs.get("PendingConfirmation", "false")).lower() == "true":
            logger.info(
                "Customer SNS subscription created but not yet confirmed/available: customer_id=%s subscription_arn=%s",
                customer_id,
                arn,
            )
            return "PENDING_CONFIRMATION"

        if attrs.get("TopicArn") and attrs.get("TopicArn") != ORDER_NOTIFICATION_TOPIC_ARN:
            logger.error(
                "SNS subscription belongs to a different topic: customer_id=%s subscription_arn=%s topic=%s",
                customer_id,
                arn,
                attrs.get("TopicArn"),
            )
            return "PENDING_CONFIRMATION"

        _set_customer_subscription_filter(arn, customer_id)
        logger.info(
            "Customer SNS subscription created and confirmed: customer_id=%s subscription_arn=%s",
            customer_id,
            arn,
        )
        return "CONFIRMED"

    logger.info(
        "Customer SNS subscription created and awaiting confirmation: customer_id=%s state=%s",
        customer_id,
        arn,
    )
    return "PENDING_CONFIRMATION"


def remove_customer_sns_subscription(customer_id, email, reason="EXPLICIT_UNSUBSCRIBE"):
    """Remove an SNS subscription only for an explicit unsubscribe action."""
    email = str(email or "").strip().lower()
    if not email:
        return False

    for sub in _list_sns_subscriptions():
        if str(sub.get("Endpoint") or "").strip().lower() != email:
            continue

        arn = sub.get("SubscriptionArn")
        if not _is_valid_subscription_arn(arn):
            continue

        if not _subscription_is_confirmed(sub):
            continue

        policy = _get_filter_policy(arn)
        ids = policy.get("customer_id", []) if isinstance(policy, dict) else []
        if str(customer_id) not in [str(value) for value in ids]:
            continue

        # This is the only application path that intentionally deletes an
        # SNS subscription. It is reached only by the explicit unsubscribe
        # endpoint below.
        sns.unsubscribe(SubscriptionArn=arn)
        logger.info(
            "Customer SNS subscription removed: customer_id=%s reason=%s subscription_arn=%s",
            customer_id,
            reason,
            arn,
        )
        return True

    return False


def _get_customer_email_subscription_status(connection, customer_id):
    """Return the application-level subscription state for a customer."""
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT status
            FROM email_subscriptions
            WHERE customer_id = %s
            LIMIT 1
        """, (customer_id,))
        row = cursor.fetchone()
    return str(row["status"]).upper() if row else None


def _update_customer_email_subscription_status(connection, customer_id, status):
    """Update notification state without ever reactivating an unsubscribed customer."""
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


def sync_customer_sns_subscription(connection, customer_id, old_email, new_email):
    try:
        old_normalized = str(old_email or "").strip().lower()
        new_normalized = str(new_email or "").strip().lower()

        # An explicit unsubscribe is an application-level choice. A customer
        # email update must not silently opt that customer back in.
        current_status = _get_customer_email_subscription_status(connection, customer_id)
        if current_status == "UNSUBSCRIBED":
            logger.info(
                "Skipping SNS subscription synchronization for unsubscribed customer: customer_id=%s",
                customer_id,
            )
            return "UNSUBSCRIBED"

        # Always establish the new endpoint first. If confirmation is still
        # pending, keep the old confirmed subscription intact so an email
        # change can never interrupt an already-working notification path.
        state = ensure_customer_sns_subscription(customer_id, new_normalized)

        if (
            old_normalized
            and old_normalized != new_normalized
            and state == "CONFIRMED"
        ):
            remove_customer_sns_subscription(
                customer_id,
                old_normalized,
                reason="CUSTOMER_EMAIL_CHANGED_AFTER_NEW_SUBSCRIPTION_CONFIRMED",
            )

        # Keep the DB state aligned with SNS reconciliation. The explicit
        # unsubscribe state is protected by the WHERE clause in the helper.
        if state == "CONFIRMED":
            _update_customer_email_subscription_status(connection, customer_id, "ACTIVE")
        elif state == "PENDING_CONFIRMATION":
            _update_customer_email_subscription_status(
                connection, customer_id, "PENDING_CONFIRMATION"
            )

        logger.info(
            "Customer SNS notification subscription synchronized: customer_id=%s state=%s",
            customer_id,
            state,
        )
        return state
    except Exception:
        # Customer data changes should not be rolled back because an external
        # SNS subscription operation failed. Notification Lambda retries the
        # subscription reconciliation when the next order event arrives.
        logger.exception(
            "Customer SNS subscription synchronization failed: customer_id=%s",
            customer_id,
        )
        return "SYNC_FAILED"




def json_serializer(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
        },
        "body": json.dumps(body, default=json_serializer),
    }


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


def ensure_subscription_table(connection):
    with connection.cursor() as cursor:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS email_subscriptions (
                subscription_id BIGINT NOT NULL AUTO_INCREMENT,
                customer_id BIGINT NOT NULL,
                email VARCHAR(255) NOT NULL,
                status VARCHAR(30) NOT NULL DEFAULT 'ACTIVE',
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP,
                unsubscribed_at DATETIME NULL,
                PRIMARY KEY (subscription_id),
                UNIQUE KEY uk_email_subscriptions_customer_id (customer_id),
                INDEX idx_email_subscriptions_status (status),
                CONSTRAINT fk_email_subscriptions_customer
                    FOREIGN KEY (customer_id)
                    REFERENCES customers(customer_id)
                    ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
    connection.commit()


def get_authorization_context(event):
    authorizer = (event.get("requestContext") or {}).get("authorizer") or {}
    role = str(authorizer.get("role") or authorizer.get("Role") or "").upper()
    customer_id = authorizer.get("customer_id")
    if customer_id not in (None, ""):
        try:
            customer_id = int(customer_id)
        except (TypeError, ValueError):
            customer_id = None
    return {"role": role, "customer_id": customer_id, "email": authorizer.get("email")}


def require_admin(auth):
    if auth["role"] != "ADMIN":
        raise PermissionError("ADMIN role is required for this operation")


def get_customer(cursor, customer_id):
    cursor.execute("""
        SELECT customer_id, customer_name, customer_email, created_at
        FROM customers
        WHERE customer_id = %s
    """, (customer_id,))
    return cursor.fetchone()


def get_customers(cursor):
    cursor.execute("""
        SELECT customer_id, customer_name, customer_email, created_at
        FROM customers
        ORDER BY customer_id
    """)
    return cursor.fetchall()


def create_customer(cursor, payload):
    customer_name = str(payload.get("customer_name", payload.get("name")) or "").strip()
    customer_email = str(payload.get("customer_email", payload.get("email")) or "").strip().lower()

    if not customer_name:
        raise ValueError("customer_name is required")
    if not customer_email or "@" not in customer_email:
        raise ValueError("customer_email is required and must be valid")

    cursor.execute(
        "INSERT INTO customers (customer_name, customer_email) VALUES (%s, %s)",
        (customer_name, customer_email),
    )
    customer_id = int(cursor.lastrowid)

    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    cursor.execute("""
        INSERT INTO customer_tokens (customer_id, token_hash, status)
        VALUES (%s, %s, 'ACTIVE')
    """, (customer_id, token_hash))

    cursor.execute("""
        INSERT INTO email_subscriptions (customer_id, email, status)
        VALUES (%s, %s, 'ACTIVE')
    """, (customer_id, customer_email))

    return customer_id, token


def update_customer(cursor, customer_id, payload):
    current = get_customer(cursor, customer_id)
    if not current:
        return None

    fields = []
    values = []
    customer_name = payload.get("customer_name", payload.get("name"))
    customer_email = payload.get("customer_email", payload.get("email"))

    if customer_name is not None:
        customer_name = str(customer_name).strip()
        if not customer_name:
            raise ValueError("customer_name cannot be empty")
        fields.append("customer_name = %s")
        values.append(customer_name)

    if customer_email is not None:
        customer_email = str(customer_email).strip().lower()
        if not customer_email or "@" not in customer_email:
            raise ValueError("customer_email cannot be empty")
        fields.append("customer_email = %s")
        values.append(customer_email)

    if not fields:
        raise ValueError("No fields supplied for update")

    values.append(customer_id)
    cursor.execute(
        f"UPDATE customers SET {', '.join(fields)} WHERE customer_id = %s",
        values,
    )

    if customer_email is not None:
        cursor.execute("""
            UPDATE orders SET customer_email = %s WHERE customer_id = %s
        """, (customer_email, customer_id))
        # Changing an address is not an unsubscribe action. Keep the
        # existing subscription state and only synchronize its endpoint.
        cursor.execute("""
            UPDATE email_subscriptions
            SET email = %s
            WHERE customer_id = %s
        """, (customer_email, customer_id))
        logger.info("Customer email synchronized without changing subscription status")

    return get_customer(cursor, customer_id)


def delete_customer(cursor, customer_id):
    current = get_customer(cursor, customer_id)
    if not current:
        return None

    cursor.execute(
        "SELECT COUNT(*) AS order_count FROM orders WHERE customer_id = %s",
        (customer_id,),
    )
    if int(cursor.fetchone()["order_count"]) > 0:
        raise ValueError(f"Customer {customer_id} cannot be deleted because orders exist")

    # Customer deletion is a customer lifecycle operation, not a notification
    # failure. The FK cascade removes its subscription and token records.
    cursor.execute("DELETE FROM customers WHERE customer_id = %s", (customer_id,))
    return current


def unsubscribe_customer(connection, customer_id, actor):
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT subscription_id, status
            FROM email_subscriptions
            WHERE customer_id = %s
            FOR UPDATE
        """, (customer_id,))
        subscription = cursor.fetchone()

        if not subscription:
            cursor.execute("""
                SELECT customer_email FROM customers WHERE customer_id = %s
            """, (customer_id,))
            customer = cursor.fetchone()
            if not customer:
                return False, "CUSTOMER_NOT_FOUND"
            cursor.execute("""
                INSERT INTO email_subscriptions (customer_id, email, status, unsubscribed_at)
                VALUES (%s, %s, 'UNSUBSCRIBED', CURRENT_TIMESTAMP)
            """, (customer_id, customer["customer_email"]))
        elif str(subscription["status"]).upper() != "UNSUBSCRIBED":
            cursor.execute("""
                UPDATE email_subscriptions
                SET status = 'UNSUBSCRIBED',
                    unsubscribed_at = CURRENT_TIMESTAMP
                WHERE customer_id = %s
            """, (customer_id,))

    connection.commit()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT customer_email FROM customers WHERE customer_id = %s",
                (customer_id,),
            )
            current_customer = cursor.fetchone()
        if current_customer:
            remove_customer_sns_subscription(
                customer_id,
                current_customer["customer_email"],
                reason="EXPLICIT_UNSUBSCRIBE",
            )
    except Exception:
        logger.exception(
            "SNS unsubscribe synchronization failed: customer_id=%s",
            customer_id,
        )

    logger.info(
        "Email subscription changed: reason=USER_UNSUBSCRIBE customer_id=%s actor=%s",
        customer_id,
        actor,
    )
    return True, "UNSUBSCRIBED"


def lambda_handler(event, context):
    connection = None
    try:
        method = str(
            event.get("httpMethod")
            or ((event.get("requestContext") or {}).get("http", {}) or {}).get("method")
            or ""
        ).upper()
        if method == "OPTIONS":
            return response(200, {"message": "OK"})

        auth = get_authorization_context(event)
        path_parameters = event.get("pathParameters") or {}
        path = str(event.get("path") or "")
        customer_id = path_parameters.get("id") or path_parameters.get("customer_id")
        if customer_id is not None:
            try:
                customer_id = int(customer_id)
            except (TypeError, ValueError):
                raise ValueError("customer id must be an integer")

        raw_body = event.get("body")
        payload = json.loads(raw_body) if isinstance(raw_body, str) and raw_body else (
            raw_body if isinstance(raw_body, dict) else {}
        )

        connection = get_db_connection()
        ensure_subscription_table(connection)

        with connection.cursor() as cursor:
            if path.endswith("/unsubscribe"):
                if customer_id is None:
                    raise ValueError("customer id is required")
                if auth["role"] == "CUSTOMER" and auth["customer_id"] != customer_id:
                    raise PermissionError("Customers can unsubscribe only their own email")
                if auth["role"] != "ADMIN" and auth["role"] != "CUSTOMER":
                    raise PermissionError("Valid CUSTOMER or ADMIN role is required")
                _, status = unsubscribe_customer(
                    connection,
                    customer_id,
                    "customer" if auth["role"] == "CUSTOMER" else "admin",
                )
                return response(200, {
                    "message": "Email notifications unsubscribed",
                    "customer_id": customer_id,
                    "subscription_status": status,
                })

            if customer_id is None:
                if method == "GET":
                    require_admin(auth)
                    customers = get_customers(cursor)
                    return response(200, {"count": len(customers), "customers": customers})
                if method == "POST":
                    # Public customer registration:
                    # no admin/customer token is required here.
                    # A new customer token is generated by create_customer().
                    created_id, token = create_customer(cursor, payload)
                    connection.commit()
                    customer = get_customer(cursor, created_id)
                    subscription_state = sync_customer_sns_subscription(
                        connection,
                        created_id,
                        None,
                        customer["customer_email"],
                    )
                    customer["notification_subscription"] = subscription_state
                    customer["token"] = token
                    return response(201, customer)
                return response(405, {"message": "Method not allowed"})

            if method == "GET":
                if auth["role"] == "CUSTOMER" and auth["customer_id"] != customer_id:
                    raise PermissionError("Customers can access only their own customer record")
                if auth["role"] != "ADMIN" and auth["role"] != "CUSTOMER":
                    raise PermissionError("Valid CUSTOMER or ADMIN role is required")
                customer = get_customer(cursor, customer_id)
                if not customer:
                    return response(404, {"message": "Customer not found"})
                return response(200, customer)

            if method == "PUT":
                require_admin(auth)
                existing = get_customer(cursor, customer_id)
                if not existing:
                    connection.rollback()
                    return response(404, {"message": "Customer not found"})
                old_email = existing["customer_email"]

                customer = update_customer(cursor, customer_id, payload)
                if not customer:
                    connection.rollback()
                    return response(404, {"message": "Customer not found"})

                connection.commit()
                subscription_state = sync_customer_sns_subscription(
                    connection,
                    customer_id,
                    old_email,
                    customer["customer_email"],
                )
                customer["notification_subscription"] = subscription_state
                return response(200, customer)

            if method == "DELETE":
                require_admin(auth)
                customer = delete_customer(cursor, customer_id)
                if not customer:
                    connection.rollback()
                    return response(404, {"message": "Customer not found"})
                connection.commit()
                return response(200, {"message": "Customer deleted successfully", "customer_id": customer_id})

            return response(405, {"message": "Method not allowed"})

    except json.JSONDecodeError:
        if connection:
            connection.rollback()
        return response(400, {"message": "Invalid JSON body"})
    except PermissionError as exc:
        if connection:
            connection.rollback()
        logger.warning("Customer authorization denied: %s", exc)
        return response(403, {"message": str(exc)})
    except ValueError as exc:
        if connection:
            connection.rollback()
        return response(400, {"message": str(exc)})
    except pymysql.MySQLError as exc:
        if connection:
            connection.rollback()
        logger.exception("Customer database operation failed")
        return response(500, {"message": "Database operation failed"})
    except Exception:
        if connection:
            connection.rollback()
        logger.exception("Customer Lambda failed")
        return response(500, {"message": "Internal server error"})
    finally:
        if connection:
            connection.close()


# Explicit deployment marker/entry point for the public customer-registration version.
def lambda_handler_v2(event, context):
    return lambda_handler(event, context)
