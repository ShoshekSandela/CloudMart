import json
import logging
import os
import re
from decimal import Decimal
from datetime import date, datetime

import boto3
import pymysql


logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")
events = boto3.client("events")



# ============================================================
# AUTHENTICATION / AUTHORIZATION HELPERS
# ============================================================

def get_authorization_context(event):
    request_context = event.get("requestContext") or {}
    authorizer = request_context.get("authorizer") or {}

    role = str(
        authorizer.get("role") or ""
    ).upper().strip()

    email = authorizer.get("email")
    customer_id = authorizer.get("customer_id")

    if role not in {"ADMIN", "CUSTOMER"}:
        raise PermissionError(
            "Authenticated role is missing or invalid"
        )

    if email is not None:
        email = str(email).strip().lower() or None

    if customer_id not in (None, ""):
        try:
            customer_id = int(customer_id)
        except (TypeError, ValueError) as exc:
            raise PermissionError(
                "Authenticated customer_id is invalid"
            ) from exc

    return {
        "role": role,
        "email": email,
        "customer_id": customer_id,
    }


def require_admin(auth):
    if auth["role"] != "ADMIN":
        raise PermissionError(
            "ADMIN role is required"
        )


def resolve_customer_identity(
    connection,
    auth,
):
    """
    Resolve CUSTOMER identity from the authenticated token context.

    The Authorizer validates the customer token against the RDS
    customer_tokens table and supplies the authenticated customer_id.
    The request body cannot override this identity.
    """
    if auth["role"] != "CUSTOMER":
        return None

    customer_id = auth.get("customer_id")
    if customer_id in (None, ""):
        raise PermissionError("Authenticated customer_id is missing")

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                customer_id,
                customer_name,
                customer_email
            FROM customers
            WHERE customer_id = %s
            LIMIT 1
            """,
            (int(customer_id),),
        )
        customer = cursor.fetchone()

    if not customer:
        raise PermissionError("Authenticated customer does not exist")

    return customer


# ============================================================
# CUSTOMER EMAIL VALIDATION
# ============================================================

def get_configured_customer_identity():
    """Return the customer identity configured by deployment."""
    customer_email = os.environ.get("CUSTOMER_EMAIL", "").strip().lower()
    if not customer_email:
        raise ValueError("Configured CUSTOMER_EMAIL is missing")

    try:
        customer_id = int(os.environ.get("CUSTOMER_ID", "1"))
    except ValueError:
        raise ValueError("Configured CUSTOMER_ID must be an integer")

    if customer_id <= 0:
        raise ValueError("Configured CUSTOMER_ID must be positive")

    return customer_id, validate_customer_email(customer_email)


def sync_configured_customer(
    connection,
    configured_email=None,
    configured_customer_id=None,
):
    """
    Synchronize the configured CloudMart customer.

    CUSTOMER_ID (default 1) is the permanent identity.
    CUSTOMER_EMAIL is the deployment/GitHub variable source of truth.

    If that email currently belongs to another customer row, that duplicate
    customer's orders are reassigned to the configured customer_id before
    the duplicate row is removed. This avoids creating a new customer every
    time the configured email changes.
    """
    if configured_email is None:
        configured_email = os.environ.get("CUSTOMER_EMAIL", "").strip().lower()
    else:
        configured_email = str(configured_email).strip().lower()

    if not configured_email:
        raise ValueError("Configured customer email is missing")

    configured_email = validate_customer_email(configured_email)

    if configured_customer_id in (None, ""):
        configured_customer_id = os.environ.get("CUSTOMER_ID", "1")

    try:
        configured_customer_id = int(configured_customer_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("Configured CUSTOMER_ID must be an integer") from exc

    if configured_customer_id <= 0:
        raise ValueError("Configured CUSTOMER_ID must be greater than zero")

    with connection.cursor() as cursor:
        # Lock the configured customer row when it exists.
        cursor.execute(
            """
            SELECT customer_id, customer_name, customer_email
            FROM customers
            WHERE customer_id = %s
            LIMIT 1
            FOR UPDATE
            """,
            (configured_customer_id,),
        )
        customer = cursor.fetchone()

        # If the configured email belongs to another row, consolidate that
        # row into the configured customer ID before updating the email.
        cursor.execute(
            """
            SELECT customer_id, customer_name, customer_email
            FROM customers
            WHERE LOWER(customer_email) = %s
              AND customer_id <> %s
            LIMIT 1
            FOR UPDATE
            """,
            (configured_email, configured_customer_id),
        )
        duplicate = cursor.fetchone()

        if duplicate:
            duplicate_id = int(duplicate["customer_id"])

            logger.warning(
                "Consolidating duplicate configured customer email %s: "
                "customer_id=%s -> customer_id=%s",
                configured_email,
                duplicate_id,
                configured_customer_id,
            )

            # Reassign any existing orders first so the duplicate customer
            # row can be safely removed.
            cursor.execute(
                """
                UPDATE orders
                SET customer_id = %s,
                    customer_email = %s
                WHERE customer_id = %s
                """,
                (
                    configured_customer_id,
                    configured_email,
                    duplicate_id,
                ),
            )

            # If the configured customer row does not exist, we can reuse
            # the duplicate row's customer name when creating ID 1.
            if not customer:
                cursor.execute(
                    """
                    INSERT INTO customers
                        (customer_id, customer_name, customer_email)
                    VALUES (%s, %s, %s)
                    """,
                    (
                        configured_customer_id,
                        duplicate["customer_name"],
                        configured_email,
                    ),
                )
                customer = {
                    "customer_id": configured_customer_id,
                    "customer_name": duplicate["customer_name"],
                    "customer_email": configured_email,
                }

            cursor.execute(
                """
                DELETE FROM customers
                WHERE customer_id = %s
                """,
                (duplicate_id,),
            )

        elif not customer:
            customer_name = configured_email.split("@", 1)[0]

            cursor.execute(
                """
                INSERT INTO customers
                    (customer_id, customer_name, customer_email)
                VALUES (%s, %s, %s)
                """,
                (
                    configured_customer_id,
                    customer_name,
                    configured_email,
                ),
            )

            customer = {
                "customer_id": configured_customer_id,
                "customer_name": customer_name,
                "customer_email": configured_email,
            }

        # The configured ID/email are authoritative.
        cursor.execute(
            """
            UPDATE customers
            SET customer_email = %s
            WHERE customer_id = %s
            """,
            (
                configured_email,
                configured_customer_id,
            ),
        )

        # Keep the denormalized legacy column synchronized for every
        # historical order belonging to the configured customer.
        cursor.execute(
            """
            UPDATE orders
            SET customer_email = %s
            WHERE customer_id = %s
              AND (
                    customer_email IS NULL
                    OR LOWER(customer_email) <> %s
              )
            """,
            (
                configured_email,
                configured_customer_id,
                configured_email,
            ),
        )
        orders_updated = cursor.rowcount

        connection.commit()

    logger.info(
        "Configured customer synchronized: customer_id=%s customer_email=%s orders_updated=%s",
        configured_customer_id,
        configured_email,
        orders_updated,
    )

    # Fetch the final row so callers always receive customer_name as well.
    # This prevents create_order() from raising KeyError: 'customer_name'.
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT customer_id, customer_name, customer_email
            FROM customers
            WHERE customer_id = %s
            LIMIT 1
            """,
            (configured_customer_id,),
        )
        final_customer = cursor.fetchone()

    if not final_customer:
        raise LookupError(
            f"Configured customer {configured_customer_id} was not found after synchronization"
        )

    return {
        "customer_id": int(final_customer["customer_id"]),
        "customer_name": final_customer["customer_name"],
        "customer_email": final_customer["customer_email"],
        "orders_updated": int(orders_updated),
    }





