#!/usr/bin/env python3
"""
Upload protobuf tiles from /mnt/test-mount/mapping/ to S3 and index them in DynamoDB.

Each tile gets a content-based UUID (SHA-256 of its serialized bytes).
A new DDB record is written for every ingestion, preserving version history:
    PK=s2_cell_token  SK=created_at(ISO8601)  tile_id  s3_key

Run after setup_aws.py:
    kubectl port-forward service/localstack 4566:4566 &
    python scripts/ingest_tiles.py [--tiles-dir /mnt/test-mount/mapping]
"""
import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import boto3
from tqdm import tqdm

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from scripts.tile_hash import compute_tile_id

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


def ingest(tiles_dir: Path):
    pb_files = sorted(tiles_dir.glob("*.pb"))
    if not pb_files:
        print(f"No .pb files found in {tiles_dir}")
        sys.exit(1)
    print(f"Ingesting {len(pb_files)} tiles from {tiles_dir}")

    s3  = boto3.client("s3", **_kwargs)
    ddb = boto3.resource("dynamodb", **_kwargs)
    table = ddb.Table(TABLE_NAME)

    now = datetime.now(timezone.utc).isoformat()

    for pb_path in tqdm(pb_files, unit="tile"):
        # The filename (without .pb) is the S2 cell token
        cell_token = pb_path.stem
        tile_bytes = pb_path.read_bytes()
        tile_id    = compute_tile_id(tile_bytes)
        s3_key     = f"tiles/{cell_token}/{tile_id}.pb"

        # Upload to S3
        s3.put_object(Bucket=BUCKET, Key=s3_key, Body=tile_bytes,
                      ContentType="application/octet-stream")

        # Write DDB record — new row per ingestion preserves version history
        table.put_item(Item={
            "s2_cell_token": cell_token,
            "created_at":    now,
            "tile_id":       tile_id,
            "s3_key":        s3_key,
        })

    print(f"Ingested {len(pb_files)} tiles into s3://{BUCKET} and DDB table '{TABLE_NAME}'")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tiles-dir", default="/mnt/test-mount/mapping",
                        help="Directory containing .pb tile files")
    args = parser.parse_args()
    ingest(Path(args.tiles_dir))
