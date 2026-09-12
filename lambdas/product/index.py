import json
import os
import re
import logging
from decimal import Decimal
from pathlib import Path
from datetime import date, datetime

import boto3
import pymysql


logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")
events = boto3.client("events")


def log_json(level, message, **fields):
    record = {"message": message, **fields}
    getattr(logger, level)(json.dumps(record, default=json_serializer))


# ============================================================
# RESPONSE
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
            "Access-Control-Allow-Methods": "GET,POST,PUT,PATCH,DELETE,OPTIONS"
        },
        "body": json.dumps(
            body,
            default=json_serializer
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
        os.environ["DB_USERNAME_PARAMETER_NAME"],
        decrypt=True
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
# ONE-TIME DATABASE SCHEMA INITIALIZATION
#
# Uses the existing database/schema.sql copied into the Lambda
# deployment package by GitHub Actions.
#
# Invoke directly with:
# {"action": "initialize_database"}
#
# Do NOT expose this as a normal API operation.
# ============================================================

def initialize_database(connection):

    schema_path = Path(__file__).with_name("schema.sql")

    if not schema_path.exists():
        raise FileNotFoundError(
            "schema.sql was not included in the Product Lambda package"
        )

    schema = schema_path.read_text(encoding="utf-8")

    # The Lambda is already connected to the cloudmart database.
    # These two statements are therefore unnecessary.
    cleaned_lines = []

    for line in schema.splitlines():

        stripped = line.strip()
        upper = stripped.upper()

        if upper.startswith("CREATE DATABASE"):
            continue

        if upper.startswith("USE CLOUDMART"):
            continue

        cleaned_lines.append(line)

    schema = "\n".join(cleaned_lines)

    # Remove SQL comments before splitting statements.
    schema = re.sub(r"/\*.*?\*/", "", schema, flags=re.DOTALL)
    schema = re.sub(r"--[^\n]*", "", schema)
    schema = re.sub(r"#[^\n]*", "", schema)

    statements = [
        statement.strip()
        for statement in schema.split(";")
        if statement.strip()
    ]

    executed = 0

    with connection.cursor() as cursor:

        for statement in statements:

            # Do not run the SELECT-only verification statements.
            if statement.upper().startswith("SELECT"):
                continue

            cursor.execute(statement)
            executed += 1

    connection.commit()

    return {
        "message": "Database schema applied successfully",
        "statements_executed": executed
    }


# ============================================================
# AUTOMATIC DATABASE SCHEMA CHECK
#
# The deployment workflow packages schema.sql with this Lambda,
# but packaging the file does NOT execute it against RDS.
#
# On the first Product API request, check for the products table.
# If it does not exist, apply the packaged schema automatically.
# This removes the need for any manual MySQL commands.
# ============================================================

def ensure_database_schema(connection):
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT COUNT(*)
            FROM information_schema.tables
            WHERE table_schema = DATABASE()
              AND table_name = 'products'
        """)
        exists = cursor.fetchone()["COUNT(*)"] > 0

    if exists:
        return False

    logger.info(
        json.dumps({
            "message": "Products table is missing; applying packaged schema automatically"
        })
    )

    result = initialize_database(connection)

    logger.info(
        json.dumps({
            "message": "Automatic database schema initialization completed",
            "statements_executed": result["statements_executed"]
        })
    )

    return True


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
# PRICE VALIDATION
# ============================================================

def validate_price(price):

    try:
        price = Decimal(str(price))
    except (TypeError, ValueError, ArithmeticError):
        raise ValueError(
            "price must be a valid number"
        )

    if price <= 0:
        raise ValueError(
            "price must be greater than 0"
        )

    return price


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
                    "EventBusName": os.environ["EVENT_BUS_NAME"],
                    "Detail": json.dumps(event_detail)
                }
            ]
        )

        if result.get("FailedEntryCount", 0) > 0:

            log_json("error", "EventBridge failed", result=result)

            return False

        log_json("info", "Inventory event published", **event_detail)

        return True

    except Exception:

        log_json("error", "Unable to publish inventory event")

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
        "category_id",
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

    price = validate_price(
        payload["price"]
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
        price,
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

            if field == "price":
                values.append(
                    validate_price(payload["price"])
                )
            else:
                values.append(
                    payload[field]
                )

            fields.append(
                f"{field} = %s"
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

    # Soft delete: keep the row in RDS, but mark the product INACTIVE.
    # This preserves the product for audit/history while removing it
    # from normal product GET/list results (which use deleted_at IS NULL).
    cursor.execute("""
        UPDATE products
        SET
            status = 'INACTIVE',
            deleted_at = CURRENT_TIMESTAMP,
            deleted_by = %s,
            delete_reason = %s,
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
# AUTHORIZATION / RBAC
#
# API Gateway already enforces the ADMIN/CUSTOMER route policy
# through the TOKEN authorizer. This second check protects the
# Product Lambda itself if it is invoked directly.
#
# ADMIN:
#   GET /products
#   POST /products
#   GET /products/{id}
#   PUT /products/{id}
#   DELETE /products/{id}
#
# CUSTOMER:
#   GET /products
#   GET /products/{id}
#
# ============================================================

def get_authorization_context(event):
    request_context = event.get("requestContext") or {}
    authorizer = request_context.get("authorizer") or {}

    role = (
        authorizer.get("role")
        or authorizer.get("Role")
        or ""
    ).upper()

    return {
        "role": role,
        "email": authorizer.get("email"),
        "customer_id": authorizer.get("customer_id")
    }


def require_admin(event):
    auth = get_authorization_context(event)

    if auth["role"] != "ADMIN":
        raise PermissionError(
            "ADMIN role is required for this operation"
        )

    return auth


# ============================================================
# CUSTOMER API
#
# Customer endpoints are handled by this existing Product Lambda
# so the repository keeps its existing Lambda files unchanged.
# API Gateway routes /customers to this Lambda.
# ============================================================

def get_customer(cursor, customer_id):
    cursor.execute("""
        SELECT customer_id, customer_name, customer_email, created_at
        FROM customers
        WHERE customer_id = %s
    """, (customer_id,))
    return cursor.fetchone()


def create_customer(cursor, payload):
    customer_name = payload.get("customer_name", payload.get("name"))
    customer_email = payload.get("customer_email", payload.get("email"))

    if not customer_name:
        raise ValueError("customer_name is required")
    if not customer_email:
        raise ValueError("customer_email is required")

    cursor.execute("""
        INSERT INTO customers (customer_name, customer_email)
        VALUES (%s, %s)
    """, (customer_name, customer_email))

    return cursor.lastrowid


def update_customer(cursor, customer_id, payload):
    current = get_customer(cursor, customer_id)
    if not current:
        return None

    fields = []
    values = []

    customer_name = payload.get("customer_name", payload.get("name"))
    customer_email = payload.get("customer_email", payload.get("email"))

    if customer_name is not None:
        if not str(customer_name).strip():
            raise ValueError("customer_name cannot be empty")
        fields.append("customer_name = %s")
        values.append(customer_name)

    if customer_email is not None:
        if not str(customer_email).strip():
            raise ValueError("customer_email cannot be empty")
        fields.append("customer_email = %s")
        values.append(customer_email)

    if not fields:
        raise ValueError("No fields supplied for update")

    values.append(customer_id)
    cursor.execute(
        f"UPDATE customers SET {', '.join(fields)} WHERE customer_id = %s",
        values
    )

    if customer_email is not None:
        cursor.execute("""
            UPDATE orders
            SET customer_email = %s
            WHERE customer_id = %s
        """, (customer_email, customer_id))

    return get_customer(cursor, customer_id)


def delete_customer(cursor, customer_id):
    current = get_customer(cursor, customer_id)
    if not current:
        return None

    cursor.execute("""
        SELECT COUNT(*) AS order_count
        FROM orders
        WHERE customer_id = %s
    """, (customer_id,))
    order_count = int(cursor.fetchone()["order_count"])

    if order_count > 0:
        raise ValueError(
            f"Customer {customer_id} cannot be deleted because orders exist"
        )

    cursor.execute(
        "DELETE FROM customer_tokens WHERE customer_id = %s",
        (customer_id,)
    )
    cursor.execute(
        "DELETE FROM customers WHERE customer_id = %s",
        (customer_id,)
    )

    return current


# ============================================================
# MAIN LAMBDA
# ============================================================

def lambda_handler(event, context):

    log_json(
        "info",
        "Product request received",
        httpMethod=event.get("httpMethod"),
        path=event.get("path"),
        requestId=getattr(context, "aws_request_id", None)
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

        # ---------------------------------------------------------
        # ONE-TIME DATABASE INITIALIZATION
        #
        # This is a direct Lambda invocation action only.
        # It must be handled BEFORE the normal schema check so that
        # an explicit initialization does not perform an unnecessary
        # products-table lookup first.
        # ---------------------------------------------------------

        if event.get("action") == "initialize_database":

            logger.info(
                json.dumps({
                    "message": "Database initialization requested"
                })
            )

            result = initialize_database(connection)

            return response(200, result)

        # ---------------------------------------------------------
        # AUTOMATIC DATABASE INITIALIZATION
        #
        # For normal Product API requests, automatically apply the
        # packaged schema if the products table is missing.
        # ---------------------------------------------------------

        ensure_database_schema(connection)

        with connection.cursor() as cursor:

            # =================================================
            # CUSTOMER APIs
            # =================================================
            request_path = str(event.get("path") or "")

            if request_path.startswith("/customers"):
                auth = get_authorization_context(event)
                customer_id = (
                    path_parameters.get("id")
                    or path_parameters.get("customer_id")
                )

                if customer_id is None:
                    if method != "POST":
                        return response(405, {"message": "Method not allowed"})

                    require_admin(event)
                    created_id = create_customer(cursor, payload)
                    connection.commit()
                    return response(201, get_customer(cursor, created_id))

                try:
                    customer_id = int(customer_id)
                except (TypeError, ValueError):
                    raise ValueError("customer id must be an integer")

                if method == "GET":
                    if auth["role"] == "CUSTOMER":
                        token_customer_id = auth.get("customer_id")
                        if token_customer_id is None or int(token_customer_id) != customer_id:
                            raise PermissionError(
                                "Customers can access only their own customer record"
                            )
                    elif auth["role"] != "ADMIN":
                        raise PermissionError("Valid CUSTOMER or ADMIN role is required")

                    customer = get_customer(cursor, customer_id)
                    if not customer:
                        return response(404, {"message": "Customer not found"})
                    return response(200, customer)

                if method == "PUT":
                    require_admin(event)
                    customer = update_customer(cursor, customer_id, payload)
                    if customer is None:
                        connection.rollback()
                        return response(404, {"message": "Customer not found"})
                    connection.commit()
                    return response(200, customer)

                if method == "DELETE":
                    require_admin(event)
                    deleted = delete_customer(cursor, customer_id)
                    if deleted is None:
                        connection.rollback()
                        return response(404, {"message": "Customer not found"})
                    connection.commit()
                    return response(200, {
                        "message": "Customer deleted successfully",
                        "customer_id": customer_id
                    })

                return response(405, {"message": "Method not allowed"})

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

                require_admin(event)

                product_id = create_product(
                    cursor,
                    payload
                )

                connection.commit()

                product = get_product(
                    cursor,
                    product_id
                )

                log_json("info", "Product created", product_id=int(product_id))

                return response(
                    201,
                    product
                )

            # =================================================
            # PUT /products/{id}
            # =================================================

            if method == "PUT" and product_id:

                require_admin(event)

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

                log_json(
                    "info",
                    "Product updated",
                    product_id=int(product_id),
                    old_stock=int(result["old_stock"]),
                    new_stock=int(result["new_stock"]),
                    threshold=int(result["threshold"]),
                    low_stock=(result["new_stock"] <= result["threshold"])
                )

                return response(
                    200,
                    result["product"]
                )

            # =================================================
            # DELETE /products/{id}
            # =================================================

            if method == "DELETE" and product_id:

                require_admin(event)

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
                            "Product soft-deleted successfully",
                        "product_id":
                            product_id,
                        "status":
                            "INACTIVE"
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

        log_json("error", "Invalid JSON body")

        return response(
            400,
            {
                "message":
                    "Invalid JSON body"
            }
        )

    except PermissionError as exc:

        if connection:
            connection.rollback()

        log_json("warning", "Authorization denied", error=str(exc))

        return response(
            403,
            {
                "message":
                    str(exc)
            }
        )

    except ValueError as exc:

        if connection:
            connection.rollback()

        log_json("warning", "Validation error", error=str(exc))

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

        log_json("error", "Database operation failed", error=str(exc))

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

        log_json("error", "Product Lambda failed", error=str(exc))

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