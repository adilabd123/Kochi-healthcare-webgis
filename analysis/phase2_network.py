#!/usr/bin/env python3
"""
Phase 2 - Walking network accessibility to healthcare (Kochi Healthcare WebGIS)
==============================================================================

Plain-language idea
    Imagine pouring water into the street network at every healthcare facility
    at the same moment. The water spreads along footpaths and streets at
    walking speed. Wherever it arrives first tells you (a) which facility is
    nearest by walking and (b) how many minutes it takes. That "pouring" is a
    multi-source Dijkstra shortest-path search on the walk graph.

What the script does
    1. Loads the walk graph (analysis/kochi_walk_2km.graphml), projects it to
       EPSG:32643 and turns it into an undirected street network (people can
       walk both ways along a footpath).
    2. Loads study area, healthcare points and buildings from PostGIS tables
       written by phase1_etl.py (or from the local Phase-1 files, --source local).
    3. Snaps every facility and every building (representative point) to its
       nearest walk node. The snap distance is counted as extra walking.
    4. For three facility sets
          all       - every healthcare point
          hospital  - type = hospital
          clinical  - all except pharmacy and laboratory (places you are treated)
       runs one multi-source Dijkstra and gets, for every node, the network
       distance (m) to the nearest facility in that set and which facility it is.
    5. Converts distance to time at each walking speed (default 4.8 km/h and
       3.6 km/h elderly) and builds 5/10/15-minute service areas: every street
       segment reachable within the time budget (partial segments included)
       buffered by 50 m and dissolved. Polygons in EPSG:32643 and EPSG:4326.
    6. Summarises building coverage, area coverage of the study area, healthcare
       points within reach of a hospital, straight-line vs network comparison and
       per-facility 15-minute catchments.
    7. Saves analysis/phase2_summary.json, analysis/phase2_accessibility_32643.gpkg,
       analysis/phase2_service_areas_4326.geojson,
       analysis/phase2_facility_access_4326.geojson
       and uploads service_areas_*, building_access_*, facility_access_*,
       node_access_* (each in 32643 and 4326, with GiST indexes) to PostGIS.

Usage
    python analysis/phase2_network.py
    python analysis/phase2_network.py --speeds 4.8,3.6 --minutes 5,10,15 --edge-buffer 50
    python analysis/phase2_network.py --source local --no-upload
"""
from __future__ import annotations

import argparse
import heapq
import json
import math
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import numpy as np
import osmnx as ox
import pandas as pd
from shapely.geometry import LineString, MultiLineString, Point
from shapely.ops import linemerge, substring, unary_union
from sqlalchemy import text

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from phase1_etl import (  # noqa: E402  (reuse Phase-1 config + loaders)
    BUILDINGS_GPKG,
    DROP_SPATIAL_INDEX_SQL,
    GRAPHML_FILE,
    add_id,
    db_settings,
    load_healthcare,
    load_study_area,
    make_engine,
    to_multi,
)

UTM43N = 32643
WGS84 = 4326

SUMMARY_JSON = SCRIPT_DIR / "phase2_summary.json"
GPKG_32643 = SCRIPT_DIR / "phase2_accessibility_32643.gpkg"
SERVICE_AREAS_GEOJSON = SCRIPT_DIR / "phase2_service_areas_4326.geojson"
FACILITY_GEOJSON = SCRIPT_DIR / "phase2_facility_access_4326.geojson"

NON_CLINICAL_TYPES = {"pharmacy", "laboratory"}
FACILITY_SETS = {
    "all": "All healthcare points (amenity=hospital|clinic|doctors, healthcare=*)",
    "hospital": "Healthcare points with type = hospital",
    "clinical": "All healthcare points except type pharmacy and laboratory",
}
SNAP_WARN_M = 250.0


def log(msg: str) -> None:
    print(msg, flush=True)


def speed_tag(speed_kmh: float) -> str:
    return f"{speed_kmh:g}".replace(".", "p")          # 4.8 -> "4p8"


