import json
import os
import logging
from decimal import Decimal

import boto3
import pymysql


logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")
events = boto3.client("events")


# ============================================================
# RESPONSE
# ============================================================

def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS"
        },
        "body": json.dumps(
            body,
            default=lambda x: float(x)
            if isinstance(x, Decimal)
            else x
        )
    }


# ============================================================
# SSM
# ============================================================

def get_parameter(name, decrypt=False):
    result = ssm.get_parameter(
        Name=name,
        WithDecryption=decrypt
    )

    return result["Parameter"]["Value"]


# ============================================================
# RDS CONNECTION
# ============================================================

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
        os.environ["DB_USERNAME_PARAMETER_NAME"]
    )

    password = get_parameter(
        os.environ["DB_PASSWORD_PARAMETER_NAME"],
        decrypt=True
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
        autocommit=False
    )


# ============================================================
# INVENTORY VALIDATION
#
# Inventory is stored directly in PRODUCTS.
# ============================================================

def validate_inventory(stock, threshold):

    try:
        stock = int(stock)
        threshold = int(threshold)
    except (TypeError, ValueError):

        raise ValueError(
            "stock_quantity and low_stock_threshold "
            "must be integers"
        )

    if stock < 0:
        raise ValueError(
            "stock_quantity cannot be negative"
        )

    if threshold < 0:
        raise ValueError(
            "low_stock_threshold cannot be negative"
        )

    return stock, threshold


# ============================================================
# EVENTBRIDGE
# ============================================================

def publish_inventory_event(
    product_id,
    product_name,
    old_stock,
    new_stock,
    threshold
):

    low_stock = new_stock <= threshold

    event_detail = {
        "product_id": int(product_id),
        "product_name": product_name,
        "old_stock": int(old_stock),
        "new_stock": int(new_stock),
        "low_stock_threshold": int(threshold),
        "low_stock": low_stock
    }

    try:

        result = events.put_events(
            Entries=[
                {
                    "Source": "cloudmart.product",
                    "DetailType": "Inventory Changed",
                    "EventBusName": os.environ.get(
                        "EVENT_BUS_NAME",
                        "default"
                    ),
                    "Detail": json.dumps(event_detail)
                }
            ]
        )

        if result.get("FailedEntryCount", 0) > 0:

            logger.error(
                "EventBridge failed: %s",
                json.dumps(result)
            )

            return False

        logger.info(
            "Inventory event published: %s",
            json.dumps(event_detail)
        )

        return True

    except Exception:

        logger.exception(
            "Unable to publish inventory event"
        )

        return False


# ============================================================
# GET ALL PRODUCTS
# ============================================================

def get_products(cursor):

    cursor.execute("""
        SELECT
            product_id,
            category_id,
            name,
            description,
            price,
            stock_quantity,
            low_stock_threshold,
            CASE
                WHEN stock_quantity <= low_stock_threshold
                THEN 'LOW_STOCK'
                ELSE 'IN_STOCK'
            END AS inventory_status,
            status,
            created_at,
            updated_at
        FROM products
        WHERE deleted_at IS NULL
        ORDER BY product_id
    """)

    return cursor.fetchall()


# ============================================================
# GET PRODUCT
# ============================================================

def get_product(cursor, product_id):

    cursor.execute("""
        SELECT
            product_id,
            category_id,
            name,
            description,
            price,
            stock_quantity,
            low_stock_threshold,
            CASE
                WHEN stock_quantity <= low_stock_threshold
                THEN 'LOW_STOCK'
                ELSE 'IN_STOCK'
            END AS inventory_status,
            status,
            created_at,
            updated_at
        FROM products
        WHERE product_id = %s
          AND deleted_at IS NULL
    """, (product_id,))

    return cursor.fetchone()


# ============================================================
# CREATE PRODUCT
# ============================================================

