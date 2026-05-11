"""Content-based tile UUID: SHA-256 of the serialized protobuf, formatted as UUID."""
import hashlib


def compute_tile_id(tile_bytes: bytes) -> str:
    h = hashlib.sha256(tile_bytes).hexdigest()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"
