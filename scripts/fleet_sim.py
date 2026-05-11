#!/usr/bin/env python3
"""
Phase 3: Fleet simulator and stress-test client.

Simulates a fleet of autonomous vehicles driving through Seattle, each
requesting its current tile on every loop iteration. Prints live throughput
and latency stats, then a final summary.

Usage:
    python scripts/fleet_sim.py [options]

    python scripts/fleet_sim.py --vehicles 200 --iterations 100 --concurrency 100
    python scripts/fleet_sim.py --vehicles 50  --iterations 20  --download
"""
import argparse
import asyncio
import random
import subprocess
import time
from dataclasses import dataclass, field
from statistics import mean

import httpx

# Seattle bounding box (WGS84)
_BB = dict(min_lat=47.4953, max_lat=47.7341, min_lon=-122.4596, max_lon=-122.2244)

# ~500 m step per iteration in lat/lon degrees at Seattle's latitude
_STEP_LAT = 0.0045   # ~500 m
_STEP_LON = 0.0065   # ~500 m


# ---------------------------------------------------------------------------
# Vehicle
# ---------------------------------------------------------------------------

@dataclass
class Vehicle:
    id: int
    lat: float
    lon: float

    def move(self):
        self.lat = _clamp(self.lat + random.uniform(-_STEP_LAT, _STEP_LAT),
                          _BB["min_lat"], _BB["max_lat"])
        self.lon = _clamp(self.lon + random.uniform(-_STEP_LON, _STEP_LON),
                          _BB["min_lon"], _BB["max_lon"])


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _random_vehicle(i: int) -> Vehicle:
    return Vehicle(
        id=i,
        lat=random.uniform(_BB["min_lat"], _BB["max_lat"]),
        lon=random.uniform(_BB["min_lon"], _BB["max_lon"]),
    )


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

@dataclass
class Metrics:
    latencies_ms: list = field(default_factory=list)
    tile_hits:    int = 0
    tile_misses:  int = 0
    errors:       int = 0

    def record_ok(self, ms: float, tile_id):
        self.latencies_ms.append(ms)
        if tile_id:
            self.tile_hits += 1
        else:
            self.tile_misses += 1

    def record_error(self):
        self.errors += 1

    @property
    def total(self):
        return len(self.latencies_ms) + self.errors

    def percentile(self, p: float) -> float:
        if not self.latencies_ms:
            return 0.0
        s = sorted(self.latencies_ms)
        k = max(0, min(len(s) - 1, int(len(s) * p / 100)))
        return s[k]

    def print_summary(self, elapsed_s: float):
        n = len(self.latencies_ms)
        rps = self.total / elapsed_s if elapsed_s > 0 else 0
        print("\n" + "=" * 60)
        print("  STRESS TEST RESULTS")
        print("=" * 60)
        print(f"  Duration          {elapsed_s:.1f}s")
        print(f"  Total requests    {self.total:,}")
        print(f"  Successful        {n:,}")
        print(f"  Errors            {self.errors:,}")
        print(f"  Tile hits         {self.tile_hits:,}")
        print(f"  Tile misses       {self.tile_misses:,}  (no tile ingested for cell)")
        print(f"  Throughput        {rps:.1f} req/s")
        if n:
            print(f"  Latency mean      {mean(self.latencies_ms):.1f} ms")
            print(f"  Latency p50       {self.percentile(50):.1f} ms")
            print(f"  Latency p95       {self.percentile(95):.1f} ms")
            print(f"  Latency p99       {self.percentile(99):.1f} ms")
            print(f"  Latency min/max   {min(self.latencies_ms):.1f} / {max(self.latencies_ms):.1f} ms")
        print("=" * 60)


# ---------------------------------------------------------------------------
# Async request helpers
# ---------------------------------------------------------------------------

async def _tile_id_request(client: httpx.AsyncClient, api_url: str,
                           vehicle: Vehicle, metrics: Metrics,
                           sem: asyncio.Semaphore):
    async with sem:
        t0 = time.perf_counter()
        try:
            r = await client.get(
                f"{api_url}/tile_id",
                params={"lat": vehicle.lat, "lon": vehicle.lon},
            )
            ms = (time.perf_counter() - t0) * 1000
            r.raise_for_status()
            data = r.json()
            tile_id = data.get("tile_id")
            metrics.record_ok(ms, tile_id)
            return tile_id
        except Exception:
            metrics.record_error()
            return None


