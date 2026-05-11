spent the last few days building a map tile server for autonomous vehicles. tiled all of seattle into ~1,000 S2 cells from raw OSM data, built an API around it, and kept finding new things to fix. thread:

---

OSM washington state dump → two-pass parser → S2 level-14 cells (~600-800m each) → protobuf per cell. 1,016 tiles covering seattle, 69k road segments, 103k routing edges. tiles are content-addressed: SHA-256 of the bytes becomes the tile ID.

---

API is two endpoints. /tile_id takes lat/lon, returns the S2 cell token + tile ID. /download takes a tile ID, returns a presigned S3 URL. DynamoDB stores version history per cell with a timestamp sort key. LocalStack mocks S3 + DDB locally — pinned to 3.8 because the latest image crash-looped on startup.

---

first performance mistake: sync boto3 inside async fastapi handlers blocks the event loop. doesn't matter how many pods you run, requests serialize on the event loop. switched to aioboto3 with persistent connections held in the lifespan context. +26% throughput, -39% p50 latency.

---

even then, DynamoDB was saturating at ~88 req/s. more pods didn't help. added 2-shard redis with MD5 consistent hashing and a 1hr TTL. now at 108 req/s, p99 down from 1328ms to 926ms. cache hit ratio is 64% — not tuned for that, it just happens because simulated vehicles keep going to the same seattle neighborhoods.

---

added prometheus-fastapi-instrumentator (one line, get HTTP histograms for free) + custom counters for cache hits/misses. grafana dashboard: request rate, /tile_id latency at p50/p95/p99, redis hit ratio, error rate. HPA set to 2–10 replicas at 60% CPU, with aggressive scale-up and conservative scale-down.

---

also built a fleet simulator — N async vehicles doing random walks through seattle, each hitting the API every iteration, live p99 output. and added a vehicle simulation to the leaflet viewer: browser builds an adjacency graph from routing edges and drives markers along it with requestAnimationFrame. random walk at every intersection. weirdly hard to stop watching.