def validate_customer_email(value):
    """
    Validate an authenticated/configured customer email.

    The Create Order API does not accept customer_email from the request
    body. The email comes from the Authorization token's authorizer context
    and is backed by the deployment configuration.
    """
    if value is None:
        raise ValueError(
            "customer_email is required"
        )

    if not isinstance(value, str):
        raise ValueError(
            "customer_email must be a string"
        )

    customer_email = value.strip()

    if not customer_email:
        raise ValueError(
            "customer_email is required"
        )

    if len(customer_email) > 254:
        raise ValueError(
            "customer_email is too long"
        )

    if not re.fullmatch(
        r"[^@\s]+@[^@\s]+\.[^@\s]+",
        customer_email,
    ):
        raise ValueError(
            "customer_email must be a valid email address"
        )

    return customer_email


# ============================================================
# SSM / RDS
# ============================================================

def get_parameter(name, decrypt=False):
    result = ssm.get_parameter(
        Name=name,
        WithDecryption=decrypt,
    )
    return result["Parameter"]["Value"]


def get_db_connection():
    host = get_parameter(
        os.environ["DB_HOST_PARAMETER_NAME"]
    )

    port = int(
        get_parameter(
            os.environ["DB_PORT_PARAMETER_NAME"]
        )
    )

    database = get_parameter(
        os.environ["DB_NAME_PARAMETER_NAME"]
    )

    username = get_parameter(
        os.environ["DB_USERNAME_PARAMETER_NAME"],
        decrypt=True,
    )

    password = get_parameter(
        os.environ["DB_PASSWORD_PARAMETER_NAME"],
        decrypt=True,
    )

    return pymysql.connect(
        host=host,
        port=port,
        user=username,
        password=password,
        database=database,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=10,
        write_timeout=10,
        autocommit=False,
    )


# ============================================================
# REQUEST PARSING / VALIDATION
# ============================================================

def parse_body(event):
    body = event.get("body")

    if isinstance(body, dict):
        return body

    if not body:
        return {}

    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "Request body must contain valid JSON"
        ) from exc


def validate_request(payload, require_customer_id=False):
    """
    Validate the order request structure.

    Customer identity is handled separately from item/order validation.
    """
    if not isinstance(payload, dict):
        raise ValueError(
            "Request body must be a JSON object"
        )

    # Customer identity fields are intentionally not used for CUSTOMER
    # Create Order requests. Identity comes from the token.
    # Keep customer_id parsing only for backwards-compatible ADMIN/internal
    # calls that explicitly request it.
    customer_id = payload.get("customer_id")

    if require_customer_id:
        if customer_id is None:
            raise ValueError(
                "customer_id is required"
            )

        try:
            customer_id = int(customer_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "customer_id must be an integer"
            ) from exc

        if customer_id <= 0:
            raise ValueError(
                "customer_id must be greater than zero"
            )

    items = payload.get("items")

    if not isinstance(items, list) or not items:
        raise ValueError(
            "items must be a non-empty array"
        )

    validated = []

    for item in items:
        if not isinstance(item, dict):
            raise ValueError(
                "Each item must be an object"
            )

        product_id = item.get("product_id")
        quantity = item.get("quantity")

        if product_id is None:
            raise ValueError(
                "product_id is required"
            )

        if quantity is None:
            raise ValueError(
                "quantity is required"
            )

        try:
            product_id = int(product_id)
            quantity = int(quantity)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "product_id and quantity must be integers"
            ) from exc

        if product_id <= 0:
            raise ValueError(
                "product_id must be greater than zero"
            )

        if quantity <= 0:
            raise ValueError(
                "quantity must be greater than zero"
            )

        validated.append(
            {
                "product_id": product_id,
                "quantity": quantity,
            }
        )

    return customer_id, validated



