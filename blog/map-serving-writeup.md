# Building a map serving system for autonomous vehicles

The premise was simple enough: given a lat/lon, return the right map tile. Getting there took longer than expected.

We started with the Washington state OpenStreetMap PBF, about 339MB of road data, and cropped it to Seattle. The plan was to divide the city into S2 level-14 cells — a Google geometry library that partitions the sphere into hierarchical cells, each one roughly 600-800 meters across at that level — serialize each cell's roads into a protobuf file, and serve them through an API.

The tiling script did two passes: first collecting every node and way within the bounding box, then grouping roads by which S2 cells they touched. That produced 1,016 tiles, 69,441 road segments, 82,674 nodes, and 103,585 routing edges. We built a Leaflet viewer to sanity-check the output — dark-themed, with toggleable layers for tile boundaries, road centerlines, and routing nodes — mostly because looking at a raw tile count doesn't tell you much about whether the geometry is actually right.

## The API

Two endpoints. `GET /tile_id` takes a lat/lon and returns the S2 cell token for that location. `GET /download` takes a tile ID and returns a presigned S3 URL. DynamoDB stores version history keyed by cell token with a timestamp sort key, so you can track when a tile changed. Each tile gets a content-addressed UUID derived from SHA-256 of its bytes.

We initially built a separate microservice to handle the coordinate-to-S2-token conversion. Cut it when we realized the "service" was a 4-line math operation that didn't need its own deployment.

LocalStack mocked S3 and DynamoDB locally. The latest image crashed on startup repeatedly — something about a deprecated `SERVICES` env var. We pinned to 3.8 and moved on.

## The fleet simulator

To load-test the API, we wrote a fleet simulator: N vehicles driving random walks through Seattle, each requesting its current tile every iteration. asyncio fires all the requests concurrently, a semaphore caps in-flight connections, and the output prints throughput and latency percentiles in real time.

Early numbers were bad — around 60 req/s with high p99 latency. Two things were wrong.

First: the API was using synchronous boto3 inside async handlers. Sync calls block the event loop, so requests serialize regardless of how many pods you're running. Switching to aioboto3 with persistent connections held in the lifespan context manager got us to +26% throughput and knocked 39% off p50 latency.

Second: even with that fixed, LocalStack DynamoDB was saturating around 88 req/s. More pods didn't help; the bottleneck wasn't compute. We added Redis with two shards — consistent hash sharding via MD5, 1-hour TTL — which pushed throughput to 108.5 req/s and brought p99 from 1,328ms to 926ms. Cache hit ratio settled around 64%. Seattle vehicles cluster in the same neighborhoods, so the same tiles get requested repeatedly.

## Monitoring

`prometheus-fastapi-instrumentator` instruments all routes automatically — HTTP request counts, latency histograms, status code breakdowns. We added counters for cache hits and misses. Grafana gets a provisioned dashboard with four panels: request rate by handler, /tile_id latency at p50/p95/p99, Redis cache hit ratio, and error rate.

The HPA targets 60% CPU with a 2-replica floor and 10-replica ceiling. Scale-up is set to 2 pods per 30 seconds with a 30-second stabilization window — the expected load pattern is sudden fleet spikes, not gradual ramps. Scale-down is slower (1 pod per minute, 2-minute window) to avoid bouncing during brief drops.

## The viewer simulation

The last thing we added was a vehicle simulation in the Leaflet viewer itself. The browser builds an adjacency graph from the routing edges and drives markers along it with `requestAnimationFrame`. No pathfinding, no traffic model — vehicles just pick a random outgoing edge at each intersection. It's not accurate to anything, but watching dots move down actual Seattle streets makes the tile data feel less abstract than staring at polylines.

## A few things that didn't go as planned

The tiling script assigns a way to every S2 cell that any of its nodes touches. A road segment that crosses a cell boundary ends up in both cells. For tile lookup this doesn't matter — you find a tile and download it. For routing it would, because you'd need to deduplicate or clip at the boundary.

The fleet simulator and the browser simulation are completely disconnected. The simulator calls the API and reports latency. The browser simulation drives on local data and calls nothing. Hooking them together — showing actual cache hits per vehicle, real response times, which tiles are hot — would make the demo more useful and the monitoring data less abstract. That's the obvious next thing to build.