def create_product(cursor, payload):

    required = [
        "name",
        "price",
        "stock_quantity",
        "low_stock_threshold"
    ]

    missing = [
        field
        for field in required
        if field not in payload
    ]

    if missing:

        raise ValueError(
            "Missing required fields: "
            + ", ".join(missing)
        )

    stock, threshold = validate_inventory(
        payload["stock_quantity"],
        payload["low_stock_threshold"]
    )

    cursor.execute("""
        INSERT INTO products (
            category_id,
            name,
            description,
            price,
            stock_quantity,
            low_stock_threshold,
            status
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s
        )
    """, (
        payload.get("category_id"),
        payload["name"],
        payload.get("description"),
        payload["price"],
        stock,
        threshold,
        payload.get("status", "ACTIVE")
    ))

    product_id = cursor.lastrowid

    return product_id


# ============================================================
# UPDATE PRODUCT / INVENTORY
# ============================================================

def update_product(cursor, product_id, payload):

    cursor.execute("""
        SELECT
            product_id,
            name,
            price,
            stock_quantity,
            low_stock_threshold,
            status
        FROM products
        WHERE product_id = %s
          AND deleted_at IS NULL
        FOR UPDATE
    """, (product_id,))

    current = cursor.fetchone()

    if not current:
        return None

    old_stock = current["stock_quantity"]

    fields = []
    values = []

    allowed_fields = [
        "category_id",
        "name",
        "description",
        "price",
        "status"
    ]

    for field in allowed_fields:

        if field in payload:

            fields.append(
                f"{field} = %s"
            )

            values.append(
                payload[field]
            )

    # --------------------------------------------------------
    # INVENTORY UPDATE
    # --------------------------------------------------------

    new_stock = current["stock_quantity"]

    if "stock_quantity" in payload:

        new_stock = int(
            payload["stock_quantity"]
        )

        if new_stock < 0:

            raise ValueError(
                "stock_quantity cannot be negative"
            )

        fields.append(
            "stock_quantity = %s"
        )

        values.append(
            new_stock
        )

    new_threshold = current[
        "low_stock_threshold"
    ]

    if "low_stock_threshold" in payload:

        new_threshold = int(
            payload["low_stock_threshold"]
        )

        if new_threshold < 0:

            raise ValueError(
                "low_stock_threshold cannot be negative"
            )

        fields.append(
            "low_stock_threshold = %s"
        )

        values.append(
            new_threshold
        )

    if not fields:

        raise ValueError(
            "No fields supplied for update"
        )

    values.append(product_id)

    cursor.execute(
        f"""
        UPDATE products
        SET
            {", ".join(fields)},
            updated_at = CURRENT_TIMESTAMP
        WHERE product_id = %s
          AND deleted_at IS NULL
        """,
        values
    )

    # Get updated product
    updated = get_product(
        cursor,
        product_id
    )

    return {
        "product": updated,
        "old_stock": old_stock,
        "new_stock": new_stock,
        "threshold": new_threshold
    }


# ============================================================
# DELETE PRODUCT
# ============================================================

def delete_product(cursor, product_id, payload):

    cursor.execute("""
        UPDATE products
        SET
            deleted_at = CURRENT_TIMESTAMP,
            deleted_by = %s,
            delete_reason = %s,
            status = 'DELETED',
            updated_at = CURRENT_TIMESTAMP
        WHERE product_id = %s
          AND deleted_at IS NULL
    """, (
        payload.get("deleted_by", "api"),
        payload.get(
            "delete_reason",
            "Deleted through API"
        ),
        product_id
    ))

    return cursor.rowcount > 0


# ============================================================
# MAIN LAMBDA
# ============================================================

