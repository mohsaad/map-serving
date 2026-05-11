# Building a map serving system for autonomous vehicles

The premise was simple enough: given a lat/lon, return the right map tile. Getting there took longer than expected.

We started with the Washington state OpenStreetMap PBF, about 339MB of road data, and cropped it to Seattle. The plan was to divide the city into S2 level-14 cells — a Google geometry library that partitions the sphere into hierarchical cells, each one roughly 600-800 meters across at that level — serialize each cell's roads into a protobuf file, and serve them through an API.

The full system looks like this:

```
  ┌─────────────────────────────────────────────────────────────────┐
  │  Data pipeline (run once)                                        │
  │                                                                  │
  │  washington.osm.pbf ──▶ tile_osm.py ──▶ 1,016 × {cell}.pb      │
  │        339 MB              2-pass             S2 level-14        │
  │                            parser        ──▶ viewer_data.json    │
  └────────────────────────────────┬────────────────────────────────┘
                                   │  ingest_tiles.py
                    ┌──────────────▼──────────────┐
                    │         LocalStack           │
                    │   ┌──────────┐ ┌──────────┐  │
                    │   │    S3    │ │ DynamoDB  │  │
                    │   │ (tiles)  │ │ (index)  │  │
                    │   └──────────┘ └──────────┘  │
                    └──────────────┬───────────────┘
                                   │
                    ┌──────────────▼───────────────┐
                    │     api-service  (×2–10)      │
                    │     FastAPI + aioboto3         │
                    └──────┬───────────────┬────────┘
                           │               │
                    ┌──────▼──────┐ ┌──────▼──────┐
                    │   redis-0   │ │   redis-1   │
                    └─────────────┘ └─────────────┘
                           │
              ┌────────────┴────────────┐
              │                         │
       ┌──────▼──────┐          ┌───────▼──────┐
       │ fleet_sim   │          │   browser    │
       │ (stress     │          │   viewer     │
       │  testing)   │          │ (simulation) │
       └─────────────┘          └──────────────┘
```

## Tiling Seattle

The tiling script did two passes over the OSM data: first collecting every node and way within the Seattle bounding box, then grouping roads by which S2 cells they touched.

S2 divides the sphere into a hierarchy of cells. At level 14, each cell is roughly 600-800 meters across — small enough to give vehicles useful local context, large enough to keep tile counts manageable.

```
  Seattle bbox: 47.4953°N–47.7341°N, 122.2244°W–122.4596°W

  ┌──────┬──────┬──────┬──────┬──────┬──────┐
  │      │      │  ╱   │      │      │      │
  │      │      │ ╱    │      │      │      │
  ├──────┼──────┼──────┼──────┼──────┼──────┤
  │      │  ●───┼──────┼──●   │      │      │  ● = routing node
  │      │      │  ╲   │      │      │      │
  ├──────┼──────┼──────┼──────┼──────┼──────┤
  │      │      │   ╲  │      │      │      │
  │      │      │    ● │      │      │      │
  ├──────┼──────┼──────┼──────┼──────┼──────┤
  │      │      │      │  ╱   │      │      │
  │      │      │      │ ╱    │      │      │
  └──────┴──────┴──────┴──────┴──────┴──────┘

  Each cell gets a token e.g. "3f29c4ab" and its own .pb file.
  1,016 tiles · 69,441 road segments · 82,674 nodes · 103,585 routing edges
```

Each `.pb` is a serialized `MapTile` proto — road segments, routing graph, and bounding box. Tile IDs are content-addressed: SHA-256 of the serialized bytes, formatted as a UUID.

We built a Leaflet viewer to sanity-check the output — dark-themed, with toggleable layers for tile boundaries, road centerlines, and routing nodes — mostly because looking at a raw tile count doesn't tell you much about whether the geometry is actually right.

## The API

