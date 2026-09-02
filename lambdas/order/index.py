import json
import logging
import os
from decimal import Decimal
from datetime import date, datetime

import boto3
import pymysql


logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")
events = boto3.client("events")


# ============================================================
# RESPONSE HELPERS
# ============================================================

def json_serializer(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,POST,PUT,OPTIONS",
        },
        "body": json.dumps(body, default=json_serializer),
    }


def error_response(status_code, code, message):
    return response(
        status_code,
        {
            "error": {
                "code": code,
                "message": message,
            }
        },
    )


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


def validate_request(payload):
    if not isinstance(payload, dict):
        raise ValueError(
            "Request body must be a JSON object"
        )

    customer_id = payload.get("customer_id")

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


# ============================================================
# RDS transaction + order/order_items + inventory
# ============================================================

class StockError(Exception):
    pass


def create_order(connection, customer_id, items):
    with connection.cursor() as cursor:

        cursor.execute(
            """
            SELECT customer_id, name, email
            FROM customers
            WHERE customer_id = %s
            """,
            (customer_id,),
        )

        customer = cursor.fetchone()

        if not customer:
            raise LookupError(
                "Customer not found"
            )

        # Combine duplicate product IDs.
        quantities = {}

        for item in items:
            product_id = item["product_id"]
            quantities[product_id] = (
                quantities.get(product_id, 0)
                + item["quantity"]
            )

        order_items = []
        total_amount = Decimal("0.00")

        for product_id, quantity in quantities.items():

            cursor.execute(
                """
                SELECT
                    product_id,
                    name,
                    price,
                    stock_quantity,
                    status,
                    deleted_at
                FROM products
                WHERE product_id = %s
                """,
                (product_id,),
            )

            product = cursor.fetchone()

            if not product:
                raise LookupError(
                    f"Product {product_id} not found"
                )

            if product["deleted_at"] is not None:
                raise LookupError(
                    f"Product {product_id} is deleted"
                )

            if product["status"] != "ACTIVE":
                raise ValueError(
                    f"Product {product_id} is not active"
                )

            # Check stock before creating the order.
            if int(product["stock_quantity"]) < quantity:
                raise StockError(
                    f"Insufficient stock for product {product_id}"
                )

            unit_price = Decimal(
                str(product["price"])
            )

            subtotal = unit_price * quantity
            total_amount += subtotal

            order_items.append(
                {
                    "product_id": product_id,
                    "quantity": quantity,
                    "unit_price": unit_price,
                    "subtotal": subtotal,
                }
            )

        cursor.execute(
            """
            INSERT INTO orders (
                customer_id,
                status,
                total_amount
            )
            VALUES (%s, %s, %s)
            """,
            (
                customer_id,
                "PENDING",
                total_amount,
            ),
        )

        order_id = cursor.lastrowid

        for item in order_items:
            cursor.execute(
                """
                INSERT INTO order_items (
                    order_id,
                    product_id,
                    quantity,
                    unit_price,
                    subtotal
                )
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    order_id,
                    item["product_id"],
                    item["quantity"],
                    item["unit_price"],
                    item["subtotal"],
                ),
            )

        # Deduct inventory inside the SAME database transaction.
        # The stock check is performed again atomically so concurrent
        # orders cannot drive inventory below zero.
        for item in order_items:
            cursor.execute(
                """
                UPDATE products
                SET stock_quantity = stock_quantity - %s
                WHERE product_id = %s
                  AND stock_quantity >= %s
                """,
                (
                    item["quantity"],
                    item["product_id"],
                    item["quantity"],
                ),
            )

            if cursor.rowcount != 1:
                raise StockError(
                    f"Insufficient stock for product {item['product_id']}"
                )

        cursor.execute(
            """
            INSERT INTO order_status_history (
                order_id,
                old_status,
                new_status,
                changed_by
            )
            VALUES (%s, %s, %s, %s)
            """,
            (
                order_id,
                None,
                "PENDING",
                "order-api",
            ),
        )

        connection.commit()

        return {
            "order_id": int(order_id),
            "customer_id": int(customer_id),
            "status": "PENDING",
            "total_amount": total_amount,
            "items": order_items,
        }


def publish_order_placed_event(order):
    detail = {
        "order_id": order["order_id"],
        "customer_id": order["customer_id"],
        "status": order["status"],
        "total_amount": float(
            order["total_amount"]
        ),
        "items": [
            {
                "product_id": item["product_id"],
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
        "status": order.get("status"),
        "total_amount": float(order["total_amount"]),
        "items": [
            {
                "product_id": int(item["product_id"]),
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
    allowed_statuses = {"CONFIRMED", "CANCELED", "FAILED"}
    new_status = str(new_status).upper().strip()

    if new_status not in allowed_statuses:
        raise ValueError(
            "status must be one of: CONFIRMED, CANCELED, FAILED"
        )

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                o.order_id,
                o.customer_id,
                o.status,
                o.total_amount
            FROM orders o
            WHERE o.order_id = %s
            FOR UPDATE
            """,
            (order_id,),
        )
        existing = cursor.fetchone()

        if not existing:
            raise LookupError(f"Order {order_id} not found")

        old_status = existing["status"]

        if old_status == new_status:
            connection.commit()
            return False

        if old_status in {"CONFIRMED", "CANCELED", "FAILED"}:
            raise ValueError(
                f"Order {order_id} is already in terminal status {old_status}"
            )

        cursor.execute(
            """
            UPDATE orders
            SET status = %s
            WHERE order_id = %s
            """,
            (new_status, order_id),
        )

        cursor.execute(
            """
            INSERT INTO order_status_history (
                order_id,
                old_status,
                new_status,
                changed_by
            )
            VALUES (%s, %s, %s, %s)
            """,
            (order_id, old_status, new_status, "order-api"),
        )

    connection.commit()
    return True