def lambda_handler(event, context):

    logger.info(
        json.dumps({
            "message": "Product request received",
            "httpMethod": event.get("httpMethod"),
            "path": event.get("path")
        })
    )

    connection = None

    try:

        method = (
            event.get("httpMethod")
            or event.get("requestContext", {})
            .get("http", {})
            .get("method")
            or ""
        ).upper()

        if method == "OPTIONS":

            return response(
                200,
                {"message": "OK"}
            )

        path_parameters = (
            event.get("pathParameters")
            or {}
        )

        product_id = (
            path_parameters.get("id")
            or path_parameters.get("product_id")
        )

        raw_body = event.get("body")

        if isinstance(raw_body, str):

            payload = (
                json.loads(raw_body)
                if raw_body
                else {}
            )

        elif isinstance(raw_body, dict):

            payload = raw_body

        else:

            payload = {}

        connection = get_db_connection()

        with connection.cursor() as cursor:

            # =================================================
            # GET /products
            # =================================================

            if method == "GET" and not product_id:

                products = get_products(cursor)

                return response(
                    200,
                    {
                        "count": len(products),
                        "products": products
                    }
                )

            # =================================================
            # GET /products/{id}
            # =================================================

            if method == "GET" and product_id:

                product = get_product(
                    cursor,
                    product_id
                )

                if not product:

                    return response(
                        404,
                        {
                            "message":
                                "Product not found"
                        }
                    )

                return response(
                    200,
                    product
                )

            # =================================================
            # POST /products
            # =================================================

            if method == "POST":

                product_id = create_product(
                    cursor,
                    payload
                )

                connection.commit()

                product = get_product(
                    cursor,
                    product_id
                )

                logger.info(
                    json.dumps({
                        "message":
                            "Product created",
                        "product_id":
                            product_id
                    })
                )

                return response(
                    201,
                    product
                )

            # =================================================
            # PUT /products/{id}
            # =================================================

            if method == "PUT" and product_id:

                result = update_product(
                    cursor,
                    product_id,
                    payload
                )

                if result is None:

                    connection.rollback()

                    return response(
                        404,
                        {
                            "message":
                                "Product not found"
                        }
                    )

                connection.commit()

                # ---------------------------------------------
                # Publish inventory event ONLY when stock
                # actually changes.
                # ---------------------------------------------

                if (
                    result["old_stock"]
                    != result["new_stock"]
                ):

                    publish_inventory_event(
                        product_id,
                        result["product"]["name"],
                        result["old_stock"],
                        result["new_stock"],
                        result["threshold"]
                    )

                logger.info(
                    json.dumps({
                        "message":
                            "Product updated",
                        "product_id":
                            int(product_id),
                        "old_stock":
                            result["old_stock"],
                        "new_stock":
                            result["new_stock"],
                        "threshold":
                            result["threshold"],
                        "low_stock":
                            (
                                result["new_stock"]
                                <= result["threshold"]
                            )
                    })
                )

                return response(
                    200,
                    result["product"]
                )

            # =================================================
            # DELETE /products/{id}
            # =================================================

            if method == "DELETE" and product_id:

                deleted = delete_product(
                    cursor,
                    product_id,
                    payload
                )

                if not deleted:

                    connection.rollback()

                    return response(
                        404,
                        {
                            "message":
                                "Product not found"
                        }
                    )

                connection.commit()

                return response(
                    200,
                    {
                        "message":
                            "Product deleted",
                        "product_id":
                            product_id
                    }
                )

            return response(
                405,
                {
                    "message":
                        "Method not allowed"
                }
            )

    except json.JSONDecodeError:

        if connection:
            connection.rollback()

        logger.exception(
            "Invalid JSON body"
        )

        return response(
            400,
            {
                "message":
                    "Invalid JSON body"
            }
        )

    except ValueError as exc:

        if connection:
            connection.rollback()

        logger.warning(
            "Validation error: %s",
            str(exc)
        )

        return response(
            400,
            {
                "message":
                    str(exc)
            }
        )

    except pymysql.MySQLError as exc:

        if connection:
            connection.rollback()

        logger.exception(
            "Database operation failed"
        )

        return response(
            500,
            {
                "message":
                    "Database operation failed",
                "error":
                    str(exc)
            }
        )

    except Exception as exc:

        if connection:
            connection.rollback()

        logger.exception(
            "Product Lambda failed"
        )

        return response(
            500,
            {
                "message":
                    "Internal server error",
                "error":
                    str(exc)
            }
        )

    finally:

        if connection:

            connection.close()