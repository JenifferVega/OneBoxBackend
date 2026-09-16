"""Shared API resources: API-owned DynamoDB tables and auth/scan helpers.

The agent's tables (projects, tasks, insights, etc.) live in agent.tools;
here we only define the tables used exclusively by the HTTP layer.
"""
import os

import boto3
from fastapi import HTTPException

dynamodb = boto3.resource('dynamodb', region_name=os.environ.get('AWS_REGION', 'us-east-1'))

attachments_table = dynamodb.Table('onebox-attachments')
user_phones_table = dynamodb.Table('onebox-user-phones')
user_tokens_table = dynamodb.Table('onebox-user-tokens')
sessions_table = dynamodb.Table('onebox-whatsapp-sessions')


def require_uid(uid_value: str) -> str:
    """Return the request's userId or raise 401 if missing.
    NEVER falls back to a default USER_ID: that fallback leaked one real
    account's data to anyone calling without authentication (cross-user leak)."""
    if not uid_value:
        raise HTTPException(status_code=401, detail="x-user-id required")
    return uid_value


def scan_all_pages(table, **scan_kwargs):
    """Perform a fully paginated Scan on a DynamoDB table.
    Required because scan() returns at most 1 MB of items and applies the
    FilterExpression AFTER reading; without pagination, items matching the
    filter can remain invisible if they live in later pages. Returns the
    full list of Items."""
    items = []
    last_key = None
    while True:
        kwargs = dict(scan_kwargs)
        if last_key:
            kwargs['ExclusiveStartKey'] = last_key
        res = table.scan(**kwargs)
        items.extend(res.get('Items', []))
        last_key = res.get('LastEvaluatedKey')
        if not last_key:
            break
    return items