def get_or_create_customer(connection, customer_email, customer_name=None):
    """
    Find a customer by email. If the email does not exist, create a new
    customer and return its customer_id and stored customer details.
    """
    customer_email = customer_email.strip().lower()

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT customer_id, customer_name, customer_email
            FROM customers
            WHERE LOWER(customer_email) = %s
            LIMIT 1
            FOR UPDATE
            """,
            (customer_email,),
        )
        customer = cursor.fetchone()

        if customer:
            return customer

        if not customer_name:
            customer_name = customer_email.split("@", 1)[0]

        cursor.execute(
            """
            INSERT INTO customers (customer_name, customer_email)
            VALUES (%s, %s)
            """,
            (customer_name, customer_email),
        )

        customer_id = cursor.lastrowid

        cursor.execute(
            """
            SELECT customer_id, customer_name, customer_email
            FROM customers
            WHERE customer_id = %s
            """,
            (customer_id,),
        )
        return cursor.fetchone()


# ============================================================
# RDS transaction + order/order_items + inventory
# ============================================================

class StockError(Exception):
    pass


def create_order(connection, customer, items):
    """Create a PENDING order without reserving inventory.

    Inventory is checked and reserved asynchronously by the OrderPlaced
    EventBridge processing rule. This keeps the API response fast while the
    order moves automatically from PENDING to CONFIRMED or FAILED.
    """
    with connection.cursor() as cursor:
        customer_id = int(customer["customer_id"])
        customer_email = customer["customer_email"]

        quantities = {}
        for item in items:
            product_id = int(item["product_id"])
            quantities[product_id] = quantities.get(product_id, 0) + int(item["quantity"])

        order_items = []
        total_amount = Decimal("0.00")

        # Validate products and capture their current prices. Stock is deliberately
        # not changed here; the asynchronous processor owns the stock decision.
        for product_id in sorted(quantities):
            quantity = quantities[product_id]
            cursor.execute("""
                SELECT product_id, name, price, stock_quantity,
                       low_stock_threshold, status, deleted_at
                FROM products WHERE product_id = %s
            """, (product_id,))
            product = cursor.fetchone()
            if not product:
                raise LookupError(f"Product {product_id} not found")
            if product["deleted_at"] is not None:
                raise LookupError(f"Product {product_id} is deleted")
            if product["status"] != "ACTIVE":
                raise ValueError(f"Product {product_id} is not active")

            unit_price = Decimal(str(product["price"]))
            subtotal = unit_price * quantity
            total_amount += subtotal
            order_items.append({
                "product_id": product_id,
                "product_name": product["name"],
                "quantity": quantity,
                "unit_price": unit_price,
                "subtotal": subtotal,
            })

        cursor.execute("""
            INSERT INTO orders (customer_id, customer_email, status, total_amount)
            VALUES (%s, %s, %s, %s)
        """, (customer_id, customer_email, "PENDING", total_amount))
        order_id = cursor.lastrowid

        for item in order_items:
            cursor.execute("""
                INSERT INTO order_items (order_id, product_id, quantity, unit_price, subtotal)
                VALUES (%s, %s, %s, %s, %s)
            """, (
                order_id, item["product_id"], item["quantity"],
                item["unit_price"], item["subtotal"]
            ))

        cursor.execute("""
            INSERT INTO order_status_history (order_id, old_status, new_status, changed_by)
            VALUES (%s, %s, %s, %s)
        """, (order_id, None, "PENDING", "order-api"))
        connection.commit()

        return {
            "order_id": int(order_id),
            "customer_id": int(customer_id),
            "customer_name": customer["customer_name"],
            "customer_email": customer_email,
            "status": "PENDING",
            "total_amount": total_amount,
            "items": order_items,
        }


def process_order_placed_event(connection, order_id):
    """Check stock for a PENDING order and move it to CONFIRMED or FAILED.

    The order row and all involved product rows are locked in one transaction.
    If every requested quantity is available, stock is deducted in the same
    transaction and the order becomes CONFIRMED. If any product lacks stock,
    the order becomes FAILED and inventory is not changed because nothing was
    deducted. If a confirmed order is later canceled/failed, update_order_status
    returns its deducted quantity to inventory and publishes Inventory Changed.
    Lifecycle events are published only after the database transaction commits.
    """
    order_id = int(order_id)
    inventory_events = []
    new_status = None

    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT order_id, customer_id, status, total_amount
            FROM orders
            WHERE order_id = %s
            FOR UPDATE
        """, (order_id,))
        order = cursor.fetchone()
        if not order:
            raise LookupError(f"Order {order_id} not found")

        # EventBridge can retry delivery. Do not process an order twice.
        if str(order["status"]).upper() != "PENDING":
            logger.info("Order %s already has status %s; skipping processing", order_id, order["status"])
            return False, str(order["status"]).upper()

        cursor.execute("""
            SELECT
                oi.product_id,
                oi.quantity,
                p.name AS product_name,
                p.stock_quantity,
                p.low_stock_threshold,
                p.status AS product_status,
                p.deleted_at
            FROM order_items oi
            JOIN products p ON p.product_id = oi.product_id
            WHERE oi.order_id = %s
            ORDER BY oi.product_id
            FOR UPDATE
        """, (order_id,))
        items = cursor.fetchall()

        if not items:
            new_status = "FAILED"
        else:
            insufficient_product = None
            for item in items:
                if item["deleted_at"] is not None or item["product_status"] != "ACTIVE":
                    insufficient_product = item
                    break
                if int(item["stock_quantity"]) < int(item["quantity"]):
                    insufficient_product = item
                    break

            if insufficient_product:
                new_status = "FAILED"
                logger.warning(
                    "Order %s failed stock check for product %s",
                    order_id, insufficient_product["product_id"],
                )
            else:
                new_status = "CONFIRMED"
                for item in items:
                    old_stock = int(item["stock_quantity"])
                    quantity = int(item["quantity"])
                    new_stock = old_stock - quantity
                    cursor.execute("""
                        UPDATE products
                        SET stock_quantity = stock_quantity - %s,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE product_id = %s
                          AND stock_quantity >= %s
                    """, (quantity, item["product_id"], quantity))
                    if cursor.rowcount != 1:
                        raise StockError(f"Insufficient stock for product {item['product_id']}")
                    inventory_events.append({
                        "product_id": int(item["product_id"]),
                        "product_name": item["product_name"],
                        "old_stock": old_stock,
                        "new_stock": new_stock,
                        "threshold": int(item["low_stock_threshold"]),
                    })

        cursor.execute(
            "UPDATE orders SET status = %s WHERE order_id = %s",
            (new_status, order_id),
        )
        cursor.execute("""
            INSERT INTO order_status_history (order_id, old_status, new_status, changed_by)
            VALUES (%s, %s, %s, %s)
        """, (order_id, "PENDING", new_status, "order-eventbridge"))
        connection.commit()

    for change in inventory_events:
        publish_inventory_event_from_order(**change)

    order = get_order_by_id(connection, order_id)
    if order and not publish_order_event(
        "OrderConfirmed" if new_status == "CONFIRMED" else "OrderFailed",
        order,
    ):
        logger.error("Order %s changed to %s but lifecycle event could not be published", order_id, new_status)

    return True, new_status


