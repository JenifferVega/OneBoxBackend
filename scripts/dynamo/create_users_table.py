#!/usr/bin/env python3
"""Create the onebox-users table (one-off, idempotent).

    python scripts/dynamo/create_users_table.py

One record per user, keyed by userId (the Cognito sub):
    {userId, timezone, language, updatedAt}

Only the key is declared: DynamoDB accepts any other attribute per item, so
new profile fields (e.g. globalRole from docs/roles_design.md) need no change
here.

Uses the AWS credentials in .env. Running it twice is safe: if the table
already exists it says so and exits.

Not run at app startup on purpose: the production role should not be able to
create tables, and several instances starting together would race.
"""
import os
import sys

from dotenv import load_dotenv

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(_ROOT, ".env"))

import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

TABLE = "onebox-users"
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
            AttributeDefinitions=[{"AttributeName": "userId", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "userId", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
    except ClientError as e:
        code = e.response["Error"]["Code"]
        print(f"Create failed ({code}): {e.response['Error'].get('Message', '')}")
        if code in ("AccessDeniedException", "UnrecognizedClientException"):
            print("These credentials cannot create tables. Create it in the AWS "
                  "console instead: DynamoDB > Create table > name onebox-users, "
                  "partition key userId (String), capacity On-demand.")
        return 1

    print(f"Creating {TABLE} in {REGION}...")
    client.get_waiter("table_exists").wait(TableName=TABLE)
    print(f"{TABLE} is ACTIVE.")
    print("Remember: the ECS task role needs read/write on this table too, "
          "or production will fail with AccessDenied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
