#!/usr/bin/env python3
"""
Create the S3 bucket and DynamoDB table in LocalStack.

Run once after LocalStack is reachable:
    kubectl port-forward service/localstack 4566:4566 &
    python scripts/setup_aws.py
"""
import os
import boto3
from botocore.exceptions import ClientError

ENDPOINT   = os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566")
REGION     = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
TABLE_NAME = os.environ.get("DDB_TABLE_NAME", "map_tiles")
BUCKET     = os.environ.get("S3_BUCKET", "map-tiles")

_kwargs = dict(
    endpoint_url=ENDPOINT,
    region_name=REGION,
    aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "test"),
    aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "test"),
)


def create_s3_bucket():
    s3 = boto3.client("s3", **_kwargs)
    try:
        s3.create_bucket(Bucket=BUCKET)
        print(f"Created S3 bucket: {BUCKET}")
    except ClientError as e:
        if e.response["Error"]["Code"] in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            print(f"S3 bucket already exists: {BUCKET}")
        else:
            raise


def create_ddb_table():
    ddb = boto3.client("dynamodb", **_kwargs)
    try:
        ddb.create_table(
            TableName=TABLE_NAME,
            # PK: s2_cell_token  SK: created_at (ISO8601, sorts chronologically)
            KeySchema=[
                {"AttributeName": "s2_cell_token", "KeyType": "HASH"},
                {"AttributeName": "created_at",    "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "s2_cell_token", "AttributeType": "S"},
                {"AttributeName": "created_at",    "AttributeType": "S"},
                {"AttributeName": "tile_id",       "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "tile_id_index",
                    "KeySchema": [{"AttributeName": "tile_id", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                    "ProvisionedThroughput": {"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
                }
            ],
            BillingMode="PROVISIONED",
            ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
        )
        print(f"Created DDB table: {TABLE_NAME}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceInUseException":
            print(f"DDB table already exists: {TABLE_NAME}")
        else:
            raise


if __name__ == "__main__":
    print(f"Using LocalStack at {ENDPOINT}")
    create_s3_bucket()
    create_ddb_table()
    print("Done.")