def publish_inventory_event_from_order(product_id, product_name, old_stock, new_stock, threshold):
    """Publish the existing Product Lambda inventory event shape from Order Lambda."""
    detail = {"product_id": int(product_id), "product_name": product_name,
              "old_stock": int(old_stock), "new_stock": int(new_stock),
              "low_stock_threshold": int(threshold),
              "low_stock": int(new_stock) <= int(threshold)}
    try:
        result = events.put_events(Entries=[{
            "Source": "cloudmart.product", "DetailType": "Inventory Changed",
            "EventBusName": os.environ["EVENT_BUS_NAME"], "Detail": json.dumps(detail)
        }])
        if result.get("FailedEntryCount", 0) > 0:
            logger.error("Inventory Changed event failed: %s", json.dumps(result, default=json_serializer))
            return False
        logger.info("Inventory Changed event published: %s", json.dumps(detail))
        return True
    except Exception:
        logger.exception("Unable to publish Inventory Changed event")
        return False

def publish_order_placed_event(order):
    detail = {
        "order_id": order["order_id"],
        "customer_id": order["customer_id"],
        "customer_name": order.get("customer_name"),
        "customer_email": order.get("customer_email"),
        "status": order["status"],
        "total_amount": float(
            order["total_amount"]
        ),
        "items_summary": "\n\n".join(
            [
                f"Product: {item.get('product_name', 'Product ' + str(item['product_id']))}\\n"
                f"Product ID: {item['product_id']}\\n"
                f"Quantity: {item['quantity']}\\n"
                f"Unit Price: {float(item['unit_price']):.2f}\\n"
                f"Subtotal: {float(item['subtotal']):.2f}"
                for item in order["items"]
            ]
        ),
        "items": [
            {
                "product_id": item["product_id"],
                "product_name": item.get("product_name"),
                "quantity": item["quantity"],
                "unit_price": float(
                    item["unit_price"]
                ),
                "subtotal": float(
                    item["subtotal"]
                ),
            }
            for item in order["items"]
        ],
    }

    try:
        result = events.put_events(
            Entries=[
                {
                    "Source": "cloudmart.order",
                    "DetailType": "OrderPlaced",
                    "EventBusName": os.environ[
                        "EVENT_BUS_NAME"
                    ],
                    "Detail": json.dumps(
                        detail
                    ),
                }
            ]
        )

        if result.get("FailedEntryCount", 0) > 0:
            logger.error(
                "OrderPlaced event failed: %s",
                json.dumps(
                    result,
                    default=json_serializer,
                ),
            )
            return False

        logger.info(
            "OrderPlaced event published: %s",
            json.dumps(detail),
        )

        return True

    except Exception:
        logger.exception(
            "Unable to publish OrderPlaced event"
        )
        return False


