# map-serving

A map tile serving system for autonomous vehicles, built on OpenStreetMap data. Divides Seattle into S2 level-14 cells, serializes each cell's road network as a protobuf, and serves tiles through a cached API backed by DynamoDB and Redis.

```
OSM PBF → S2 tiling → protobuf files → S3/DynamoDB → FastAPI → Redis cache → clients
```

## Architecture

```
┌─────────────┐     ┌─────────────┐     ┌───────────────┐
│  fleet_sim  │────▶│  api-service│────▶│  Redis (×2)   │
│  (clients)  │     │  :8000      │     │  sharded LRU  │
└─────────────┘     └──────┬──────┘     └───────────────┘
                           │
              ┌────────────┴────────────┐
              ▼                         ▼
        ┌──────────┐             ┌──────────┐
        │ DynamoDB │             │    S3    │
        │ (index)  │             │ (tiles)  │
        └──────────┘             └──────────┘
```

- **S2 level-14 cells** — ~600-800m across, ~1,016 tiles covering Seattle
- **DynamoDB** — tile index keyed by S2 cell token; stores version history with timestamp sort key
- **S3** — protobuf tile files at `tiles/{cell_token}/{tile_id}.pb`
- **Redis** — 2-shard cache with consistent hash sharding (MD5), 1-hour TTL
- **LocalStack** — mocks S3 and DynamoDB locally (pinned to 3.8)
- **HPA** — autoscales api-service from 2 to 10 replicas at 60% CPU

## Prerequisites

- Python 3.10+
- [minikube](https://minikube.sigs.k8s.io/) with Docker driver
- kubectl at `~/.local/bin/kubectl`
- ~500MB disk space for OSM data

## Setup

```bash
python3 -m venv ~/envs/map-serving
source ~/envs/map-serving/bin/activate
pip install -r requirements.txt
```

## Data pipeline

**1. Download OSM data**

```bash
python scripts/download_osm.py
# Downloads washington-latest.osm.pbf (~339MB) to data/
```

**2. Tile Seattle**

```bash
python scripts/tile_osm.py
# Writes .pb files to /mnt/test-mount/mapping/
# Writes viewer/viewer_data.json for the map viewer
# Output: ~1,016 tiles, 69k roads, 82k nodes, 103k routing edges
```

**3. Deploy to minikube**

```bash
minikube start
minikube addons enable metrics-server
bash scripts/build_and_deploy.sh
```

This builds Docker images inside minikube's daemon and applies all manifests including LocalStack, Redis, the API, HPA, and the Prometheus/Grafana monitoring stack.

**4. Ingest tiles**

```bash
kubectl port-forward service/localstack 4566:4566 &
python scripts/setup_aws.py    # creates S3 bucket + DynamoDB table
python scripts/ingest_tiles.py # uploads tiles, writes DDB records
```

## API

| Endpoint | Description |
|----------|-------------|
| `GET /tile_id?lat=&lon=` | Returns the S2 cell token and tile ID for a coordinate |
| `GET /download?tile_id=` | Returns a presigned S3 URL for the tile protobuf |
| `GET /health` | Health check |
| `GET /metrics` | Prometheus metrics |

Example:
```bash
MINIKUBE_IP=$(minikube ip)
curl "http://${MINIKUBE_IP}:30000/tile_id?lat=47.614&lon=-122.332"
# {"tile_id": "abc123...", "s2_cell_token": "...", "cache": "miss"}
```

## Fleet simulator

Simulates N vehicles driving random walks through Seattle, each requesting its current tile on every iteration.

```bash
python scripts/fleet_sim.py \
  --vehicles 50 \
  --iterations 20 \
  --concurrency 50
```

| Flag | Default | Description |
|------|---------|-------------|
| `--vehicles` | 100 | Number of simulated vehicles |
| `--iterations` | 50 | Iterations per vehicle |
| `--concurrency` | 100 | Max concurrent requests |
| `--interval` | 0.0 | Seconds between iterations |
| `--api-url` | auto | API base URL (defaults to `http://$(minikube ip):30000`) |
| `--download` | off | Also benchmark `/download` endpoint |

## Map viewer

```bash
cd viewer && python3 -m http.server 8080
# Open http://localhost:8080
```

Shows S2 tile boundaries, road centerlines, routing edges, and intersection nodes as toggleable layers. The "Fleet Simulation" panel in the bottom-left spawns vehicles that drive along the road graph in the browser using `requestAnimationFrame`.

If accessing from a remote machine over SSH:
```bash
ssh -L 8080:localhost:8080 user@remote-host
```

## Monitoring

| Service | NodePort | Description |
|---------|----------|-------------|
| Prometheus | 30090 | Metrics store, scrapes all annotated pods |
| Grafana | 30030 | Dashboard (login: `admin`/`admin`, or anonymous viewer) |

The Grafana dashboard ("Map Serving API") shows request rate, /tile_id latency at p50/p95/p99, Redis cache hit ratio, and error rate.

To access from a laptop SSH'd into the minikube host:
```bash
ssh -L 30030:192.168.49.2:30030 -L 30090:192.168.49.2:30090 user@remote-host
# Grafana:    http://localhost:30030
# Prometheus: http://localhost:30090
```

To watch the HPA scale under load:
```bash
kubectl get hpa -w &
python scripts/fleet_sim.py --vehicles 100 --iterations 30 --concurrency 100
```

## Project layout

```
proto/              Protobuf schema (Lanelet2-inspired road/routing types)
generated/          Pre-compiled Python protobuf bindings
scripts/
  download_osm.py   Download Washington state OSM PBF
  tile_osm.py       Tile Seattle into S2 cells, write protobufs
  setup_aws.py      Create S3 bucket and DynamoDB table in LocalStack
  ingest_tiles.py   Upload tiles to S3, write DynamoDB records
  fleet_sim.py      Async fleet stress-test client
  build_and_deploy.sh  Build images and apply all k8s manifests
services/
  api/              FastAPI service (tile lookup + presigned URL)
k8s/
  configmap.yaml    Environment config (endpoints, bucket names)
  localstack.yaml   LocalStack deployment (pinned to 3.8)
  redis.yaml        Redis StatefulSet (2 shards, headless service)
  api.yaml          API deployment + NodePort service
  hpa.yaml          HPA (min 2, max 10, CPU 60% target)
  monitoring.yaml   Prometheus + Grafana stack
viewer/
  index.html        Leaflet map viewer with fleet simulation
  viewer_data.json  Pre-rendered tile/road/routing geometry
blog/
  map-serving-writeup.md  Project writeup
```

## Tile format

Each `.pb` file is a serialized `MapTile` proto:

```protobuf
message MapTile {
  string tile_id = 1;
  repeated RoadSegment road_segments = 2;
  RoutingGraph routing_graph = 3;
  BoundingBox bbox = 4;
}
```

Tile IDs are content-addressed: SHA-256 of the serialized bytes, formatted as a UUID.