# ============================================================
# PUT /orders/{id}
# ============================================================

def update_order(connection, order_id, customer_id, items):
    """
    Replace the items of an existing PENDING order.

    Inventory is adjusted by the quantity delta:
      - quantity increased  -> deduct additional stock
      - quantity decreased -> return stock
      - item removed        -> return its previous stock
      - new item            -> deduct stock

    Everything is performed in one RDS transaction so the order,
    order_items and inventory remain consistent.
    """
    with connection.cursor() as cursor:

        cursor.execute(
            """
            SELECT
                order_id,
                customer_id,
                status
            FROM orders
            WHERE order_id = %s
            FOR UPDATE
            """,
            (order_id,),
        )

        existing_order = cursor.fetchone()

        if not existing_order:
            raise LookupError(
                f"Order {order_id} not found"
            )

        if int(existing_order["customer_id"]) != int(customer_id):
            raise ValueError(
                f"Order {order_id} does not belong to customer {customer_id}"
            )

        if existing_order["status"] != "PENDING":
            raise ValueError(
                f"Order {order_id} can only be updated while status is PENDING"
            )

        requested_quantities = {}

        for item in items:
            product_id = int(item["product_id"])
            quantity = int(item["quantity"])

            requested_quantities[product_id] = (
                requested_quantities.get(product_id, 0)
                + quantity
            )

        cursor.execute(
            """
            SELECT
                order_item_id,
                product_id,
                quantity,
                unit_price
            FROM order_items
            WHERE order_id = %s
            FOR UPDATE
            """,
            (order_id,),
        )

        existing_items = cursor.fetchall()

        current_items = {
            int(item["product_id"]): item
            for item in existing_items
        }

        product_details = {}

        # Validate and lock all requested products.
        for product_id in requested_quantities:

            cursor.execute(
                """
                SELECT
                    product_id,
                    name,
                    price,
                    stock_quantity,
                    status,
                    deleted_at
                FROM products
                WHERE product_id = %s
                FOR UPDATE
                """,
                (product_id,),
            )

            product = cursor.fetchone()

            if not product:
                raise LookupError(
                    f"Product {product_id} not found"
                )

            if product["deleted_at"] is not None:
                raise LookupError(
                    f"Product {product_id} is deleted"
                )

            if product["status"] != "ACTIVE":
                raise ValueError(
                    f"Product {product_id} is not active"
                )

            product_details[product_id] = product

        # Lock removed products too because their stock is returned.
        for product_id in current_items:
            if product_id not in requested_quantities:

                cursor.execute(
                    """
                    SELECT
                        product_id,
                        stock_quantity
                    FROM products
                    WHERE product_id = %s
                    FOR UPDATE
                    """,
                    (product_id,),
                )

                product = cursor.fetchone()

                if not product:
                    raise LookupError(
                        f"Product {product_id} not found"
                    )

                product_details[product_id] = product

        # --------------------------------------------------------
        # Apply inventory deltas.
        # --------------------------------------------------------
        for product_id, old_item in current_items.items():

            old_quantity = int(old_item["quantity"])
            new_quantity = int(
                requested_quantities.get(product_id, 0)
            )

            delta = new_quantity - old_quantity

            if delta > 0:
                # Quantity increased: deduct only the difference.
                cursor.execute(
                    """
                    UPDATE products
                    SET stock_quantity = stock_quantity - %s
                    WHERE product_id = %s
                      AND stock_quantity >= %s
                    """,
                    (
                        delta,
                        product_id,
                        delta,
                    ),
                )

                if cursor.rowcount != 1:
                    raise StockError(
                        f"Insufficient stock for product {product_id}"
                    )

            elif delta < 0:
                # Quantity decreased: return the difference to inventory.
                cursor.execute(
                    """
                    UPDATE products
                    SET stock_quantity = stock_quantity + %s
                    WHERE product_id = %s
                    """,
                    (
                        abs(delta),
                        product_id,
                    ),
                )

        # New products need their full requested quantity deducted.
        for product_id, new_quantity in requested_quantities.items():

            if product_id in current_items:
                continue

            cursor.execute(
                """
                UPDATE products
                SET stock_quantity = stock_quantity - %s
                WHERE product_id = %s
                  AND stock_quantity >= %s
                """,
                (
                    new_quantity,
                    product_id,
                    new_quantity,
                ),
            )

            if cursor.rowcount != 1:
                raise StockError(
                    f"Insufficient stock for product {product_id}"
                )

        # --------------------------------------------------------
        # Update order_items.
        # Existing items keep their original unit price.
        # New items use the current product price.
        # --------------------------------------------------------
        total_amount = Decimal("0.00")

        for product_id, quantity in requested_quantities.items():

            if product_id in current_items:

                order_item_id = current_items[product_id]["order_item_id"]

                unit_price = Decimal(
                    str(current_items[product_id]["unit_price"])
                )

                subtotal = unit_price * quantity

                cursor.execute(
                    """
                    UPDATE order_items
                    SET quantity = %s,
                        subtotal = %s
                    WHERE order_item_id = %s
                      AND order_id = %s
                    """,
                    (
                        quantity,
                        subtotal,
                        order_item_id,
                        order_id,
                    ),
                )

            else:

                product = product_details[product_id]

                unit_price = Decimal(
                    str(product["price"])
                )

                subtotal = unit_price * quantity

                cursor.execute(
                    """
                    INSERT INTO order_items (
                        order_id,
                        product_id,
                        quantity,
                        unit_price,
                        subtotal
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        order_id,
                        product_id,
                        quantity,
                        unit_price,
                        subtotal,
                    ),
                )

            total_amount += unit_price * quantity

        # Remove products no longer present in the request.
        for product_id, old_item in current_items.items():

            if product_id not in requested_quantities:

                cursor.execute(
                    """
                    DELETE FROM order_items
                    WHERE order_item_id = %s
                      AND order_id = %s
                    """,
                    (
                        old_item["order_item_id"],
                        order_id,
                    ),
                )

        cursor.execute(
            """
            UPDATE orders
            SET total_amount = %s
            WHERE order_id = %s
            """,
            (
                total_amount,
                order_id,
            ),
        )

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
                c.name AS customer_name,
                c.email AS customer_email,
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
                name,
                email
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
                c.name AS customer_name,
                c.email AS customer_email,
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

def lambda_handler(event, context):

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

                return response(
                    200,
                    order,
                )

            # ------------------------------------------------
            # GET /orders?customerId=X
            # ------------------------------------------------
            customer_id = (
                get_customer_id_from_query(
                    event
                )
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
        # Normal request creates a PENDING order and publishes
        # OrderPlaced. A lifecycle request can update an existing
        # order with CONFIRMED, CANCELED or FAILED and publishes
        # the matching EventBridge event.
        # ----------------------------------------------------
        if method == "POST":

            payload = parse_body(event)

            if payload.get("status") is not None:
                try:
                    order_id = int(payload.get("order_id"))
                except (TypeError, ValueError) as exc:
                    raise ValueError("order_id must be an integer") from exc

                if order_id <= 0:
                    raise ValueError("order_id must be greater than zero")

                new_status = str(payload.get("status")).upper().strip()

                connection = get_db_connection()
                changed = update_order_status(
                    connection,
                    order_id,
                    new_status,
                )

                order = get_order_by_id(connection, order_id)

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
                    }[new_status]

                    if not publish_order_event(event_type, order):
                        logger.error(
                            "Order %s changed to %s but %s could not be published",
                            order_id,
                            new_status,
                            event_type,
                        )

                return response(200, order)

            customer_id, items = (
                validate_request(payload)
            )

            connection = get_db_connection()

            order = create_order(
                connection,
                customer_id,
                items,
            )

            if not publish_order_placed_event(order):
                logger.error(
                    "Order %s created but OrderPlaced could not be published",
                    order["order_id"],
                )

            return response(201, order)

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

            customer_id, items = validate_request(
                payload
            )

            connection = get_db_connection()

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
            "Supported methods are GET, POST, PUT, OPTIONS",
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
            "Order database operation failed"
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
