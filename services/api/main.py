import hashlib
import logging
import os
from contextlib import asynccontextmanager

import aioboto3
import s2sphere
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import Counter
from prometheus_fastapi_instrumentator import Instrumentator
from redis.asyncio import Redis

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

S2_LEVEL      = 14
AWS_ENDPOINT  = os.environ["AWS_ENDPOINT_URL"]
S3_PRESIGN_EP = os.environ.get("S3_PRESIGN_ENDPOINT", AWS_ENDPOINT)
DDB_TABLE     = os.environ["DDB_TABLE_NAME"]
S3_BUCKET     = os.environ["S3_BUCKET"]
AWS_REGION    = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
AWS_KEY       = os.environ.get("AWS_ACCESS_KEY_ID", "test")
AWS_SECRET    = os.environ.get("AWS_SECRET_ACCESS_KEY", "test")
REDIS_HOSTS   = [h.strip() for h in os.environ.get("REDIS_HOSTS", "").split(",") if h.strip()]
REDIS_TTL     = int(os.environ.get("REDIS_CACHE_TTL", "3600"))

_session = aioboto3.Session(
    region_name=AWS_REGION,
    aws_access_key_id=AWS_KEY,
    aws_secret_access_key=AWS_SECRET,
)

# Custom cache metrics exposed on /metrics alongside the auto-instrumented HTTP metrics
CACHE_HITS   = Counter("api_cache_hits_total",   "Redis cache hits",   ["endpoint"])
CACHE_MISSES = Counter("api_cache_misses_total",  "Redis cache misses", ["endpoint"])


def latlon_to_cell_token(lat: float, lon: float) -> str:
    ll  = s2sphere.LatLng.from_degrees(lat, lon)
    cid = s2sphere.CellId.from_lat_lng(ll).parent(S2_LEVEL)
    return cid.to_token()


def _shard(cell_token: str, clients: list[Redis]) -> Redis:
    idx = int(hashlib.md5(cell_token.encode()).hexdigest(), 16) % len(clients)
    return clients[idx]


@asynccontextmanager
async def lifespan(app: FastAPI):
    redis_clients = [Redis.from_url(f"redis://{host}") for host in REDIS_HOSTS]
    async with _session.resource("dynamodb", endpoint_url=AWS_ENDPOINT) as ddb:
        async with _session.client("s3", endpoint_url=S3_PRESIGN_EP) as s3:
            app.state.table         = await ddb.Table(DDB_TABLE)
            app.state.s3            = s3
            app.state.redis_clients = redis_clients
            yield
    for r in redis_clients:
        await r.aclose()


app = FastAPI(title="Map Serving API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# Auto-instrument all routes: exposes http_request_duration_seconds histogram
# (count, sum, buckets) with method/handler/status_code labels on GET /metrics
Instrumentator().instrument(app).expose(app)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/tile_id")
async def tile_id(
    request: Request,
    lat: float = Query(..., description="Latitude in WGS84"),
    lon: float = Query(..., description="Longitude in WGS84"),
):
    cell_token = latlon_to_cell_token(lat, lon)
    clients    = request.app.state.redis_clients

    # L1: Redis
    if clients:
        try:
            cached = await _shard(cell_token, clients).get(f"tile:{cell_token}")
            if cached is not None:
                CACHE_HITS.labels(endpoint="tile_id").inc()
                return {"tile_id": cached.decode(), "s2_cell_token": cell_token, "cache": "hit"}
        except Exception as e:
            log.warning("Redis read error: %s", e)

    # L2: DynamoDB
    resp = await request.app.state.table.query(
        KeyConditionExpression="#pk = :pk",
        ExpressionAttributeNames={"#pk": "s2_cell_token"},
        ExpressionAttributeValues={":pk": cell_token},
        ScanIndexForward=False,
        Limit=1,
    )

    CACHE_MISSES.labels(endpoint="tile_id").inc()

    if not resp.get("Items"):
        return {"tile_id": None, "s2_cell_token": cell_token, "cache": "miss"}

    tile_id_val = resp["Items"][0]["tile_id"]

    if clients:
        try:
            await _shard(cell_token, clients).set(
                f"tile:{cell_token}", tile_id_val, ex=REDIS_TTL
            )
        except Exception as e:
            log.warning("Redis write error: %s", e)

    return {"tile_id": tile_id_val, "s2_cell_token": cell_token, "cache": "miss"}


@app.get("/download")
async def download(
    request: Request,
    tile_id: str = Query(..., description="Tile UUID returned by /tile_id"),
):
    resp = await request.app.state.table.query(
        IndexName="tile_id_index",
        KeyConditionExpression="#tid = :tid",
        ExpressionAttributeNames={"#tid": "tile_id"},
        ExpressionAttributeValues={":tid": tile_id},
        Limit=1,
    )
    if not resp.get("Items"):
        raise HTTPException(404, f"Tile '{tile_id}' not found")

    s3_key = resp["Items"][0]["s3_key"]
    url = await request.app.state.s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": S3_BUCKET, "Key": s3_key},
        ExpiresIn=3600,
    )
    return {"tile_id": tile_id, "url": url, "expires_in": 3600}