async def _download_request(client: httpx.AsyncClient, api_url: str,
                            tile_id: str, metrics: Metrics,
                            sem: asyncio.Semaphore):
    if not tile_id:
        return
    async with sem:
        t0 = time.perf_counter()
        try:
            r = await client.get(f"{api_url}/download", params={"tile_id": tile_id})
            ms = (time.perf_counter() - t0) * 1000
            r.raise_for_status()
            metrics.record_ok(ms, tile_id)
        except Exception:
            metrics.record_error()


# ---------------------------------------------------------------------------
# Simulation loop
# ---------------------------------------------------------------------------

async def run(api_url: str, n_vehicles: int, n_iterations: int,
              concurrency: int, interval: float, include_download: bool):

    vehicles = [_random_vehicle(i) for i in range(n_vehicles)]
    tile_metrics    = Metrics()
    download_metrics = Metrics()
    sem = asyncio.Semaphore(concurrency)

    print(f"Fleet:       {n_vehicles} vehicles")
    print(f"Iterations:  {n_iterations}")
    print(f"Concurrency: {concurrency} max in-flight requests")
    print(f"API:         {api_url}")
    print(f"Download:    {'yes' if include_download else 'no'}")
    print()

    wall_start = time.perf_counter()

    async with httpx.AsyncClient(timeout=10.0) as client:
        for iteration in range(1, n_iterations + 1):
            iter_start = time.perf_counter()

            # Fire /tile_id for every vehicle concurrently
            tile_tasks = [
                _tile_id_request(client, api_url, v, tile_metrics, sem)
                for v in vehicles
            ]
            tile_ids = await asyncio.gather(*tile_tasks)

            # Optionally fire /download for each tile_id received
            if include_download:
                dl_tasks = [
                    _download_request(client, api_url, tid, download_metrics, sem)
                    for tid in tile_ids
                ]
                await asyncio.gather(*dl_tasks)

            # Advance all vehicles
            for v in vehicles:
                v.move()

            iter_ms = (time.perf_counter() - iter_start) * 1000
            elapsed = time.perf_counter() - wall_start
            rps = tile_metrics.total / elapsed if elapsed > 0 else 0
            print(
                f"  iter {iteration:>4}/{n_iterations}  "
                f"iter_time={iter_ms:>6.0f}ms  "
                f"cumulative: {tile_metrics.total:>6,} reqs  "
                f"{rps:>7.1f} req/s  "
                f"errors={tile_metrics.errors}",
                flush=True,
            )

            if interval > 0:
                await asyncio.sleep(interval)

    total_elapsed = time.perf_counter() - wall_start

    print("\n--- /tile_id ---")
    tile_metrics.print_summary(total_elapsed)

    if include_download and download_metrics.total > 0:
        print("\n--- /download ---")
        download_metrics.print_summary(total_elapsed)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _default_api_url() -> str:
    try:
        ip = subprocess.check_output(
            ["minikube", "ip"], text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        return f"http://{ip}:30000"
    except Exception:
        return "http://localhost:30000"


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vehicles",    type=int,   default=100,
                        help="Number of simulated AV agents (default 100)")
    parser.add_argument("--iterations",  type=int,   default=50,
                        help="Loop iterations (default 50)")
    parser.add_argument("--concurrency", type=int,   default=100,
                        help="Max in-flight HTTP requests (default 100)")
    parser.add_argument("--interval",    type=float, default=0.0,
                        help="Seconds to wait between iterations (default 0 = max speed)")
    parser.add_argument("--api-url",     default=None,
                        help="Base API URL (default: auto-detect from minikube)")
    parser.add_argument("--download",    action="store_true",
                        help="Also call GET /download for each tile_id received")
    args = parser.parse_args()

    api_url = args.api_url or _default_api_url()
    asyncio.run(run(
        api_url=api_url,
        n_vehicles=args.vehicles,
        n_iterations=args.iterations,
        concurrency=args.concurrency,
        interval=args.interval,
        include_download=args.download,
    ))


if __name__ == "__main__":
    main()
