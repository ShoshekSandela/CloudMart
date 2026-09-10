import json
import logging
import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

sns = boto3.client('sns')


def lambda_handler(event, context):
    logger.info('Received event: %s', json.dumps(event))

    # EventBridge may send the payload directly as a dict when InputTransformer is used.
    payload = event

    # If event is wrapped (string), try to parse
    if isinstance(event, str):
        try:
            payload = json.loads(event)
        except Exception:
            payload = {"message": event}

    message = payload.get('message') if isinstance(payload, dict) else str(payload)
    topic_arn = payload.get('topicArn') if isinstance(payload, dict) else None
    subject = payload.get('subject') if isinstance(payload, dict) else None

    if not topic_arn:
        logger.error('No topicArn provided in event payload')
        return {'status': 'error', 'reason': 'no_topic_arn'}

    try:
        publish_args = {'TopicArn': topic_arn, 'Message': message}
        if subject:
            publish_args['Subject'] = subject
        resp = sns.publish(**publish_args)
        logger.info('Published message to %s, messageId=%s', topic_arn, resp.get('MessageId'))
        return {'status': 'ok', 'messageId': resp.get('MessageId')}
    except Exception as e:
        logger.exception('Failed to publish to SNS')
        return {'status': 'error', 'reason': str(e)}
