-- ============================================================
-- CLOUDMART RDS MYSQL SCHEMA
-- ============================================================

CREATE DATABASE IF NOT EXISTS cloudmart;

USE cloudmart;


-- ============================================================
-- 1. CUSTOMERS
-- ============================================================

CREATE TABLE IF NOT EXISTS customers (
    customer_id BIGINT NOT NULL AUTO_INCREMENT,
    name VARCHAR(255) NOT NULL,
    email VARCHAR(255) NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (customer_id),
    UNIQUE KEY uk_customers_email (email)

) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 2. CATEGORIES
-- ============================================================

CREATE TABLE IF NOT EXISTS categories (
    category_id BIGINT NOT NULL AUTO_INCREMENT,
    category_name VARCHAR(255) NOT NULL,
    description TEXT,
    status VARCHAR(50) NOT NULL DEFAULT 'ACTIVE',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (category_id)

) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 3. PRODUCTS
-- ============================================================

CREATE TABLE IF NOT EXISTS products (
    product_id BIGINT NOT NULL AUTO_INCREMENT,
    category_id BIGINT NOT NULL,

    name VARCHAR(255) NOT NULL,
    description TEXT,

    price DECIMAL(10,2) NOT NULL,

    -- Inventory is stored in PRODUCTS
    stock_quantity INT NOT NULL DEFAULT 0,
    low_stock_threshold INT NOT NULL DEFAULT 5,

    status VARCHAR(50) NOT NULL DEFAULT 'ACTIVE',

    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    deleted_at DATETIME NULL,
    deleted_by VARCHAR(255) NULL,
    delete_reason VARCHAR(500) NULL,

    PRIMARY KEY (product_id),

    INDEX idx_products_category_id (category_id),
    INDEX idx_products_status (status),
    INDEX idx_products_stock (stock_quantity),
    INDEX idx_products_low_stock (
        stock_quantity,
        low_stock_threshold
    ),

    CONSTRAINT fk_products_category
        FOREIGN KEY (category_id)
        REFERENCES categories(category_id)

) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 4. ORDERS
-- ============================================================

CREATE TABLE IF NOT EXISTS orders (
    order_id BIGINT NOT NULL AUTO_INCREMENT,
    customer_id BIGINT NOT NULL,

    status VARCHAR(50) NOT NULL DEFAULT 'PENDING',

    total_amount DECIMAL(10,2) NOT NULL DEFAULT 0.00,

    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,

    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    PRIMARY KEY (order_id),

    INDEX idx_orders_customer_id (customer_id),
    INDEX idx_orders_status (status),

    CONSTRAINT fk_orders_customer
        FOREIGN KEY (customer_id)
        REFERENCES customers(customer_id)

) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 5. ORDER STATUS HISTORY
-- ============================================================

CREATE TABLE IF NOT EXISTS order_status_history (
    history_id BIGINT NOT NULL AUTO_INCREMENT,
    order_id BIGINT NOT NULL,

    old_status VARCHAR(50),
    new_status VARCHAR(50) NOT NULL,

    changed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,

    changed_by VARCHAR(255),

    PRIMARY KEY (history_id),

    INDEX idx_order_status_history_order_id (order_id),

    CONSTRAINT fk_order_status_history_order
        FOREIGN KEY (order_id)
        REFERENCES orders(order_id)
        ON DELETE CASCADE

) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 6. ORDER ITEMS
-- ============================================================

CREATE TABLE IF NOT EXISTS order_items (
    order_item_id BIGINT NOT NULL AUTO_INCREMENT,

    order_id BIGINT NOT NULL,
    product_id BIGINT NOT NULL,

    quantity INT NOT NULL,

    unit_price DECIMAL(10,2) NOT NULL,

    subtotal DECIMAL(10,2) NOT NULL,

    PRIMARY KEY (order_item_id),

    INDEX idx_order_items_order_id (order_id),
    INDEX idx_order_items_product_id (product_id),

    CONSTRAINT fk_order_items_order
        FOREIGN KEY (order_id)
        REFERENCES orders(order_id)
        ON DELETE CASCADE,

    CONSTRAINT fk_order_items_product
        FOREIGN KEY (product_id)
        REFERENCES products(product_id)

) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- SAMPLE CATEGORIES
-- ============================================================

INSERT INTO categories (
    category_name,
    description,
    status
)
SELECT
    'Electronics',
    'Electronic products',
    'ACTIVE'
WHERE NOT EXISTS (
    SELECT 1
    FROM categories
    WHERE category_name = 'Electronics'
);


INSERT INTO categories (
    category_name,
    description,
    status
)
SELECT
    'Accessories',
    'Computer and mobile accessories',
    'ACTIVE'
WHERE NOT EXISTS (
    SELECT 1
    FROM categories
    WHERE category_name = 'Accessories'
);


-- ============================================================
-- SAMPLE CUSTOMERS
-- ============================================================

INSERT INTO customers (
    name,
    email
)
SELECT
    'CloudMart Test Customer',
    'customer@cloudmart.com'
WHERE NOT EXISTS (
    SELECT 1
    FROM customers
    WHERE email = 'customer@cloudmart.com'
);


-- ============================================================
-- SAMPLE PRODUCTS
-- ============================================================

INSERT INTO products (
    category_id,
    name,
    description,
    price,
    stock_quantity,
    low_stock_threshold,
    status
)
SELECT
    category_id,
    'CloudMart Laptop',
    'CloudMart sample laptop',
    899.99,
    25,
    5,
    'ACTIVE'
FROM categories
WHERE category_name = 'Electronics'
  AND NOT EXISTS (
      SELECT 1
      FROM products
      WHERE name = 'CloudMart Laptop'
  )
LIMIT 1;


INSERT INTO products (
    category_id,
    name,
    description,
    price,
    stock_quantity,
    low_stock_threshold,
    status
)
SELECT
    category_id,
    'CloudMart Mouse',
    'CloudMart wireless mouse',
    29.99,
    15,
    5,
    'ACTIVE'
FROM categories
WHERE category_name = 'Accessories'
  AND NOT EXISTS (
      SELECT 1
      FROM products
      WHERE name = 'CloudMart Mouse'
  )
LIMIT 1;


INSERT INTO products (
    category_id,
    name,
    description,
    price,
    stock_quantity,
    low_stock_threshold,
    status
)
SELECT
    category_id,
    'CloudMart Keyboard',
    'CloudMart mechanical keyboard',
    79.99,
    3,
    5,
    'ACTIVE'
FROM categories
WHERE category_name = 'Accessories'
  AND NOT EXISTS (
      SELECT 1
      FROM products
      WHERE name = 'CloudMart Keyboard'
  )
LIMIT 1;


-- ============================================================
-- VERIFICATION
-- ============================================================

SELECT 'CUSTOMERS' AS table_name, COUNT(*) AS row_count
FROM customers

UNION ALL

SELECT 'CATEGORIES', COUNT(*)
FROM categories

UNION ALL

SELECT 'PRODUCTS', COUNT(*)
FROM products

UNION ALL

SELECT 'ORDERS', COUNT(*)
FROM orders

UNION ALL

SELECT 'ORDER_ITEMS', COUNT(*)
FROM order_items

UNION ALL

SELECT 'ORDER_STATUS_HISTORY', COUNT(*)
FROM order_status_history;


-- ============================================================
-- VERIFY PRODUCT INVENTORY
-- ============================================================

SELECT
    product_id,
    name,
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
ORDER BY product_id;