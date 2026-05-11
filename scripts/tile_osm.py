#!/usr/bin/env python3
"""
Parse the Washington State OSM PBF, tile roads into S2 level-14 cells for
the Seattle bounding box, write protobuf tiles to /mnt/test-mount/mapping/,
and emit viewer/viewer_data.json for the Leaflet viewer.

Usage:
    python scripts/tile_osm.py [--pbf data/washington-latest.osm.pbf]
                               [--out /mnt/test-mount/mapping]
                               [--viewer viewer/viewer_data.json]
"""
import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import osmium
import s2sphere
from tqdm import tqdm

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from generated import map_tile_pb2 as pb

# Seattle bounding box (WGS84)
SEATTLE_BBOX = {
    "min_lat": 47.4953,
    "max_lat": 47.7341,
    "min_lon": -122.4596,
    "max_lon": -122.2244,
}

S2_LEVEL = 14

# OSM highway values we care about (excludes footways, cycleways, etc.)
ROAD_HIGHWAY_TAGS = {
    "motorway", "trunk", "primary", "secondary", "tertiary",
    "residential", "service", "unclassified",
    "motorway_link", "trunk_link", "primary_link",
    "secondary_link", "tertiary_link",
}

HIGHWAY_CLASS_MAP = {
    "motorway":      pb.HIGHWAY_MOTORWAY,
    "trunk":         pb.HIGHWAY_TRUNK,
    "primary":       pb.HIGHWAY_PRIMARY,
    "secondary":     pb.HIGHWAY_SECONDARY,
    "tertiary":      pb.HIGHWAY_TERTIARY,
    "residential":   pb.HIGHWAY_RESIDENTIAL,
    "service":       pb.HIGHWAY_SERVICE,
    "unclassified":  pb.HIGHWAY_UNCLASSIFIED,
    "motorway_link": pb.HIGHWAY_MOTORWAY_LINK,
    "trunk_link":    pb.HIGHWAY_TRUNK_LINK,
    "primary_link":  pb.HIGHWAY_PRIMARY_LINK,
    "secondary_link":pb.HIGHWAY_SECONDARY_LINK,
    "tertiary_link": pb.HIGHWAY_TERTIARY_LINK,
}


def _in_bbox(lat: float, lon: float) -> bool:
    bb = SEATTLE_BBOX
    return bb["min_lat"] <= lat <= bb["max_lat"] and bb["min_lon"] <= lon <= bb["max_lon"]


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    R = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _parse_speed(tag: str) -> float:
    """Convert OSM maxspeed tag to km/h; returns 0.0 if unparseable."""
    tag = tag.strip()
    if not tag:
        return 0.0
    if tag.endswith("mph"):
        try:
            return float(tag[:-3].strip()) * 1.60934
        except ValueError:
            return 0.0
    try:
        return float(tag)
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# Pass 1 — collect nodes and ways
# ---------------------------------------------------------------------------

