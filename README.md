# CloudMart

CloudMart is an AWS-hosted e-commerce application. The application uses
API Gateway and AWS Lambda for the API layer, Amazon RDS for MySQL for
application data, Amazon S3 for generated reports, AWS Systems Manager
Parameter Store for runtime configuration and protected values, EventBridge
for application events and scheduled processing, SNS for notifications,
CloudWatch for observability, and an EC2-hosted Operations Dashboard for
administration.

## Key Features

- Token-based authentication and authorization
- Product management
- Product inventory management
- Customer management
- Order creation and management
- Order status lifecycle management
- Order cancellation and inventory restoration
- Event-driven order processing
- Admin Operations Dashboard
- Daily sales/operations reporting
- S3-based report storage
- Event-driven notifications
- Low-stock notifications
- CloudWatch monitoring and alarms
- Environment-specific AWS deployments

## Prerequisites

- AWS account
- GitHub repository with GitHub Actions enabled
- AWS IAM role configured for GitHub OIDC
- AWS CLI
- Python
- Git
- Required GitHub repository/environment secrets

The infrastructure is defined with AWS CloudFormation and deployed
through GitHub Actions using AWS authentication through OIDC.

> **API invoke URL:** `<https://<api-id>.execute-api.<region>.amazonaws.com/<environment>>`

> **Dashboard URL:** `<EC2-DASHBOARD-URL>`

> **AWS Region:** `<AWS-REGION>`

> **Environment:** `dev`, `test`, or `prod` as configured for the
> deployment.

The EC2 Operations Dashboard authenticates with the administrator token and
uses the protected dashboard/API resources. The dashboard also reads
generated daily reports from S3.

------------------------------------------------------------------------

## AWS Services

| Service | Purpose |
|---|---|
| Amazon API Gateway | HTTP API entry point |
| AWS Lambda | Application and processing logic |
| Lambda Authorizer | Token validation and authorization |
| Token Manager Lambda | Authentication token initialization and management |
| Amazon RDS MySQL | Application database |
| Amazon S3 | Generated daily reports |
| AWS Systems Manager Parameter Store | Runtime configuration and protected parameters |
| Amazon EventBridge | Application events and scheduled daily reporting |
| Amazon SNS | Order, low-stock, and monitoring notifications |
| Amazon EC2 | Operations Dashboard |
| Amazon CloudWatch | Logs, custom metrics, dashboards, and alarms |
| AWS IAM | Service roles and permissions |
| AWS VPC | Network isolation and private application connectivity |
| AWS CloudFormation | Infrastructure deployment |
| GitHub Actions | CI/CD deployment |

------------------------------------------------------------------------

## CloudFormation and Deployment Stacks

The deployment uses application resources together with monitoring and
supporting infrastructure stacks.

| Stack / Template | Responsibility |
|---|---|
| `cloudformation/application-stack.yaml` | API Gateway, authorizer, token manager, product, customer, order, notification, daily report, alarm notification, EventBridge, SNS, and related application resources |
| `cloudformation/monitoring-stack.yaml` | CloudWatch dashboard, alarms, monitoring SNS, and monitoring integrations |
| `cloudformation/network-stack.yaml` | VPC, subnets, route tables, security groups, and VPC endpoints |
| `cloudformation/iam-stack.yaml` | IAM roles and permissions required by the application |
| `cloudformation/data-stack.yaml` | S3 storage and RDS MySQL database resources |

The GitHub Actions workflow deploys the required infrastructure and
application resources and updates the EC2 Operations Dashboard as part of
the deployment process.

------------------------------------------------------------------------

## Repository Layout

```text
CloudMart/
├── .github/
│   └── workflows/
│       └── deploy.yaml
├── cloudformation/
│   ├── application-stack.yaml
│   ├── monitoring-stack.yaml
│   ├── network-stack.yaml
│   ├── iam-stack.yaml
│   └── data-stack.yaml
├── lambdas/
│   ├── authorizer/
│   ├── token-manager/
│   ├── product/
│   ├── customer/
│   ├── order/
│   ├── notification/
│   └── report/
├── dashboard/
└── README.md
```

The Alarm Notification Lambda is deployed as part of the monitoring/application
resources according to the CloudFormation configuration.

------------------------------------------------------------------------

# Authentication and Authorization

CloudMart uses an API Gateway Lambda Authorizer for protected API
requests.

### Token authentication

The Token Manager Lambda initializes and manages the authentication tokens
used by the application.

The Authorizer Lambda validates the token supplied in the `Authorization`
header before allowing access to protected APIs.

```http
Authorization: Bearer <token>
```

An invalid or missing token results in:

```text
401 Unauthorized
```

A valid token allows the request to continue to the corresponding
application Lambda.

The authentication configuration is stored in SSM Parameter Store.

------------------------------------------------------------------------

# API Endpoints

All routes below are the routes provided by the CloudMart application.

The API base URL is:

```text
<API_INVOKE_URL>
```

For example:

```text
<API_INVOKE_URL>/products
```

Use the actual deployed API Gateway invoke URL in place of
`<API_INVOKE_URL>`.

