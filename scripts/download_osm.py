#!/usr/bin/env python3
"""
Download the Washington State OSM PBF extract from Geofabrik.

Output: data/washington-latest.osm.pbf
"""
import sys
from pathlib import Path

import requests
from tqdm import tqdm

URL = "https://download.geofabrik.de/north-america/us/washington-latest.osm.pbf"
ROOT = Path(__file__).parent.parent
OUT_PATH = ROOT / "data" / "washington-latest.osm.pbf"


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        print(f"Already exists: {dest}  (delete to re-download)")
        return

    print(f"Downloading {url}")
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        with open(dest, "wb") as f, tqdm(
            total=total, unit="B", unit_scale=True, unit_divisor=1024
        ) as bar:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
                bar.update(len(chunk))

    print(f"Saved to {dest}")


if __name__ == "__main__":
    download(URL, OUT_PATH)