def metres_per_minute(speed_kmh: float) -> float:
    return speed_kmh * 1000.0 / 60.0


def as_geometry_column(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if gdf.geometry.name != "geometry":
        gdf = gdf.rename_geometry("geometry")
    return gdf


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #
def load_inputs(source: str, engine):
    if source == "postgis":
        study = gpd.read_postgis("SELECT id, geom FROM study_area_32643", engine, geom_col="geom")
        healthcare = gpd.read_postgis(
            "SELECT id, osm_type, osm_id, name, type, geom FROM healthcare_32643 ORDER BY id",
            engine, geom_col="geom")
        buildings = gpd.read_postgis(
            "SELECT id, osm_id, building, name, area_m2, geom FROM buildings_32643 ORDER BY id",
            engine, geom_col="geom")
    else:
        study = load_study_area()
        healthcare = load_healthcare(study.geometry.iloc[0])
        if not BUILDINGS_GPKG.exists():
            raise SystemExit(f"Missing {BUILDINGS_GPKG}; run phase1_etl.py first.")
        b = gpd.read_file(BUILDINGS_GPKG, layer="buildings").to_crs(UTM43N)
        buildings = add_id(b.drop(columns=["id"], errors="ignore"))
        for col in ("osm_id", "building", "name", "area_m2"):
            if col not in buildings.columns:
                buildings[col] = None
        buildings = buildings[["id", "osm_id", "building", "name", "area_m2", "geometry"]]

    study, healthcare, buildings = (as_geometry_column(g.to_crs(UTM43N))
                                    for g in (study, healthcare, buildings))
    study_geom = unary_union(list(study.geometry))
    healthcare["type"] = healthcare["type"].fillna("other").astype(str)
    return study_geom, healthcare.reset_index(drop=True), buildings.reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Graph
# --------------------------------------------------------------------------- #
def load_network():
    if not GRAPHML_FILE.exists():
        raise SystemExit(f"Missing {GRAPHML_FILE}; run phase1_etl.py first.")
    G = ox.load_graphml(GRAPHML_FILE)
    Gp = ox.project_graph(G, to_crs=f"EPSG:{UTM43N}")
    nodes, edges = ox.graph_to_gdfs(Gp, nodes=True, edges=True, fill_edge_geometry=True)

    nodes = nodes.reset_index().rename(columns={"osmid": "node_id"})
    nodes = gpd.GeoDataFrame(nodes[["node_id", "geometry"]], geometry="geometry", crs=f"EPSG:{UTM43N}")
    xy = {int(n): (p.x, p.y) for n, p in zip(nodes["node_id"], nodes.geometry)}

    # Undirected: keep the shortest edge between each node pair.
    e = edges.reset_index()
    best: dict[tuple[int, int], tuple[float, int, int, object]] = {}
    for u, v, length, geom in zip(e["u"], e["v"], e["length"], e.geometry):
        u, v, length = int(u), int(v), float(length)
        if u == v or not math.isfinite(length):
            continue
        key = (u, v) if u < v else (v, u)
        if key not in best or length < best[key][0]:
            best[key] = (length, u, v, geom)

    adj: dict[int, list[tuple[int, float]]] = defaultdict(list)
    rows = []
    for (a, b), (length, u, v, geom) in best.items():
        adj[a].append((b, length))
        adj[b].append((a, length))
        rows.append((u, v, length, oriented_line(geom, xy[u], xy[v])))
    und = gpd.GeoDataFrame(rows, columns=["u", "v", "length_m", "geometry"],
                           geometry="geometry", crs=f"EPSG:{UTM43N}")
    log(f"  graph: {len(nodes)} nodes | {len(e)} directed edges -> {len(und)} undirected segments")
    return nodes, und, adj


def oriented_line(geom, xy_u, xy_v) -> LineString:
    """Return the edge geometry as a LineString running from node u to node v."""
    if geom is not None and geom.geom_type == "MultiLineString":
        geom = linemerge(geom)
    if geom is None or geom.is_empty or geom.geom_type != "LineString":
        return LineString([xy_u, xy_v])
    x0, y0 = geom.coords[0]
    if math.dist((x0, y0), xy_v) < math.dist((x0, y0), xy_u):
        return LineString(list(geom.coords)[::-1])
    return geom


def snap_to_nodes(points: gpd.GeoSeries, nodes: gpd.GeoDataFrame) -> pd.DataFrame:
    pts = gpd.GeoDataFrame({"_row": range(len(points))}, geometry=points.values, crs=points.crs)
    j = gpd.sjoin_nearest(pts, nodes, how="left", distance_col="snap_m")
    j = j.drop_duplicates(subset="_row").sort_values("_row")
    return pd.DataFrame({"node_id": j["node_id"].astype("int64").values,
                         "snap_m": j["snap_m"].astype(float).values})


def multi_source_dijkstra(adj, seeds: dict[int, tuple[float, int]]):
    """seeds: node -> (initial distance, facility id). Returns (dist, label) dicts."""
    dist: dict[int, float] = {}
    label: dict[int, int] = {}
    heap = [(d, n, lab) for n, (d, lab) in seeds.items()]
    heapq.heapify(heap)
    while heap:
        d, n, lab = heapq.heappop(heap)
        if n in dist:
            continue
        dist[n] = d
        label[n] = lab
        for nb, w in adj.get(n, ()):
            if nb not in dist:
                heapq.heappush(heap, (d + w, nb, lab))
    return dist, label


# --------------------------------------------------------------------------- #
# Service areas
# --------------------------------------------------------------------------- #
def reached_segments(und: gpd.GeoDataFrame, dist: dict[int, float], budget_m: float) -> list:
    """All street pieces reachable within budget_m (full and partial segments)."""
    pieces = []
    inf = math.inf
    for u, v, length, geom in zip(und["u"], und["v"], und["length_m"], und.geometry):
        du, dv = dist.get(u, inf), dist.get(v, inf)
        if du > budget_m and dv > budget_m:
            continue
        if length <= 0:
            continue
        reach_u = min(length, budget_m - du) if du <= budget_m else 0.0
        reach_v = min(length, budget_m - dv) if dv <= budget_m else 0.0
        if reach_u + reach_v >= length:
            pieces.append(geom)
            continue
        if reach_u > 0:
            pieces.append(substring(geom, 0.0, reach_u / length, normalized=True))
        if reach_v > 0:
            pieces.append(substring(geom, 1.0 - reach_v / length, 1.0, normalized=True))
    return [p for p in pieces if p is not None and not p.is_empty and p.length > 0]


def service_area_polygon(pieces: list, edge_buffer_m: float):
    if not pieces:
        return None
    import shapely as _shp
    _seg_list = [g for g in pieces if g.geom_type == "LineString" and not g.is_empty]
    _merged = _shp.line_merge(_shp.multilinestrings(_seg_list))
    _parts = list(_merged.geoms) if hasattr(_merged, 'geoms') else [_merged]
    _parts = [_shp.simplify(g, 1.5) for g in _parts if not g.is_empty]
    _bufs = _shp.buffer(_parts, edge_buffer_m, quad_segs=3)
    poly = _shp.union_all(_bufs).simplify(1.0, preserve_topology=True)
    return to_multi(poly)


# --------------------------------------------------------------------------- #
# Helpers for statistics
# --------------------------------------------------------------------------- #
def pct(part: float, whole: float) -> float | None:
    return round(100.0 * part / whole, 2) if whole else None


def q(series: pd.Series, p: float) -> float | None:
    s = series.dropna()
    return round(float(s.quantile(p)), 1) if len(s) else None


def band(time_min: float, minutes: list[int]) -> str:
    if time_min is None or not math.isfinite(time_min):
        return "unreachable"
    lo = 0
    for m in minutes:
        if time_min <= m:
            return f"{lo}-{m} min"
        lo = m
    return f">{minutes[-1]} min"


def json_default(obj):
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return None if not math.isfinite(float(obj)) else float(obj)
    raise TypeError(f"Not JSON serialisable: {type(obj)}")


# --------------------------------------------------------------------------- #
# PostGIS upload
# --------------------------------------------------------------------------- #
def upload(gdf_utm: gpd.GeoDataFrame, base: str, engine) -> None:
    for srid in (UTM43N, WGS84):
        table = f"{base}_{srid}"
        g = gdf_utm.to_crs(srid).rename_geometry("geom")
        g.to_postgis(table, engine, schema="public", if_exists="replace", index=False, chunksize=10000)
        with engine.begin() as conn:
            for idx in conn.execute(DROP_SPATIAL_INDEX_SQL, {"table": table}).scalars().all():
                conn.execute(text(f'DROP INDEX IF EXISTS public."{idx}"'))
            conn.execute(text(f'ALTER TABLE public."{table}" ADD PRIMARY KEY (id)'))
            conn.execute(text(f'CREATE INDEX "{table}_geom_gist" ON public."{table}" USING GIST (geom)'))
            conn.execute(text(f'ANALYZE public."{table}"'))
        log(f"  {table:<26} {len(g):>7} rows (PK + GiST)")


# --------------------------------------------------------------------------- #
# Main analysis
# --------------------------------------------------------------------------- #
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Phase 2 walking accessibility to healthcare.")
    p.add_argument("--source", choices=["postgis", "local"], default="postgis",
                   help="Where to read study area / healthcare / buildings (default postgis).")
    p.add_argument("--speeds", default="4.8,3.6",
                   help="Walking speeds in km/h, first = primary (default 4.8,3.6).")
    p.add_argument("--minutes", default="5,10,15", help="Time thresholds in minutes (default 5,10,15).")
    p.add_argument("--edge-buffer", type=float, default=50.0,
                   help="Buffer (m) around reached street segments for service areas (default 50).")
    p.add_argument("--no-upload", action="store_true", help="Do not write results to PostGIS.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    speeds = [float(s) for s in args.speeds.split(",") if s.strip()]
    minutes = sorted(int(m) for m in args.minutes.split(",") if m.strip())
    primary = speeds[0]
    t_start = time.perf_counter()

    engine = None
    if args.source == "postgis" or not args.no_upload:
        cfg = db_settings()
        engine = make_engine(cfg)
        log(f"PostGIS: {cfg['username']}@{cfg['host']}:{cfg['port']}/{cfg['database']}")

    log("\n[1/6] Loading inputs")
    study, healthcare, buildings = load_inputs(args.source, engine)
    study_km2 = study.area / 1e6
    log(f"  study area {study_km2:.4f} km2 | {len(healthcare)} healthcare points | {len(buildings)} buildings")

    log("\n[2/6] Loading and projecting walk network")
    nodes, und, adj = load_network()

    log("\n[3/6] Snapping facilities and buildings to the network")
    hc_snap = snap_to_nodes(healthcare.geometry, nodes)
    healthcare["node_id"] = hc_snap["node_id"].values
    healthcare["snap_m"] = hc_snap["snap_m"].round(1).values
    b_pts = buildings.geometry.representative_point()
    b_snap = snap_to_nodes(b_pts, nodes)
    buildings["node_id"] = b_snap["node_id"].values
    buildings["snap_m"] = b_snap["snap_m"].round(1).values
    far = healthcare[healthcare["snap_m"] > SNAP_WARN_M]
    log(f"  facility snap distance: median {healthcare['snap_m'].median():.1f} m, "
        f"max {healthcare['snap_m'].max():.1f} m ({len(far)} > {SNAP_WARN_M:.0f} m)")
    log(f"  building snap distance: median {buildings['snap_m'].median():.1f} m, "
        f"p95 {buildings['snap_m'].quantile(0.95):.1f} m")

    masks = {
        "all": pd.Series(True, index=healthcare.index),
        "hospital": healthcare["type"].eq("hospital"),
        "clinical": ~healthcare["type"].isin(NON_CLINICAL_TYPES),
    }

    node_access = nodes.copy()
    service_rows = []
    summary_sets: dict[str, dict] = {}

    log("\n[4/6] Network distances and service areas")
    for set_name, mask in masks.items():
        fac = healthcare[mask]
        if fac.empty:
            log(f"  [{set_name}] no facilities - skipped")
            continue

        seeds: dict[int, tuple[float, int]] = {}
        for fid, node, snap in zip(fac["id"], fac["node_id"], fac["snap_m"]):
            if node not in seeds or snap < seeds[node][0]:
                seeds[int(node)] = (float(snap), int(fid))
        dist, label = multi_source_dijkstra(adj, seeds)

        node_access[f"{set_name}_dist_m"] = node_access["node_id"].map(dist).round(1)
        node_access[f"{set_name}_fac_id"] = node_access["node_id"].map(label).astype("Int64")

        # Buildings: network distance = building snap + node distance.
        b_dist = buildings["snap_m"] + buildings["node_id"].map(dist)
        buildings[f"{set_name}_dist_m"] = b_dist.round(1)
        buildings[f"{set_name}_fac_id"] = buildings["node_id"].map(label).astype("Int64")

        # Straight-line distance to the nearest facility of this set.
        ej = gpd.sjoin_nearest(
            gpd.GeoDataFrame({"_row": range(len(b_pts))}, geometry=b_pts.values, crs=b_pts.crs),
            fac[["geometry"]], how="left", distance_col="euclid_m",
        ).drop_duplicates(subset="_row").sort_values("_row")
        buildings[f"{set_name}_euclid_m"] = ej["euclid_m"].round(1).values

        # Healthcare points: distance to nearest facility of this set (0 if it is one).
        h_dist = healthcare["snap_m"] + healthcare["node_id"].map(dist)
        h_dist[mask] = 0.0
        healthcare[f"{set_name}_dist_m"] = h_dist.round(1)

        detour = (b_dist / buildings[f"{set_name}_euclid_m"]).where(buildings[f"{set_name}_euclid_m"] >= 100)
        set_summary = {
            "description": FACILITY_SETS[set_name],
            "facilities": int(len(fac)),
            "distinct_snap_nodes": int(len(seeds)),
            "buildings_unreachable": int(b_dist.isna().sum()),
            "network_dist_m": {"median": q(b_dist, 0.5), "p90": q(b_dist, 0.9), "max": q(b_dist, 1.0)},
            "straight_line_dist_m": {"median": q(buildings[f"{set_name}_euclid_m"], 0.5),
                                     "p90": q(buildings[f"{set_name}_euclid_m"], 0.9)},
            "detour_ratio_network_over_straight_line": {
                "note": "buildings with straight-line distance >= 100 m",
                "median": None if detour.dropna().empty else round(float(detour.median()), 3),
                "p90": None if detour.dropna().empty else round(float(detour.quantile(0.9)), 3),
            },
            "by_speed": {},
        }

        for speed in speeds:
            tag = speed_tag(speed)
            mpm = metres_per_minute(speed)
            t_min = b_dist / mpm
            buildings[f"{set_name}_t{tag}_min"] = t_min.round(2)
            buildings[f"{set_name}_band{tag}"] = [band(t, minutes) for t in t_min]
            healthcare[f"{set_name}_t{tag}_min"] = (h_dist / mpm).round(2)

            speed_summary = {
                "metres_per_minute": round(mpm, 2),
                "time_min": {"median": q(t_min, 0.5), "p90": q(t_min, 0.9)},
                "thresholds": {},
            }
            for m in minutes:
                budget = mpm * m
                pieces = reached_segments(und, dist, budget)
                poly = service_area_polygon(pieces, args.edge_buffer)
                area_km2 = poly.area / 1e6 if poly is not None else 0.0
                in_study_km2 = poly.intersection(study).area / 1e6 if poly is not None else 0.0
                n_b = int((b_dist <= budget).sum())
                n_h = int((h_dist <= budget).sum())
                n_euclid = int((buildings[f"{set_name}_euclid_m"] <= budget).sum())
                row = {
                    "facility_set": set_name,
                    "speed_kmh": speed,
                    "minutes": m,
                    "distance_m": round(budget, 1),
                    "area_km2": round(area_km2, 4),
                    "area_in_study_km2": round(in_study_km2, 4),
                    "pct_of_study_area": pct(in_study_km2, study_km2),
                    "buildings_within": n_b,
                    "pct_buildings": pct(n_b, len(buildings)),
                    "healthcare_points_within": n_h,
                    "pct_healthcare_points": pct(n_h, len(healthcare)),
                }
                speed_summary["thresholds"][f"{m}_min"] = {
                    **{k: v for k, v in row.items() if k not in ("facility_set", "speed_kmh", "minutes")},
                    "buildings_within_straight_line": n_euclid,
                    "pct_buildings_straight_line": pct(n_euclid, len(buildings)),
                    "straight_line_overestimate_pct_points": (
                        round(pct(n_euclid, len(buildings)) - pct(n_b, len(buildings)), 2)
                        if len(buildings) else None),
                }
                service_rows.append({**row, "geometry": poly})
            set_summary["by_speed"][f"{speed:g}_kmh"] = speed_summary

        # Per-facility catchment: buildings whose nearest facility (by walking)
        # is this one and that are within the largest threshold at primary speed.
        budget_max = metres_per_minute(primary) * minutes[-1]
        within = buildings[b_dist <= budget_max]
        catch = within[f"{set_name}_fac_id"].value_counts()
        col = f"{set_name}_catch{minutes[-1]}_{speed_tag(primary)}"
        healthcare[col] = healthcare["id"].map(catch).where(mask)
        healthcare[col] = healthcare[col].fillna(0).where(mask).astype("Int64")
        set_summary["catchment_top5"] = [
            {"id": int(fid),
             "name": healthcare.loc[healthcare["id"] == fid, "name"].iloc[0],
             "type": healthcare.loc[healthcare["id"] == fid, "type"].iloc[0],
             "buildings": int(n)}
            for fid, n in catch.head(5).items()
        ]
        summary_sets[set_name] = set_summary

        p15 = set_summary["by_speed"][f"{primary:g}_kmh"]["thresholds"][f"{minutes[-1]}_min"]
        log(f"  [{set_name:<8}] {len(fac):>3} facilities | {minutes[-1]} min @ {primary:g} km/h: "
            f"{p15['pct_buildings']}% buildings, {p15['pct_of_study_area']}% of study area "
            f"(straight-line would say {p15['pct_buildings_straight_line']}%)")

    log("\n[5/6] Writing local outputs")
    service = gpd.GeoDataFrame(service_rows, geometry="geometry", crs=f"EPSG:{UTM43N}")
    service = service[service.geometry.notna()].reset_index(drop=True)
    service.insert(0, "id", range(1, len(service) + 1))

    healthcare["is_hospital"] = masks["hospital"]
    healthcare["is_clinical"] = masks["clinical"]
    facility_access = healthcare.copy()

    building_access = buildings.drop(columns=["geometry"]).copy()
    building_access = gpd.GeoDataFrame(building_access, geometry=buildings.geometry.map(to_multi),
                                       crs=f"EPSG:{UTM43N}")
    node_access.insert(0, "id", range(1, len(node_access) + 1))

    if GPKG_32643.exists():
        GPKG_32643.unlink()
    service.to_file(GPKG_32643, layer="service_areas", driver="GPKG")
    building_access.to_file(GPKG_32643, layer="building_access", driver="GPKG")
    facility_access.to_file(GPKG_32643, layer="facility_access", driver="GPKG")
    node_access.to_file(GPKG_32643, layer="node_access", driver="GPKG")
    for path, gdf in ((SERVICE_AREAS_GEOJSON, service), (FACILITY_GEOJSON, facility_access)):
        if path.exists():
            path.unlink()
        gdf.to_crs(WGS84).to_file(path, driver="GeoJSON")
    log(f"  {GPKG_32643.name} (service_areas, building_access, facility_access, node_access)")
    log(f"  {SERVICE_AREAS_GEOJSON.name}, {FACILITY_GEOJSON.name}")

    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "inputs": {
            "source": args.source,
            "graphml": GRAPHML_FILE.name,
            "study_area_km2": round(study_km2, 4),
            "healthcare_points": int(len(healthcare)),
            "healthcare_by_type": {k: int(v) for k, v in healthcare["type"].value_counts().items()},
            "buildings": int(len(buildings)),
            "walk_nodes": int(len(nodes)),
            "walk_segments_undirected": int(len(und)),
        },
        "parameters": {
            "speeds_kmh": speeds,
            "primary_speed_kmh": primary,
            "minutes": minutes,
            "service_area_edge_buffer_m": args.edge_buffer,
            "metric_crs": f"EPSG:{UTM43N}",
            "network_distance": "snap(building->node) + shortest path + snap(node->facility), undirected walk graph",
        },
        "snapping": {
            "facility_snap_m": {"median": q(healthcare["snap_m"], 0.5), "max": q(healthcare["snap_m"], 1.0),
                                f"count_over_{SNAP_WARN_M:.0f}m": int(len(far))},
            "building_snap_m": {"median": q(buildings["snap_m"], 0.5), "p95": q(buildings["snap_m"], 0.95)},
        },
        "facility_sets": summary_sets,
        "limitations": [
            "Healthcare points were extracted only inside the study area, so buildings near its "
            "edge may in reality be closer to facilities just outside it (edge effect).",
            "Building access uses one representative point per footprint and straight-line snapping "
            "to the nearest walk node.",
            "Service-area polygons are reached street segments buffered by the edge buffer; they "
            "approximate, not exactly delimit, the walkable area. Building coverage figures come "
            "from each building's own network distance, not from overlaying the polygons, so a "
            "few counted buildings set back more than the buffer from a street fall outside them.",
            "Walking speed is constant; slopes, crossings, footpath condition and weather are ignored.",
            "OSM 'hospital' tags may include clinics or primary health centres (see Q3).",
        ],
        "outputs": [GPKG_32643.name, SERVICE_AREAS_GEOJSON.name, FACILITY_GEOJSON.name, SUMMARY_JSON.name],
        "runtime_s": None,
    }

    if not args.no_upload:
        log("\n[6/6] Uploading to PostGIS")
        upload(service, "service_areas", engine)
        upload(building_access, "building_access", engine)
        upload(facility_access, "facility_access", engine)
        upload(node_access, "node_access", engine)
        summary["postgis_tables"] = [f"{b}_{s}" for b in
                                     ("service_areas", "building_access", "facility_access", "node_access")
                                     for s in (UTM43N, WGS84)]
    else:
        log("\n[6/6] Upload skipped (--no-upload)")

    summary["runtime_s"] = round(time.perf_counter() - t_start, 1)
    SUMMARY_JSON.write_text(json.dumps(summary, indent=2, default=json_default, ensure_ascii=False),
                            encoding="utf-8")
    log(f"\nSaved {SUMMARY_JSON.name}")

    log("\nCoverage table (share of buildings within each walking time):")
    log(f"  {'set':<9} {'speed':>9} " + " ".join(f"{m:>6}min" for m in minutes) + "   area15%")
    for s_name, s in summary_sets.items():
        for sp_key, sp in s["by_speed"].items():
            cells = " ".join(f"{sp['thresholds'][f'{m}_min']['pct_buildings']:>6}%" for m in minutes)
            a15 = sp["thresholds"][f"{minutes[-1]}_min"]["pct_of_study_area"]
            log(f"  {s_name:<9} {sp_key:>9} {cells}   {a15}%")
    log(f"\nPhase 2 finished in {summary['runtime_s']} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
