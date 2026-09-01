import json


def lambda_handler(event, context):
    print("CloudMart Order Lambda invoked")
    print("Event:", json.dumps(event))

    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": "application/json"
        },
        "body": json.dumps({
            "message": "Order Lambda is working"
        })
    }