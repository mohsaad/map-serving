# Map Serving

## Background

We want to create a map serving service.

## Phase 1

Let's take OpenStreetMap and tile them into S2 tiles and store them on disk at /mnt/test-mount/mapping.

Create a script for downloading OpenStreetMap graph and road tiles, and segmenting them into S2
tiles.

Create a web viewer that visualizes these tiles at a high level. Download OSM data for Seattle
and visualize it.

## Phase 2

We will the define an API:

* GET /download --> retrieves an S3 presigned link to download given a UUID tile
* GET /tile_id  --> given a latlon, return the tile ID this latlon belongs to
  * This should hit a separate geo-encoding service that searches if the tile exists
    and return null if it does not


We are expected to serve millions of requests for downloads and tile ID per minute. To handle
this, let's spin up a minikube instance to run our API server. Our framework will be FastAPI
so we can scale. We'll also need to set up a db for our tiles.

Our DB will be a local mongodb/simulated dynamodb. We'll store each tile as a GUID, along with a version history of the tile (this will be a flat tile at first). Then, a client will receive a link to download the tile, which is stored in the filesystem. For our case, just return the path.

## Phase 3

Let's create a client that simulates a fleet of autonomous vehicles driving through Seattle. At every iteration of a loop, create a LatLon and request the tiles. The idea here is to stress test this system.