# ============================================================
# ORDER LIFECYCLE EVENT HELPERS
# ============================================================

def publish_order_event(detail_type, order):
    detail = {
        "order_id": int(order["order_id"]),
        "customer_id": int(order["customer_id"]),
        "customer_name": order.get("customer_name"),
        "customer_email": order.get("customer_email"),
        "status": order.get("status"),
        "total_amount": float(order["total_amount"]),
        "items_summary": "\n\n".join(
            [
                f"Product: {item.get('product_name', 'Product ' + str(item['product_id']))}\\n"
                f"Product ID: {item['product_id']}\\n"
                f"Quantity: {item['quantity']}\\n"
                f"Unit Price: {float(item['unit_price']):.2f}\\n"
                f"Subtotal: {float(item['subtotal']):.2f}"
                for item in (order.get("items") or [])
            ]
        ),
        "items": [
            {
                "product_id": int(item["product_id"]),
                "product_name": item.get("product_name"),
                "quantity": int(item["quantity"]),
                "unit_price": float(item["unit_price"]),
                "subtotal": float(item["subtotal"]),
            }
            for item in (order.get("items") or [])
        ],
    }

    try:
        result = events.put_events(
            Entries=[
                {
                    "Source": "cloudmart.order",
                    "DetailType": detail_type,
                    "EventBusName": os.environ["EVENT_BUS_NAME"],
                    "Detail": json.dumps(detail),
                }
            ]
        )

        if result.get("FailedEntryCount", 0) != 0:
            logger.error(
                "%s event failed: %s",
                detail_type,
                json.dumps(result, default=json_serializer),
            )
            return False

        logger.info(
            "%s event published: %s",
            detail_type,
            json.dumps(detail, default=json_serializer),
        )
        return True

    except Exception:
        logger.exception("Unable to publish %s event", detail_type)
        return False


def update_order_status(connection, order_id, new_status):
    allowed_statuses = {"CONFIRMED", "CANCELED", "FAILED", "COMPLETED"}
    new_status = str(new_status).upper().strip()
    if new_status not in allowed_statuses:
        raise ValueError("status must be one of: CONFIRMED, CANCELED, FAILED, COMPLETED")

    inventory_events = []
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT order_id, customer_id, status, total_amount
            FROM orders WHERE order_id = %s FOR UPDATE
        """, (order_id,))
        existing = cursor.fetchone()
        if not existing:
            raise LookupError(f"Order {order_id} not found")
        old_status = str(existing["status"]).upper()
        if old_status == new_status:
            connection.commit()
            return False
        if old_status in {"CANCELED", "FAILED", "COMPLETED"}:
            raise ValueError(f"Order {order_id} is already in terminal status {old_status}")

        # Stock is deducted exactly when the order becomes CONFIRMED.
        # If a confirmed order is later CANCELED or FAILED, return its
        # quantity to inventory exactly once and publish Inventory Changed
        # events so the existing inventory/low-stock flow is updated.
        # PENDING -> CANCELED/FAILED has no inventory change because stock
        # was never deducted.
        if old_status == "CONFIRMED" and new_status in {"CANCELED", "FAILED"}:
            cursor.execute("""
                SELECT oi.product_id, oi.quantity, p.name AS product_name,
                       p.stock_quantity, p.low_stock_threshold
                FROM order_items oi
                JOIN products p ON p.product_id = oi.product_id
                WHERE oi.order_id = %s
                ORDER BY oi.product_id
                FOR UPDATE
            """, (order_id,))
            for item in cursor.fetchall():
                old_stock = int(item["stock_quantity"])
                new_stock = old_stock + int(item["quantity"])
                cursor.execute("""
                    UPDATE products
                    SET stock_quantity = stock_quantity + %s, updated_at = CURRENT_TIMESTAMP
                    WHERE product_id = %s
                """, (item["quantity"], item["product_id"]))
                inventory_events.append({
                    "product_id": int(item["product_id"]),
                    "product_name": item["product_name"],
                    "old_stock": old_stock,
                    "new_stock": new_stock,
                    "threshold": int(item["low_stock_threshold"]),
                })

        cursor.execute("UPDATE orders SET status = %s WHERE order_id = %s", (new_status, order_id))
        cursor.execute("""
            INSERT INTO order_status_history (order_id, old_status, new_status, changed_by)
            VALUES (%s, %s, %s, %s)
        """, (order_id, old_status, new_status, "order-api"))
    connection.commit()

    for change in inventory_events:
        publish_inventory_event_from_order(**change)
    return True


# ============================================================
# PUT /orders/{id}
# ============================================================

def update_order(connection, order_id, customer_id, items):
    """Replace the items of a PENDING order before stock processing.

    Because PENDING orders do not reserve inventory, this operation changes
    only order_items and total_amount. The asynchronous OrderPlaced processor
    performs the final stock check and reservation.
    """
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT order_id, customer_id, status
            FROM orders
            WHERE order_id = %s
            FOR UPDATE
        """, (order_id,))
        existing_order = cursor.fetchone()

        if not existing_order:
            raise LookupError(f"Order {order_id} not found")
        if int(existing_order["customer_id"]) != int(customer_id):
            raise ValueError(f"Order {order_id} does not belong to customer {customer_id}")
        if existing_order["status"] != "PENDING":
            raise ValueError(f"Order {order_id} can only be updated while status is PENDING")

        requested_quantities = {}
        for item in items:
            product_id = int(item["product_id"])
            quantity = int(item["quantity"])
            requested_quantities[product_id] = requested_quantities.get(product_id, 0) + quantity

        product_details = {}
        for product_id in sorted(requested_quantities):
            cursor.execute("""
                SELECT product_id, name, price, status, deleted_at
                FROM products
                WHERE product_id = %s
            """, (product_id,))
            product = cursor.fetchone()
            if not product:
                raise LookupError(f"Product {product_id} not found")
            if product["deleted_at"] is not None:
                raise LookupError(f"Product {product_id} is deleted")
            if product["status"] != "ACTIVE":
                raise ValueError(f"Product {product_id} is not active")
            product_details[product_id] = product

        cursor.execute("DELETE FROM order_items WHERE order_id = %s", (order_id,))
        total_amount = Decimal("0.00")
        for product_id, quantity in requested_quantities.items():
            product = product_details[product_id]
            unit_price = Decimal(str(product["price"]))
            subtotal = unit_price * quantity
            total_amount += subtotal
            cursor.execute("""
                INSERT INTO order_items (order_id, product_id, quantity, unit_price, subtotal)
                VALUES (%s, %s, %s, %s, %s)
            """, (order_id, product_id, quantity, unit_price, subtotal))

        cursor.execute("UPDATE orders SET total_amount = %s WHERE order_id = %s", (total_amount, order_id))
        connection.commit()

        return {
            "order_id": int(order_id),
            "customer_id": int(customer_id),
            "status": existing_order["status"],
            "total_amount": total_amount,
        }


