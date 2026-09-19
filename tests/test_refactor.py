import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


def read(relative):
    return (ROOT / relative).read_text(encoding="utf-8")


class CloudMartRefactorTests(unittest.TestCase):
    def test_authorizer_only_validates_tokens(self):
        source = read("lambdas/authorizer/index.py")
        tree = ast.parse(source)
        function_names = {
            node.name for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertNotIn("generate_token", function_names)
        self.assertNotIn("rotate_customer_tokens", function_names)
        self.assertNotIn("initialize_tokens", function_names)
        self.assertNotIn("put_parameter", source)

    def test_product_lambda_has_no_customer_business_functions(self):
        tree = ast.parse(read("lambdas/product/index.py"))
        function_names = {
            node.name for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for name in ("create_customer", "update_customer", "delete_customer", "get_customer"):
            self.assertNotIn(name, function_names)

    def test_notification_template_is_human_readable(self):
        source = read("lambdas/notification/index.py")
        self.assertIn('html_body = f"""<!doctype html>', source)
        self.assertIn("<table", source)
        self.assertIn("Order Details", source)
        self.assertIn("Items", source)
        self.assertIn('Body": {', source)
        self.assertIn('"Html": {"Data": html_body', source)
        self.assertIn('"Text": {"Data": text_body', source)
        self.assertIn("html.escape", source)
        self.assertNotIn("json.dumps(detail)", source.split("def build_message", 1)[1].split("def lambda_handler", 1)[0])

    def test_subscription_state_is_only_changed_by_unsubscribe_flow(self):
        customer = read("lambdas/customer/index.py")
        notification = read("lambdas/notification/index.py")
        self.assertIn("status = 'UNSUBSCRIBED'", customer)
        self.assertIn("reason=USER_UNSUBSCRIBE", customer)
        self.assertIn("subscription state is not modified", notification)
        self.assertNotIn("status = 'UNSUBSCRIBED'", notification)

    def test_authorizer_role_can_read_auth_token_parameter(self):
        iam = read("cloudformation/iam-stack.yaml")
        auth_parameter_arn = (
            "parameter/cloudmart/${Environment}/auth/token"
        )
        self.assertIn(auth_parameter_arn, iam)

    def test_infrastructure_contains_new_components_and_order_events(self):
        app = read("cloudformation/application-stack.yaml")
        self.assertIn("CustomerFunction:", app)
        self.assertIn("TokenManagerFunction:", app)
        self.assertIn("NotificationFunction:", app)
        for event_name in (
            "OrderPlaced",
            "OrderConfirmed",
            "OrderCanceled",
            "OrderFailed",
            "OrderCompleted",
        ):
            self.assertIn(f"detail-type: [{event_name}]", app)

        self.assertNotIn("CloudMartCustomerOrderNotificationTopic:", app)

    def test_subscription_schema_exists(self):
        schema = read("database/schema.sql")
        self.assertIn("CREATE TABLE IF NOT EXISTS email_subscriptions", schema)
        self.assertIn("UNSUBSCRIBED", schema)


if __name__ == "__main__":
    unittest.main()
