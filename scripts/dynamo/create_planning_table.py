#!/usr/bin/env python3
"""Create the onebox-planning table (one-off, idempotent).

    python scripts/dynamo/create_planning_table.py

The planning chat of each project (Intelligence > Replanificación):
    projectId (HASH) + itemId (RANGE)
    itemId = "msg#<iso>#<rand>"  a chat message (user / assistant)
           = "prop#<id>"         a plan proposal (pending / applied / rejected /
                                 reverted / stale / superseded) with its operations
                                 and, once applied, the events needed to revert it.

Uses the AWS credentials in .env. Running it twice is safe.
"""
import os
import sys

from dotenv import load_dotenv

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(_ROOT, ".env"))

import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

TABLE = "onebox-planning"
REGION = os.environ.get("AWS_REGION", "us-east-1")


def main() -> int:
    client = boto3.client("dynamodb", region_name=REGION)
    try:
        desc = client.describe_table(TableName=TABLE)["Table"]
        print(f"{TABLE} already exists in {REGION} (status {desc['TableStatus']}). Nothing to do.")
        return 0
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            print(f"Could not check {TABLE}: {e}")
            return 1
    try:
        client.create_table(
            TableName=TABLE,
            AttributeDefinitions=[{"AttributeName": "projectId", "AttributeType": "S"},
                                  {"AttributeName": "itemId", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "projectId", "KeyType": "HASH"},
                       {"AttributeName": "itemId", "KeyType": "RANGE"}],
            BillingMode="PAY_PER_REQUEST",
        )
    except ClientError as e:
        code = e.response["Error"]["Code"]
        print(f"Create failed ({code}): {e.response['Error'].get('Message', '')}")
        if code in ("AccessDeniedException", "UnrecognizedClientException"):
            print("These credentials cannot create tables. Create it in the AWS console: "
                  "DynamoDB > Create table > name onebox-planning, partition key projectId (String), "
                  "sort key itemId (String), capacity On-demand.")
        return 1
    client.get_waiter("table_exists").wait(TableName=TABLE)
    print(f"{TABLE} created in {REGION}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