Two endpoints. `GET /tile_id` takes a lat/lon and returns the S2 cell token for that location. `GET /download` takes a tile ID and returns a presigned S3 URL. DynamoDB stores version history keyed by cell token with a timestamp sort key, so you can track when a tile changed.

```
  GET /tile_id?lat=47.614&lon=-122.332
       │
       ▼
  ┌─────────────────────┐
  │  S2 math  (~1μs)    │   lat/lon ──▶ CellId.from_lat_lng()
  │                     │              .parent(14).to_token()
  └──────────┬──────────┘
             │  cell_token = "3f29c4ab"
             ▼
  ┌──────────────────────┐   hit    ┌───────────────────────────┐
  │  Redis (MD5 shard)   │─────────▶│  {"cache": "hit", ...}    │
  └──────────┬───────────┘          └───────────────────────────┘
             │ miss
             ▼
  ┌──────────────────────┐
  │  DynamoDB query      │   KeyConditionExpression on cell_token
  └──────────┬───────────┘
             │
             ├──▶ write-back to Redis
             │
             └──▶  {"cache": "miss", "tile_id": "abc123...", ...}


  GET /download?tile_id=abc123...
       │
       ▼
  ┌──────────────────────┐
  │  DynamoDB GSI query  │   tile_id_index
  └──────────┬───────────┘
             │  s3_key = "tiles/3f29c4ab/abc123.pb"
             ▼
  ┌──────────────────────┐
  │  S3 presigned URL    │   expires in 3600s
  └──────────────────────┘
```

We initially built a separate microservice to handle the coordinate-to-S2-token conversion. Cut it when we realized the "service" was a 4-line math operation that didn't need its own deployment.

LocalStack mocked S3 and DynamoDB locally. The latest image crashed on startup repeatedly — something about a deprecated `SERVICES` env var. We pinned to 3.8 and moved on.

## The fleet simulator

To load-test the API, we wrote a fleet simulator: N vehicles driving random walks through Seattle, each requesting its current tile every iteration. asyncio fires all the requests concurrently, a semaphore caps in-flight connections, and the output prints throughput and latency percentiles in real time.

```
  ┌──────────────────────────────────────────────────────────┐
  │  fleet_sim.py --vehicles 50 --iterations 20              │
  │                                                          │
  │  Vehicle 0  ·  ·  ·  ·  ·  ·  →  ·  →  ·  ·           │
  │  Vehicle 1     ·  ·  →  ·  ·  ·  ·  ·  →  ·           │
  │  Vehicle 2  ·  ·  ·  ·  ·  ·  ·  →  ·  ·  ·           │
  │  ...         (random walk, clamped to Seattle bbox)      │
  │                                                          │
  │  asyncio.Semaphore(concurrency=50)                       │
  │  ├── all vehicles fire requests in parallel              │
  │  └── cap in-flight to avoid overwhelming the API         │
  │                                                          │
  │  iter  20/20  iter_time=89ms  151.7 req/s  errors=0     │
  │  p50=52ms  p95=194ms  p99=198ms                          │
  └──────────────────────────────────────────────────────────┘
```

Early numbers were bad — around 60 req/s with high p99 latency. Two things were wrong.

First: the API was using synchronous boto3 inside async handlers. Sync calls block the event loop, so requests serialize regardless of how many pods you're running.

```
  Before (sync boto3):                After (aioboto3):

  request 1 ──▶ [  DDB call  ] ──▶   request 1 ──▶ [DDB]─┐
  request 2        waiting            request 2 ──▶ [DDB]─┤ concurrent
  request 3        waiting            request 3 ──▶ [DDB]─┤
  request 4        waiting            request 4 ──▶ [DDB]─┘

  ~60 req/s, p50=~180ms               +26% throughput, -39% p50
```

Second: even with that fixed, LocalStack DynamoDB was saturating around 88 req/s. More pods didn't help; the bottleneck wasn't compute.

## Redis sharding

We added Redis with two shards — consistent hash sharding via MD5, 1-hour TTL. The shard for a given cell token is determined at request time:

```
  cell_token = "3f29c4ab"
        │
        ▼
  MD5("3f29c4ab") = "a3f8b21c..."
        │
        ▼
  int("a3f8b21c...") % 2 = 1
        │
      ┌─┴─┐
      0   1
      │   │
      ▼   ▼
  redis-0  redis-1      ← StatefulSet, stable DNS via headless service
  (shard 0)  (shard 1)    redis-0.redis-headless:6379
                           redis-1.redis-headless:6379
```

Result: 108.5 req/s, p99 down from 1,328ms to 926ms. Cache hit ratio settled at 64%. Seattle vehicles cluster in the same neighborhoods, so the same tiles get requested over and over — the cache hit ratio wasn't tuned for, it just emerged.

## Monitoring

`prometheus-fastapi-instrumentator` instruments all routes automatically — HTTP request counts, latency histograms, status code breakdowns. We added counters for cache hits and misses on top of that.

```
  Grafana dashboard: "Map Serving API"
  ┌─────────────────────────┬─────────────────────────┐
  │  Request Rate (req/s)   │  Latency — /tile_id     │
  │                         │                         │
  │  ▁▂▄▆█▇▅▃▂▁            │  p99 ────────────       │
  │  by handler             │  p95 ──────────         │
  │                         │  p50 ────────           │
  ├─────────────────────────┼─────────────────────────┤
  │  Redis Cache Hit Ratio  │  Error Rate             │
  │                         │                         │
  │  hit ratio ─────────    │  5xx                    │
  │  hits/s ─────────       │  4xx                    │
  │  misses/s ──────        │                         │
  └─────────────────────────┴─────────────────────────┘
  refresh: 5s · window: last 15 min
```

The HPA targets 60% CPU with a 2-replica floor and 10-replica ceiling. Scale-up is aggressive because the expected load pattern is sudden fleet spikes. Scale-down is conservative to avoid bouncing during brief drops.

```
  replicas
    10 │                     ┌──┐
     8 │                  ┌──┘  └──┐
     6 │               ┌──┘        └──┐
     4 │            ┌──┘              └──┐
     2 │────────────┘                    └────────────
       └─────────────────────────────────────────────▶ time
         idle      fleet spike              idle

       ◀─30s─▶ scale up 2 pods/step, 30s stabilization window
                             ◀─2min─▶ scale down 1 pod/min
```

## The viewer simulation

The last thing we added was a vehicle simulation in the Leaflet viewer itself. The browser builds an adjacency graph from the routing edges and drives markers along it with `requestAnimationFrame`. No pathfinding, no traffic model — vehicles just pick a random outgoing edge at each intersection.

```
  Adjacency graph (built in-browser from routing_edges):

  node A ──▶ node B ──▶ node D
    │                     │
    └──▶ node C ──▶ node E▼
                         node F

  Vehicle state per frame:
  { fromPos, toPos, t ∈ [0,1], speed }

  each frame:
    t += (speed × dt) / edgeLength
    if t >= 1: advance to toPos, pick random next edge
    marker.setLatLng(lerp(fromPos, toPos, t))
```

It's not accurate to anything, but watching dots move down actual Seattle streets makes the tile data feel less abstract than staring at polylines.

## A few things that didn't go as planned

The tiling script assigns a way to every S2 cell that any of its nodes touches. A road segment that crosses a cell boundary ends up in both cells. For tile lookup this doesn't matter — you find a tile and download it. For routing it would, because you'd need to deduplicate or clip at the boundary.

```
  Cell A │ Cell B
         │
    ─────┼──────────  ← this road segment lives in both cells
         │
```

The fleet simulator and the browser simulation are completely disconnected. The simulator calls the API and reports latency. The browser simulation drives on local data and calls nothing. Hooking them together — showing actual cache hits per vehicle, real response times, which tiles are hot — would make the demo more useful and the monitoring data less abstract. That's the obvious next thing to build.