# ============================================================
#  GET /orders/{id}
# ============================================================

def get_path_order_id(event):
    path_params = event.get("pathParameters") or {}

    value = (
        path_params.get("id")
        or path_params.get("orderId")
    )

    if value is None:
        path = (
            event.get("path")
            or event.get("resource")
            or ""
        )

        parts = [
            part
            for part in path.split("/")
            if part
        ]

        if (
            len(parts) >= 2
            and parts[0].lower() == "orders"
        ):
            value = parts[1]

    if value is None or str(value).strip() == "":
        return None

    try:
        order_id = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "order id must be an integer"
        ) from exc

    if order_id <= 0:
        raise ValueError(
            "order id must be greater than zero"
        )

    return order_id


def get_order_by_id(connection, order_id):
    with connection.cursor() as cursor:

        cursor.execute(
            """
            SELECT
                o.order_id,
                o.customer_id,
                c.customer_name AS customer_name,
                c.customer_email AS customer_email,
                o.status,
                o.total_amount,
                o.created_at,
                o.updated_at
            FROM orders o
            LEFT JOIN customers c
                ON c.customer_id = o.customer_id
            WHERE o.order_id = %s
            """,
            (order_id,),
        )

        order = cursor.fetchone()

        if not order:
            return None

        cursor.execute(
            """
            SELECT
                oi.order_item_id,
                oi.product_id,
                p.name AS product_name,
                oi.quantity,
                oi.unit_price,
                oi.subtotal
            FROM order_items oi
            LEFT JOIN products p
                ON p.product_id = oi.product_id
            WHERE oi.order_id = %s
            ORDER BY oi.order_item_id
            """,
            (order_id,),
        )

        items = cursor.fetchall()

        order["order_id"] = int(
            order["order_id"]
        )

        order["customer_id"] = int(
            order["customer_id"]
        )

        for item in items:
            item["order_item_id"] = int(
                item["order_item_id"]
            )
            item["product_id"] = int(
                item["product_id"]
            )
            item["quantity"] = int(
                item["quantity"]
            )

        order["items"] = items

        return order


# ============================================================
# TASK 4: GET /orders?customerId=X
# ============================================================

def get_customer_id_from_query(event):
    query = event.get(
        "queryStringParameters"
    ) or {}

    value = query.get("customerId")

    # Compatibility with customer_id if used.
    if value is None:
        value = query.get("customer_id")

    if value is None or str(value).strip() == "":
        raise ValueError(
            "customerId query parameter is required"
        )

    try:
        customer_id = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "customerId must be an integer"
        ) from exc

    if customer_id <= 0:
        raise ValueError(
            "customerId must be greater than zero"
        )

    return customer_id


def get_orders_by_customer(
    connection,
    customer_id,
):
    with connection.cursor() as cursor:

        # First verify the customer exists.
        cursor.execute(
            """
            SELECT
                customer_id,
                customer_name,
                customer_email
            FROM customers
            WHERE customer_id = %s
            """,
            (customer_id,),
        )

        customer = cursor.fetchone()

        if not customer:
            raise LookupError(
                f"Customer {customer_id} not found"
            )

        cursor.execute(
            """
            SELECT
                o.order_id,
                o.customer_id,
                c.customer_name AS customer_name,
                c.customer_email AS customer_email,
                o.status,
                o.total_amount,
                o.created_at,
                o.updated_at
            FROM orders o
            LEFT JOIN customers c
                ON c.customer_id = o.customer_id
            WHERE o.customer_id = %s
            ORDER BY
                o.created_at DESC,
                o.order_id DESC
            """,
            (customer_id,),
        )

        orders = cursor.fetchall()

        for order in orders:

            order["order_id"] = int(
                order["order_id"]
            )

            order["customer_id"] = int(
                order["customer_id"]
            )

            cursor.execute(
                """
                SELECT
                    oi.order_item_id,
                    oi.product_id,
                    p.name AS product_name,
                    oi.quantity,
                    oi.unit_price,
                    oi.subtotal
                FROM order_items oi
                LEFT JOIN products p
                    ON p.product_id = oi.product_id
                WHERE oi.order_id = %s
                ORDER BY oi.order_item_id
                """,
                (order["order_id"],),
            )

            items = cursor.fetchall()

            for item in items:
                item["order_item_id"] = int(
                    item["order_item_id"]
                )
                item["product_id"] = int(
                    item["product_id"]
                )
                item["quantity"] = int(
                    item["quantity"]
                )

            order["items"] = items

        return orders


