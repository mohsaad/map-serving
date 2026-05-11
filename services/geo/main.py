import logging
import os
from contextlib import asynccontextmanager

import aioboto3
import s2sphere
from fastapi import FastAPI, Query, Request

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

S2_LEVEL     = 14
AWS_ENDPOINT = os.environ["AWS_ENDPOINT_URL"]
DDB_TABLE    = os.environ["DDB_TABLE_NAME"]
AWS_REGION   = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
AWS_KEY      = os.environ.get("AWS_ACCESS_KEY_ID", "test")
AWS_SECRET   = os.environ.get("AWS_SECRET_ACCESS_KEY", "test")

_session = aioboto3.Session(
    region_name=AWS_REGION,
    aws_access_key_id=AWS_KEY,
    aws_secret_access_key=AWS_SECRET,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Open one persistent DynamoDB connection for the lifetime of the process.
    async with _session.resource("dynamodb", endpoint_url=AWS_ENDPOINT) as ddb:
        app.state.table = await ddb.Table(DDB_TABLE)
        yield


app = FastAPI(title="Geo-Encoding Service", lifespan=lifespan)


def latlon_to_cell_token(lat: float, lon: float) -> str:
    ll  = s2sphere.LatLng.from_degrees(lat, lon)
    cid = s2sphere.CellId.from_lat_lng(ll).parent(S2_LEVEL)
    return cid.to_token()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/tile_id")
async def tile_id(
    request: Request,
    lat: float = Query(..., description="Latitude in WGS84"),
    lon: float = Query(..., description="Longitude in WGS84"),
):
    """
    Given a lat/lon, return the tile ID of the S2 level-14 cell that contains it,
    or null if no tile has been ingested for that cell.
    """
    cell_token = latlon_to_cell_token(lat, lon)
    log.debug("lat=%.6f lon=%.6f → cell=%s", lat, lon, cell_token)

    resp = await request.app.state.table.query(
        KeyConditionExpression="#pk = :pk",
        ExpressionAttributeNames={"#pk": "s2_cell_token"},
        ExpressionAttributeValues={":pk": cell_token},
        ScanIndexForward=False,  # latest version first (SK is ISO8601 created_at)
        Limit=1,
    )

    if not resp.get("Items"):
        return {"tile_id": None, "s2_cell_token": cell_token}

    return {"tile_id": resp["Items"][0]["tile_id"], "s2_cell_token": cell_token}
