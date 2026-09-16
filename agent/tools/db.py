"""DynamoDB tables and external endpoint constants. Imported by everything.

Moved verbatim out of the old single-file agent/tools.py.
"""

import boto3
import os



GMAIL_LIST_API = os.environ.get(
    "GMAIL_LIST_API", 
    "https://twjvhacirvnjr2aqp5j55ws5ri0ckgtc.lambda-url.us-east-1.on.aws/API"
)
GMAIL_INSPECT_API = os.environ.get(
    "GMAIL_INSPECT_API",
    "https://va6ackelj53zk566fbpvd4riim0hsnwn.lambda-url.us-east-1.on.aws/"
)

dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
projects_table = dynamodb.Table('onebox-projects')
conversations_table = dynamodb.Table('onebox-conversations')
tasks_table = dynamodb.Table('onebox-tasks')
insights_table = dynamodb.Table('onebox-insights')


notifications_table = dynamodb.Table('onebox-notifications')
invitations_table = dynamodb.Table('onebox-invitations')
