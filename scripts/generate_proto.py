#!/usr/bin/env python3
"""Regenerate generated/map_tile_pb2.py from proto/map_tile.proto."""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent

if __name__ == "__main__":
    from grpc_tools import protoc
    ret = protoc.main([
        "grpc_tools.protoc",
        f"--proto_path={ROOT / 'proto'}",
        f"--python_out={ROOT / 'generated'}",
        str(ROOT / "proto" / "map_tile.proto"),
    ])
    sys.exit(ret)