------------------------------------------------------------------------

# Product APIs

## Admin API Calls

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/products` | Create product |
| GET | `/products` | View all products |
| GET | `/products/{productId}` | View product |
| PUT | `/products/{productId}` | Update product |
| DELETE | `/products/{productId}` | Delete/deactivate product |

## Customer API Calls

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/products` | View products |
| GET | `/products/{productId}` | View product |

Customers cannot create, update, or delete products.

------------------------------------------------------------------------

# Customer APIs

## Admin API Calls

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/customers/{customerId}` | View customer |
| PUT | `/customers/{customerId}` | Update customer |
| DELETE | `/customers/{customerId}` | Deactivate customer |

## Customer API Calls

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/customers` | Register customer |
| GET | `/customers/{customerId}` | View customer |
| PUT | `/customers/{customerId}` | Update customer |
| DELETE | `/customers/{customerId}` | Deactivate customer |

`POST /customers` is used for customer registration according to the
application implementation.

------------------------------------------------------------------------

# Order APIs

## Admin API Calls

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/orders` | Create order |
| GET | `/orders/{orderId}` | View order |
| GET | `/orders?customerId={customerId}` | View customer orders |
| PUT | `/orders/{orderId}` | Update order status |
| PATCH | `/orders/{orderId}` | Update order items |
| PATCH | `/orders/{orderId}/cancel` | Cancel order |

## Customer API Calls

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/orders` | Place an order |
| GET | `/orders/{orderId}` | View own order |
| GET | `/orders?customerId={customerId}` | View own orders |
| PATCH | `/orders/{orderId}` | Update order items |
| PATCH | `/orders/{orderId}/cancel` | Cancel order |

Customer order access is restricted to the authenticated customer's own
orders. Admin access is based on the administrator authorization token.

------------------------------------------------------------------------

# Dashboard APIs

## Admin API Calls

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/dashboard/products` | View product and inventory information |
| GET | `/dashboard/orders` | View recent orders |
| GET | `/dashboard/customers` | View customer information |

The dashboard is an admin/operations interface.

------------------------------------------------------------------------

# EC2 Operations Dashboard

The EC2 Operations Dashboard provides an administrative view of CloudMart.

The dashboard:

1. Authenticates the administrator.
2. Calls the dashboard product API.
3. Calls the dashboard order API.
4. Displays product inventory information.
5. Displays recent orders.
6. Displays order and operational information.
7. Displays generated daily reports.
8. Provides access to reports stored in S3.

The dashboard is deployed in the Monitoring Public Subnet.

------------------------------------------------------------------------

# Daily Reports

The Daily Report Lambda is scheduled through EventBridge.

The reporting flow is:

```text
EventBridge Schedule
        |
        v
Daily Report Lambda
        |
        v
RDS MySQL
        |
        v
Generate CSV
        |
        v
S3 Reports Bucket
        |
        v
EC2 Operations Dashboard
```

The Daily Report Lambda retrieves the required data from RDS MySQL,
generates a CSV report, and stores the report in the configured S3 report
bucket.

The dashboard can use the generated report stored in S3 for operational
visibility.

------------------------------------------------------------------------

# EventBridge and Notifications

Application events are published and processed through EventBridge.

Order-related events include the configured order lifecycle events such
as:

```text
OrderPlaced
OrderConfirmed
OrderCanceled
OrderFailed
```

EventBridge rules route matching events to the configured targets.

SNS is used for:

- Order notifications
- Low-stock notifications
- Monitoring/alarm notifications

### Order Notifications

The order notification flow is:

```text
Order Lambda
     |
     v
EventBridge
     |
     v
Notification Lambda
     |
     v
SNS
     |
     v
Customer
```

### Low-Stock Notifications

The low-stock notification flow is:

```text
Order / Inventory Processing
          |
          v
      EventBridge
          |
          v
 Notification Lambda
          |
          v
          SNS
          |
          v
 Admin / Operations Team