# ============================================================
# LAMBDA HANDLER
# ============================================================

# ============================================================
# CONFIGURED CUSTOMER EMAIL SYNCHRONIZATION
#
# GitHub Actions CUSTOMER_EMAIL is the source of truth.
# This direct action updates the configured customer and all of its
# existing orders so historical order rows remain consistent.
# ============================================================



def json_serializer(value):
    """Serialize database values that JSON does not handle natively."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body, default=json_serializer),
    }


def error_response(status_code, code, message):
    return response(
        status_code,
        {
            "code": code,
            "message": message,
        },
    )


def authorize_order_access(connection, auth, order):
    """Allow ADMIN all orders; CUSTOMER only its authenticated customer orders."""
    if auth["role"] == "ADMIN":
        return

    if auth["role"] != "CUSTOMER":
        raise PermissionError("Authenticated role is invalid")

    customer = resolve_customer_identity(connection, auth)

    if int(order["customer_id"]) != int(customer["customer_id"]):
        raise PermissionError(
            "Customer cannot access another customer's order"
        )


def lambda_handler(event, context):
    if event.get("action") == "sync_configured_customer":
        connection = None
        try:
            connection = get_db_connection()
            return response(
                200,
                sync_configured_customer(connection),
            )
        except Exception as exc:
            if connection:
                connection.rollback()
            logger.exception("Configured customer synchronization failed")
            return response(
                500,
                {
                    "message": "Configured customer synchronization failed",
                    "error": str(exc),
                },
            )
        finally:
            if connection:
                connection.close()


    # ------------------------------------------------------------
    # EventBridge: process OrderPlaced asynchronously.
    # This path is intentionally separate from API Gateway requests.
    # ------------------------------------------------------------
    if (event.get("source") == "cloudmart.order" and
            event.get("detail-type") == "OrderPlaced"):
        connection = None
        try:
            detail = event.get("detail") or {}
            order_id = detail.get("order_id")
            if order_id in (None, ""):
                raise ValueError("OrderPlaced event is missing order_id")
            connection = get_db_connection()
            processed, status = process_order_placed_event(connection, order_id)
            return {
                "statusCode": 200,
                "processed": processed,
                "order_id": int(order_id),
                "status": status,
            }
        except Exception:
            if connection:
                connection.rollback()
            logger.exception("OrderPlaced event processing failed")
            raise
        finally:
            if connection:
                connection.close()


    logger.info(
        "Order Lambda invoked: %s",
        json.dumps(
            event,
            default=json_serializer,
        ),
    )

    method = (
        event.get("httpMethod")
        or ""
    ).upper()

    if method == "OPTIONS":
        return response(
            200,
            {"message": "OK"},
        )

    connection = None

    try:

        # ----------------------------------------------------
        # GET /orders/{id}
        # ----------------------------------------------------
        if method == "GET":

            path_order_id = get_path_order_id(
                event
            )

            auth = get_authorization_context(event)
            connection = get_db_connection()

            if path_order_id is not None:

                order = get_order_by_id(
                    connection,
                    path_order_id,
                )

                if not order:
                    return error_response(
                        404,
                        "ORDER_NOT_FOUND",
                        f"Order {path_order_id} not found",
                    )

                authorize_order_access(
                    connection,
                    auth,
                    order,
                )

                return response(
                    200,
                    order,
                )

            # ------------------------------------------------
            # GET /orders?customerId=X
            # ------------------------------------------------
            if auth["role"] == "CUSTOMER":
                customer = resolve_customer_identity(
                    connection,
                    auth,
                )
                customer_id = int(
                    customer["customer_id"]
                )
            else:
                customer_id = get_customer_id_from_query(
                    event
                )

            orders = get_orders_by_customer(
                connection,
                customer_id,
            )

            return response(
                200,
                {
                    "customer_id": customer_id,
                    "count": len(orders),
                    "orders": orders,
                },
            )

        # ----------------------------------------------------
        # POST /orders
        #
        # Creates a PENDING order and publishes OrderPlaced.
        # Order status changes are handled separately by
        # PATCH /orders/{id}/status.
        # ----------------------------------------------------
        if method == "POST":

            payload = parse_body(event)

            auth = get_authorization_context(event)

            # Customer identity must come exclusively from the Authorization
            # token. Do not accept customer email/name/id from the request body.
            if auth["role"] == "CUSTOMER":
                forbidden_identity_fields = [
                    field
                    for field in ("customer_id", "customer_email", "customer_name")
                    if field in payload
                ]
                if forbidden_identity_fields:
                    raise ValueError(
                        "Customer identity must not be supplied in the request body; "
                        "use the Authorization Bearer token"
                    )

            _, items = validate_request(payload)

            connection = get_db_connection()

            if auth["role"] == "CUSTOMER":
                # CUSTOMER identity comes only from the Authorizer token.
                customer = resolve_customer_identity(
                    connection,
                    auth,
                )
            else:
                # ADMIN creates orders for the configured CUSTOMER identity.
                # Postman cannot select an arbitrary customer email.
                customer = sync_configured_customer(connection)

            order = create_order(
                connection,
                customer,
                items,
            )

            if not publish_order_placed_event(order):
                logger.error(
                    "Order %s created but OrderPlaced could not be published",
                    order["order_id"],
                )

            return response(201, order)

        # ----------------------------------------------------
        # PATCH /orders/{id}/status
        #
        # Updates only the order status. This replaces the old
        # POST /orders status-update behavior.
        # Status changes remain ADMIN-only, matching the existing
        # authorization rule.
        # ----------------------------------------------------
        if method == "PATCH":

            order_id = get_path_order_id(event)

            if order_id is None:
                raise ValueError(
                    "order id is required in the path"
                )

            payload = parse_body(event)

            auth = get_authorization_context(event)
            require_admin(auth)

            new_status = payload.get("status")

            if new_status is None:
                raise ValueError(
                    "status is required"
                )

            connection = get_db_connection()

            changed = update_order_status(
                connection,
                order_id,
                new_status,
            )

            order = get_order_by_id(
                connection,
                order_id,
            )

            if not order:
                return error_response(
                    404,
                    "ORDER_NOT_FOUND",
                    f"Order {order_id} not found",
                )

            if changed:
                event_type = {
                    "CONFIRMED": "OrderConfirmed",
                    "CANCELED": "OrderCanceled",
                    "FAILED": "OrderFailed",
                    "COMPLETED": "OrderCompleted",
                }[str(new_status).upper().strip()]

                if not publish_order_event(event_type, order):
                    logger.error(
                        "Order %s changed to %s but %s could not be published",
                        order_id,
                        str(new_status).upper().strip(),
                        event_type,
                    )

            return response(200, order)

        # ----------------------------------------------------
        # PUT /orders/{id}
        #
        # Updates an existing PENDING order. Inventory is
        # adjusted by the quantity delta in the same RDS
        # transaction as order_items and total_amount.
        # ----------------------------------------------------
        if method == "PUT":

            order_id = get_path_order_id(event)

            if order_id is None:
                raise ValueError(
                    "order id is required in the path"
                )

            payload = parse_body(event)

            auth = get_authorization_context(event)

            _, items = validate_request(
                payload,
                require_customer_id=False,
            )

            connection = get_db_connection()

            existing_order = get_order_by_id(
                connection,
                order_id,
            )

            if not existing_order:
                return error_response(
                    404,
                    "ORDER_NOT_FOUND",
                    f"Order {order_id} not found",
                )

            if auth["role"] == "CUSTOMER":
                customer = resolve_customer_identity(
                    connection,
                    auth,
                )
                customer_id = int(
                    customer["customer_id"]
                )

                if int(existing_order["customer_id"]) != customer_id:
                    raise PermissionError(
                        "Customer cannot update another customer's order"
                    )
            else:
                customer_id = int(
                    existing_order["customer_id"]
                )

            update_order(
                connection,
                order_id,
                customer_id,
                items,
            )

            # Return the complete updated order.
            order = get_order_by_id(
                connection,
                order_id,
            )

            if not order:
                return error_response(
                    404,
                    "ORDER_NOT_FOUND",
                    f"Order {order_id} not found",
                )

            logger.info(
                "Order %s updated successfully: %s",
                order_id,
                json.dumps(
                    order,
                    default=json_serializer,
                ),
            )

            return response(
                200,
                order,
            )

        return error_response(
            405,
            "METHOD_NOT_ALLOWED",
            "Supported methods are GET, POST, PUT, PATCH, OPTIONS",
        )

    except PermissionError as exc:

        if connection:
            connection.rollback()

        return error_response(
            403,
            "FORBIDDEN",
            str(exc),
        )

    except ValueError as exc:

        if connection:
            connection.rollback()

        return error_response(
            400,
            "INVALID_REQUEST",
            str(exc),
        )

    except LookupError as exc:

        if connection:
            connection.rollback()

        return error_response(
            404,
            "NOT_FOUND",
            str(exc),
        )

    except StockError as exc:

        if connection:
            connection.rollback()

        return error_response(
            409,
            "INSUFFICIENT_STOCK",
            str(exc),
        )

    except pymysql.MySQLError as exc:

        if connection:
            connection.rollback()

        logger.exception(
            "Database operation failed",
            extra={
                "error": str(exc),
                "mysql_errno": exc.args[0] if exc.args else None,
            },
        )

        return error_response(
            500,
            "DATABASE_ERROR",
            "Database operation failed",
        )

    except Exception:

        if connection:
            connection.rollback()

        logger.exception(
            "Order request failed"
        )

        return error_response(
            500,
            "INTERNAL_SERVER_ERROR",
            "Unable to process order request",
        )

    finally:

        if connection:

            try:
                connection.close()
            except Exception:
                logger.exception(
                    "Failed to close database connection"
                )