class OsmCollector(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        # node_id -> (lat, lon)  — only nodes inside Seattle bbox
        self.nodes: dict[int, tuple[float, float]] = {}
        # way_id -> {"tags": dict, "node_ids": list[int]}
        self.ways: dict[int, dict] = {}

    def node(self, n):
        lat, lon = float(n.location.lat), float(n.location.lon)
        if _in_bbox(lat, lon):
            self.nodes[n.id] = (lat, lon)

    def way(self, w):
        hw = w.tags.get("highway", "")
        if hw not in ROAD_HIGHWAY_TAGS:
            return
        node_ids = [n.ref for n in w.nodes]
        self.ways[w.id] = {
            "tags": dict(w.tags),
            "node_ids": node_ids,
        }


# ---------------------------------------------------------------------------
# Build routing graph and tile data
# ---------------------------------------------------------------------------

def find_intersection_nodes(ways: dict) -> set[int]:
    """Return OSM node IDs that appear in 2+ ways (intersections)."""
    node_way_count: dict[int, int] = defaultdict(int)
    for way in ways.values():
        seen = set()
        for nid in way["node_ids"]:
            if nid not in seen:
                node_way_count[nid] += 1
                seen.add(nid)
    # Also always treat endpoints as routing nodes
    endpoints: set[int] = set()
    for way in ways.values():
        nids = way["node_ids"]
        if nids:
            endpoints.add(nids[0])
            endpoints.add(nids[-1])
    return {nid for nid, cnt in node_way_count.items() if cnt >= 2} | endpoints


def cell_token(cell_id: s2sphere.CellId) -> str:
    return cell_id.to_token()


def build_tiles(
    nodes: dict[int, tuple[float, float]],
    ways: dict,
    intersection_nodes: set[int],
) -> dict[str, pb.MapTile]:
    """
    Group road segments and routing graph by S2 level-14 cell token.
    A segment belongs to a cell if any of its nodes falls in that cell.
    """
    # cell_token -> MapTile (built incrementally)
    tiles: dict[str, pb.MapTile] = {}
    # cell_token -> set of routing node IDs already added
    tile_routing_nodes: dict[str, set[int]] = defaultdict(set)
    # cell_token -> set of (from, to, way_id) edges already added
    tile_routing_edges: dict[str, set[tuple]] = defaultdict(set)

    def get_or_create_tile(token: str, cid: s2sphere.CellId) -> pb.MapTile:
        if token not in tiles:
            cell = s2sphere.Cell(cid)
            verts = [cell.get_vertex(i) for i in range(4)]
            latlngs = [s2sphere.LatLng.from_point(v) for v in verts]
            lats = [ll.lat().degrees for ll in latlngs]
            lons = [ll.lng().degrees for ll in latlngs]
            tile = pb.MapTile(
                tile_id=pb.S2CellId(id=cid.id(), level=S2_LEVEL),
                bbox_min=pb.Point(lat=min(lats), lon=min(lons)),
                bbox_max=pb.Point(lat=max(lats), lon=max(lons)),
            )
            tiles[token] = tile
        return tiles[token]

    def add_routing_node(tile: pb.MapTile, token: str, nid: int):
        if nid in tile_routing_nodes[token]:
            return
        if nid not in nodes:
            return
        lat, lon = nodes[nid]
        tile.routing_graph.nodes.append(pb.RoutingNode(
            osm_node_id=nid,
            location=pb.Point(lat=lat, lon=lon),
            is_intersection=(nid in intersection_nodes),
        ))
        tile_routing_nodes[token].add(nid)

    def add_routing_edge(tile: pb.MapTile, token: str, from_nid: int, to_nid: int,
                         way_id: int, length_m: float, bidirectional: bool):
        key = (min(from_nid, to_nid), max(from_nid, to_nid), way_id)
        if key in tile_routing_edges[token]:
            return
        tile.routing_graph.edges.append(pb.RoutingEdge(
            from_node_id=from_nid,
            to_node_id=to_nid,
            osm_way_id=way_id,
            length_meters=length_m,
            bidirectional=bidirectional,
        ))
        tile_routing_edges[token].add(key)

    for way_id, way in tqdm(ways.items(), desc="Tiling ways", unit="way"):
        node_ids = way["node_ids"]
        tags = way["tags"]

        # Resolve nodes; skip if none are in our bbox
        resolved = [(nid, nodes[nid]) for nid in node_ids if nid in nodes]
        if len(resolved) < 2:
            continue

        hw_tag = tags.get("highway", "")
        hw_class = HIGHWAY_CLASS_MAP.get(hw_tag, pb.HIGHWAY_UNKNOWN)
        oneway = tags.get("oneway", "no") in ("yes", "1", "true")
        name = tags.get("name", "")
        speed = _parse_speed(tags.get("maxspeed", ""))

        try:
            lanes = int(tags.get("lanes", 0))
        except ValueError:
            lanes = 0
        try:
            lanes_fwd = int(tags.get("lanes:forward", 0))
        except ValueError:
            lanes_fwd = 0
        try:
            lanes_bwd = int(tags.get("lanes:backward", 0))
        except ValueError:
            lanes_bwd = 0

        turn_fwd = tags.get("turn:lanes:forward", tags.get("turn:lanes", ""))
        turn_bwd = tags.get("turn:lanes:backward", "")

        # Find which S2 cells this way touches
        cell_to_nodes: dict[str, list[tuple[int, tuple[float, float]]]] = defaultdict(list)
        for nid, (lat, lon) in resolved:
            ll = s2sphere.LatLng.from_degrees(lat, lon)
            cid = s2sphere.CellId.from_lat_lng(ll).parent(S2_LEVEL)
            tok = cell_token(cid)
            cell_to_nodes[tok].append((nid, (lat, lon)))

        for tok, cell_nodes in cell_to_nodes.items():
            cid = s2sphere.CellId.from_token(tok)
            tile = get_or_create_tile(tok, cid)

            # Road segment: use all resolved nodes as centerline (full way)
            pts = [pb.Point(lat=lat, lon=lon) for _, (lat, lon) in
                   [(nid, nodes[nid]) for nid in node_ids if nid in nodes]]
            seg = pb.RoadSegment(
                osm_way_id=way_id,
                centerline=pb.LineString(points=pts),
                highway_class=hw_class,
                lane_count=lanes,
                lane_count_forward=lanes_fwd,
                lane_count_backward=lanes_bwd,
                turn_lanes_forward=turn_fwd,
                turn_lanes_backward=turn_bwd,
                road_name=name,
                oneway=oneway,
                max_speed_kmh=speed,
                start_node_id=node_ids[0],
                end_node_id=node_ids[-1],
            )
            tile.road_segments.append(seg)

            # Routing graph: split way at intersection nodes; add edges
            seg_start = resolved[0][0]
            seg_length = 0.0
            prev_lat, prev_lon = resolved[0][1]

            for i, (nid, (lat, lon)) in enumerate(resolved):
                seg_length += _haversine_m(prev_lat, prev_lon, lat, lon)
                prev_lat, prev_lon = lat, lon

                is_junction = (nid in intersection_nodes) or (i == len(resolved) - 1)
                if is_junction and nid != seg_start:
                    # Emit an edge from seg_start -> nid
                    add_routing_node(tile, tok, seg_start)
                    add_routing_node(tile, tok, nid)
                    add_routing_edge(tile, tok, seg_start, nid, way_id,
                                     seg_length, not oneway)
                    seg_start = nid
                    seg_length = 0.0

    return tiles


# ---------------------------------------------------------------------------
# Write protobuf tiles to disk
# ---------------------------------------------------------------------------

def write_tiles(tiles: dict[str, pb.MapTile], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for token, tile in tqdm(tiles.items(), desc="Writing tiles", unit="tile"):
        path = out_dir / f"{token}.pb"
        path.write_bytes(tile.SerializeToString())
    print(f"Wrote {len(tiles)} tiles to {out_dir}")


# ---------------------------------------------------------------------------
# Emit viewer_data.json for the Leaflet viewer
# ---------------------------------------------------------------------------

def write_viewer_json(
    tiles: dict[str, pb.MapTile],
    nodes: dict[int, tuple[float, float]],
    out_path: Path,
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)

    tile_boundaries = []  # list of [lat, lon] rings
    road_lines = []       # list of [[lat, lon], ...]
    routing_nodes = []    # list of [lat, lon]
    routing_edges = []    # list of [[lat, lon], [lat, lon]]

    routing_node_locs: dict[int, tuple[float, float]] = {}

    for token, tile in tiles.items():
        cid = s2sphere.CellId.from_token(token)
        cell = s2sphere.Cell(cid)
        ring = []
        for i in range(4):
            v = cell.get_vertex(i)
            ll = s2sphere.LatLng.from_point(v)
            ring.append([ll.lat().degrees, ll.lng().degrees])
        ring.append(ring[0])  # close ring
        tile_boundaries.append(ring)

        for seg in tile.road_segments:
            coords = [[p.lat, p.lon] for p in seg.centerline.points]
            if len(coords) >= 2:
                road_lines.append(coords)

        for rn in tile.routing_graph.nodes:
            routing_node_locs[rn.osm_node_id] = (rn.location.lat, rn.location.lon)

    # Deduplicate routing nodes across tiles
    for nid, (lat, lon) in routing_node_locs.items():
        routing_nodes.append([lat, lon])

    # Routing edges: collect unique edges and resolve coordinates
    seen_edges: set[tuple] = set()
    for tile in tiles.values():
        node_loc = {rn.osm_node_id: (rn.location.lat, rn.location.lon)
                    for rn in tile.routing_graph.nodes}
        for edge in tile.routing_graph.edges:
            key = (min(edge.from_node_id, edge.to_node_id),
                   max(edge.from_node_id, edge.to_node_id),
                   edge.osm_way_id)
            if key in seen_edges:
                continue
            seen_edges.add(key)
            a = node_loc.get(edge.from_node_id) or routing_node_locs.get(edge.from_node_id)
            b = node_loc.get(edge.to_node_id) or routing_node_locs.get(edge.to_node_id)
            if a and b:
                routing_edges.append([[a[0], a[1]], [b[0], b[1]]])

    data = {
        "tile_boundaries": tile_boundaries,
        "road_lines": road_lines,
        "routing_nodes": routing_nodes,
        "routing_edges": routing_edges,
    }
    out_path.write_text(json.dumps(data))
    print(f"Viewer data: {out_path}  "
          f"({len(tile_boundaries)} tiles, {len(road_lines)} roads, "
          f"{len(routing_nodes)} nodes, {len(routing_edges)} edges)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pbf",    default=str(ROOT / "data" / "washington-latest.osm.pbf"))
    parser.add_argument("--out",    default="/mnt/test-mount/mapping")
    parser.add_argument("--viewer", default=str(ROOT / "viewer" / "viewer_data.json"))
    args = parser.parse_args()

    pbf_path = Path(args.pbf)
    if not pbf_path.exists():
        print(f"PBF not found: {pbf_path}\nRun: python scripts/download_osm.py")
        sys.exit(1)

    print(f"Pass 1: collecting nodes and ways from {pbf_path.name} ...")
    collector = OsmCollector()
    collector.apply_file(str(pbf_path), locations=True)
    print(f"  {len(collector.nodes):,} nodes, {len(collector.ways):,} highway ways")

    print("Finding intersection nodes ...")
    intersections = find_intersection_nodes(collector.ways)
    print(f"  {len(intersections):,} routing nodes (intersections + endpoints)")

    print("Building tiles ...")
    tiles = build_tiles(collector.nodes, collector.ways, intersections)
    print(f"  {len(tiles)} S2 level-{S2_LEVEL} tiles")

    write_tiles(tiles, Path(args.out))
    write_viewer_json(tiles, collector.nodes, Path(args.viewer))


if __name__ == "__main__":
    main()