```

------------------------------------------------------------------------

# Monitoring and Observability

CloudMart uses CloudWatch for application and infrastructure monitoring.

The monitoring stack provides CloudWatch metrics, dashboards, logs, and
alarms.

Custom application monitoring includes metrics such as:

```text
OrdersPlaced
OrdersFailed
LowStockEvents
```

The monitoring dashboard includes service-level information such as
API Gateway, Lambda, EC2, and RDS metrics.

Configured alarms include monitoring for areas such as:

- Low-stock events
- Failed orders
- Lambda execution errors
- RDS CPU utilization
- EC2 CPU utilization
- API Gateway 5XX errors

Alarm notifications are sent through the Alarm Notification Lambda and
Monitoring SNS topic.

------------------------------------------------------------------------

# Database

CloudMart uses Amazon RDS for MySQL.

The database contains the application's core entities, including:

```text
CUSTOMERS
PRODUCTS
INVENTORY
ORDERS
ORDER_ITEMS
CATEGORIES
```

Product inventory supports values such as stock quantity, low-stock
threshold, and inventory status.

The RDS database is deployed in private subnets and is not directly exposed
to the public internet.

Database initialization is performed through the deployed application
workflow using the Product Lambda database-initialization action rather
than through a separate database-init Lambda.

------------------------------------------------------------------------

# Configuration and Secrets

Runtime configuration is stored outside the source code.

SSM Parameter Store is used for values such as:

```text
/cloudmart/{environment}/db/password
/cloudmart/{environment}/auth/token
/cloudmart/{environment}/auth/*
```

Secret values are stored using the configured protected parameter type.

GitHub Actions uses repository/environment secrets for deployment-specific
values.

Do not store secret values directly in:

- Python source files
- YAML templates
- README files
- GitHub repository files
- CloudFormation outputs
- API examples

------------------------------------------------------------------------

# Environment Separation

CloudMart supports environment-specific deployment.

The environment is passed to CloudFormation and resource names are
parameterized using the environment.

For example:

```text
cloudmart-dev-...
cloudmart-test-...
cloudmart-prod-...
```

This keeps resources for different environments separate.

The same infrastructure templates can therefore be deployed for different
environments by changing the environment configuration.

------------------------------------------------------------------------

# GitHub Actions Deployment

The deployment workflow uses GitHub Actions with AWS OIDC authentication.

The deployment process includes:

```text
Checkout
   |
   v
Configure AWS credentials
   |
   v
Validate templates
   |
   v
Deploy network infrastructure
   |
   v
Deploy data infrastructure
   |
   v
Deploy IAM infrastructure
   |
   v
Deploy application
   |
   v
Deploy monitoring
   |
   v
Update EC2 dashboard
```

The deployment workflow uses the configured AWS deployment role rather
than storing long-lived AWS access keys in the repository.

Lambda deployment uses the configured SAM/CloudFormation deployment process
and does not require a separate manual ZIP packaging workflow.

------------------------------------------------------------------------

# Deployment Verification

After deployment, verify:

### Admin API Calls

```http
GET /dashboard/products
Authorization: Bearer <admin-token>
```

```http
GET /dashboard/orders
Authorization: Bearer <admin-token>
```

```http
GET /dashboard/customers
Authorization: Bearer <admin-token>
```

Expected result:

```text
HTTP 200
```

### Product API Calls

```http
GET /products
Authorization: Bearer <token>
```

Expected result:

```text
HTTP 200
```

### Customer API Calls

```http
GET /customers/{customerId}
Authorization: Bearer <customer-token>
```

### Order API Calls

```http
POST /orders
Authorization: Bearer <customer-token>
```

```http
GET /orders?customerId=<customer-id>
Authorization: Bearer <customer-token>
```

### Events and Notifications

Verify:

- EventBridge rules exist.
- Order events are published.
- Notification Lambda receives relevant events.
- SNS notifications are delivered.
- Low-stock notifications are triggered when the configured threshold
  is reached.

### Reports and Dashboard

Verify:

- EventBridge scheduled rule runs.
- Daily Report Lambda executes.
- CSV report is generated.
- Report is uploaded to S3.
- EC2 Operations Dashboard can access the report.

### Monitoring

Verify:

- CloudWatch logs are created.
- CloudWatch metrics are available.
- CloudWatch alarms are configured.
- Alarm Notification Lambda receives alarm events.
- Monitoring SNS notifications are delivered.

------------------------------------------------------------------------

# Teardown

Before deleting the environment:

1. Back up any required RDS data.
2. Preserve required S3 reports.
3. Verify the target environment.
4. Check CloudFormation stack dependencies.
5. Delete resources through CloudFormation rather than manually deleting
   individual resources.

Delete dependent resources/stacks in the appropriate reverse dependency
order.

Do not manually remove individual resources simply to bypass a failed
CloudFormation stack.

------------------------------------------------------------------------

# Important API Summary

## Admin

```text
Products
POST   /products
GET    /products
GET    /products/{productId}
PUT    /products/{productId}
DELETE /products/{productId}

Customers
GET    /customers/{customerId}
PUT    /customers/{customerId}
DELETE /customers/{customerId}

Orders
POST   /orders
GET    /orders/{orderId}
GET    /orders?customerId={customerId}
PUT    /orders/{orderId}
PATCH  /orders/{orderId}
PATCH  /orders/{orderId}/cancel

Dashboard
GET    /dashboard/products
GET    /dashboard/orders
GET    /dashboard/customers
```

## Customer

```text
Customer
POST   /customers
GET    /customers/{customerId}
PUT    /customers/{customerId}
DELETE /customers/{customerId}

Products
GET    /products
GET    /products/{productId}

Orders
POST   /orders
GET    /orders/{orderId}
GET    /orders?customerId={customerId}
PATCH  /orders/{orderId}
PATCH  /orders/{orderId}/cancel
```

## Authentication

```text
Token-based authentication

Authorization: Bearer <token>
```

The authentication/token-management implementation is handled by the
Token Manager Lambda and Authorizer Lambda.

## Public

```text
POST /customers
```

Customer registration is available without an existing customer token;
protected operations require a valid authorization token.
