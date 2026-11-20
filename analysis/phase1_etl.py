#!/usr/bin/env python3
"""
Phase 1 ETL - Kochi Healthcare WebGIS
=====================================

Loads the Step-0 outputs, downloads buildings and the walking network from
OpenStreetMap, and writes everything to PostGIS in two coordinate systems.

Inputs (created by step0_verify_kochi.py)
    analysis/study_area.geojson
    analysis/healthcare_features.geojson

Local outputs
    analysis/kochi_walk_2km.graphml   walking graph (study area + 2 km buffer)
    analysis/kochi_walk_2km.gpkg      layers: walk_nodes, walk_edges   (EPSG:4326)
    analysis/kochi_buildings.gpkg     layer : buildings                (EPSG:4326)

PostGIS tables (public schema), each in two versions:
    study_area_4326   study_area_32643
    healthcare_4326   healthcare_32643
    buildings_4326    buildings_32643
    walk_nodes_4326   walk_nodes_32643
    walk_edges_4326   walk_edges_32643

Every table gets an integer primary key "id" and a geometry column "geom".
NO spatial (GiST) indexes are left on any table: GeoPandas/GeoAlchemy2
create one automatically during to_postgis(), so this script drops them
straight after each upload. That keeps the Q2 "before index" benchmark honest.

Connection settings come from the project's .env file (environment variables
override it):
    POSTGRES_HOST (default localhost)  POSTGIS_PORT (default 5433)
    POSTGRES_DB   POSTGRES_USER        POSTGRES_PASSWORD

Usage
    python analysis/phase1_etl.py                 # full run (downloads from OSM)
    python analysis/phase1_etl.py --reuse-local   # reuse saved .gpkg/.graphml files
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

import geopandas as gpd
import osmnx as ox
import pandas as pd
import requests
from shapely.geometry import MultiLineString, MultiPolygon, Polygon
from shapely.ops import unary_union
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine

# --------------------------------------------------------------------------- #
# Paths and constants
# --------------------------------------------------------------------------- #
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
ENV_FILE = PROJECT_ROOT / ".env"

STUDY_AREA_FILE = SCRIPT_DIR / "study_area.geojson"
HEALTHCARE_FILE = SCRIPT_DIR / "healthcare_features.geojson"
GRAPHML_FILE = SCRIPT_DIR / "kochi_walk_2km.graphml"
NETWORK_GPKG = SCRIPT_DIR / "kochi_walk_2km.gpkg"
BUILDINGS_GPKG = SCRIPT_DIR / "kochi_buildings.gpkg"

WGS84 = 4326
UTM43N = 32643
TARGET_SRIDS = (WGS84, UTM43N)

NETWORK_BUFFER_M = 2000.0
HEALTH_DEDUP_M = 50.0
AMENITY_TYPES = {"hospital", "clinic", "doctors"}

TABLE_BASES = ["study_area", "healthcare", "buildings", "walk_nodes", "walk_edges"]


class NetworkUnavailable(RuntimeError):
    """Overpass / Nominatim could not be reached."""


NETWORK_ERRORS = (requests.exceptions.ConnectionError, requests.exceptions.Timeout)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def load_env_file(path: Path) -> dict[str, str]:
    """Minimal .env parser: KEY=VALUE lines, '#' comments, optional quotes."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        elif " #" in val:
            val = val.split(" #", 1)[0].rstrip()
        values[key] = val
    return values


def db_settings() -> dict[str, str]:
    file_vals = load_env_file(ENV_FILE)

    def get(key: str, default: str | None = None) -> str | None:
        return os.environ.get(key) or file_vals.get(key) or default

    settings = {
        "host": get("POSTGRES_HOST", "localhost"),
        "port": get("POSTGIS_PORT", "5433"),
        "database": get("POSTGRES_DB", "kochi_health"),
        "username": get("POSTGRES_USER", "webgis"),
        "password": get("POSTGRES_PASSWORD"),
    }
    if not settings["password"]:
        raise SystemExit(
            f"POSTGRES_PASSWORD not found. Create {ENV_FILE} (Copy-Item .env.example .env) "
            "or set the environment variable."
        )
    return settings


def make_engine(cfg: dict[str, str]) -> Engine:
    url = URL.create(
        "postgresql+psycopg2",
        username=cfg["username"],
        password=cfg["password"],
        host=cfg["host"],
        port=int(cfg["port"]),
        database=cfg["database"],
    )
    return create_engine(url, connect_args={"connect_timeout": 10}, future=True)


def configure_osmnx() -> None:
    cache_dir = SCRIPT_DIR / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    ox.settings.use_cache = True
    ox.settings.cache_folder = str(cache_dir)
    ox.settings.log_console = False
    ox.settings.requests_timeout = 300


# --------------------------------------------------------------------------- #
# Generic helpers
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    print(msg, flush=True)


def to_multi(geom):
    """Promote single parts to Multi* so each PostGIS column has one geometry type."""
    if isinstance(geom, Polygon):
        return MultiPolygon([geom])
    if geom is not None and geom.geom_type == "LineString":
        return MultiLineString([geom])
    return geom


def clean_text(value) -> str | None:
    if isinstance(value, (list, tuple, set)):
        parts = [clean_text(v) for v in value]
        parts = [p for p in parts if p]
        return "; ".join(dict.fromkeys(parts)) if parts else None
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    s = re.sub(r"\s+", " ", str(value)).strip()
    return s or None


def normalise_name(value) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower()) if isinstance(value, str) else ""


def standardise_osm_index(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """osmnx 2.x index (element, id) -> columns osm_type, osm_id."""
    gdf = gdf.reset_index()
    rename = {"element": "osm_type", "element_type": "osm_type", "id": "osm_id", "osmid": "osm_id"}
    return gdf.rename(columns={k: v for k, v in rename.items() if k in gdf.columns})


def add_id(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    gdf = gdf.reset_index(drop=True)
    gdf.insert(0, "id", range(1, len(gdf) + 1))
    return gdf


def write_gpkg(gdf: gpd.GeoDataFrame, path: Path, layer: str) -> None:
    gdf.to_crs(WGS84).to_file(path, layer=layer, driver="GPKG")


# --------------------------------------------------------------------------- #
# 1. Inputs from Step 0
# --------------------------------------------------------------------------- #
def load_study_area() -> gpd.GeoDataFrame:
    if not STUDY_AREA_FILE.exists():
        raise SystemExit(f"Missing {STUDY_AREA_FILE}. Run analysis/step0_verify_kochi.py first.")
    raw = gpd.read_file(STUDY_AREA_FILE)
    if raw.crs is None:
        raw = raw.set_crs(WGS84)
    utm = raw.to_crs(UTM43N)
    merged = unary_union([g.buffer(0) for g in utm.geometry if g is not None and not g.is_empty])
    source = clean_text(raw["source"].iloc[0]) if "source" in raw.columns else "step0"
    gdf = gpd.GeoDataFrame(
        {
            "name": ["Edappally + Palarivattom + Kaloor"],
            "source": [source],
            "area_km2_utm": [round(merged.area / 1e6, 4)],
        },
        geometry=[to_multi(merged)],
        crs=UTM43N,
    )
    log(f"  study_area: 1 polygon, {gdf['area_km2_utm'].iloc[0]} km2 (source: {source})")
    return add_id(gdf)


def derive_type(row) -> str:
    category = clean_text(row.get("category"))
    if category:
        return category.split("=", 1)[1].lower() if category.startswith("healthcare=") else category.lower()
    amenity = clean_text(row.get("amenity"))
    if amenity and amenity.lower() in AMENITY_TYPES:
        return amenity.lower()
    healthcare = clean_text(row.get("healthcare"))
    return healthcare.lower() if healthcare else "other"


def load_healthcare(study_utm) -> gpd.GeoDataFrame:
    if not HEALTHCARE_FILE.exists():
        raise SystemExit(f"Missing {HEALTHCARE_FILE}. Run analysis/step0_verify_kochi.py first.")
    raw = gpd.read_file(HEALTHCARE_FILE)
    if raw.crs is None:
        raw = raw.set_crs(WGS84)
    gdf = raw.to_crs(UTM43N)
    n_in = len(gdf)

    # Polygons / lines -> centroid (computed in metres, so it is a true centroid).
    non_point = ~gdf.geometry.geom_type.isin(["Point"])
    gdf.loc[non_point, "geometry"] = gdf.loc[non_point, "geometry"].centroid
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].copy()

    for col in ("osm_type", "osm_id", "name", "amenity", "healthcare", "category"):
        if col not in gdf.columns:
            gdf[col] = None
    gdf["name"] = gdf["name"].map(clean_text)
    gdf["type"] = gdf.apply(derive_type, axis=1)
    gdf["amenity"] = gdf["amenity"].map(clean_text)
    gdf["healthcare"] = gdf["healthcare"].map(clean_text)
    gdf["osm_type"] = gdf["osm_type"].map(clean_text)

    # Keep points inside the study area.
    gdf = gdf[gdf.geometry.apply(study_utm.covers)].copy()

    # Dedup 1: same OSM element.
    has_osm = gdf["osm_id"].notna()
    dup_elem = has_osm & gdf.duplicated(subset=["osm_type", "osm_id"])
    gdf = gdf[~dup_elem].copy()

    # Dedup 2: same normalised name within HEALTH_DEDUP_M metres (keep first).
    gdf["_nn"] = gdf["name"].map(normalise_name)
    drop = set()
    for _, grp in gdf[gdf["_nn"] != ""].groupby("_nn"):
        kept = []
        for idx, row in grp.iterrows():
            if any(row.geometry.distance(k) <= HEALTH_DEDUP_M for k in kept):
                drop.add(idx)
            else:
                kept.append(row.geometry)
    gdf = gdf.drop(index=list(drop))

    gdf["osm_id"] = pd.to_numeric(gdf["osm_id"], errors="coerce").astype("Int64")
    out = gdf[["osm_type", "osm_id", "name", "type", "amenity", "healthcare", "geometry"]]
    out = gpd.GeoDataFrame(out, geometry="geometry", crs=UTM43N)
    out = out.sort_values(["type", "name"], na_position="last")
    log(f"  healthcare: {n_in} read -> {len(out)} kept "
        f"({int(non_point.sum())} polygon/line features converted to centroids, "
        f"{int(dup_elem.sum())} same-element and {len(drop)} same-name<{HEALTH_DEDUP_M:.0f} m duplicates removed)")
    return add_id(out)


# --------------------------------------------------------------------------- #
# 2. Buildings
# --------------------------------------------------------------------------- #
def download_buildings(study_utm, reuse_local: bool) -> gpd.GeoDataFrame:
    if reuse_local and BUILDINGS_GPKG.exists():
        gdf = gpd.read_file(BUILDINGS_GPKG, layer="buildings").to_crs(UTM43N)
        log(f"  buildings: {len(gdf)} reused from {BUILDINGS_GPKG.name}")
        return add_id(gdf.drop(columns=["id"], errors="ignore"))

    study_wgs84 = gpd.GeoSeries([study_utm], crs=UTM43N).to_crs(WGS84).iloc[0]
    log("  Downloading building footprints from Overpass (can take a few minutes) ...")
    t0 = time.perf_counter()
    try:
        raw = ox.features_from_polygon(study_wgs84, tags={"building": True})
    except NETWORK_ERRORS as exc:
        raise NetworkUnavailable(f"Cannot reach Overpass API: {exc}") from exc
    raw = standardise_osm_index(raw).to_crs(UTM43N)
    n_raw = len(raw)

    raw = raw[raw.geometry.geom_type.isin(["Polygon", "MultiPolygon"])].copy()
    invalid = ~raw.geometry.is_valid
    raw.loc[invalid, "geometry"] = raw.loc[invalid, "geometry"].buffer(0)
    raw = raw[raw.geometry.notna() & ~raw.geometry.is_empty
              & raw.geometry.geom_type.isin(["Polygon", "MultiPolygon"])].copy()

    # A building belongs to the study area if its representative point is inside.
    inside = raw.geometry.representative_point().apply(study_utm.covers)
    raw = raw[inside].copy()

    for col in ("building", "name", "building:levels", "amenity"):
        if col not in raw.columns:
            raw[col] = None
    out = gpd.GeoDataFrame(
        {
            "osm_type": raw["osm_type"].map(clean_text),
            "osm_id": pd.to_numeric(raw["osm_id"], errors="coerce").astype("Int64"),
            "building": raw["building"].map(clean_text),
            "name": raw["name"].map(clean_text),
            "levels": raw["building:levels"].map(clean_text),
            "amenity": raw["amenity"].map(clean_text),
            "area_m2": raw.geometry.area.round(1),
        },
        geometry=raw.geometry.map(to_multi),
        crs=UTM43N,
    )
    out = out.drop_duplicates(subset=["osm_type", "osm_id"])
    log(f"  buildings: {n_raw} raw -> {len(out)} polygon footprints inside study area "
        f"({time.perf_counter() - t0:.1f} s)")
    write_gpkg(out, BUILDINGS_GPKG, "buildings")
    log(f"  saved {BUILDINGS_GPKG.name}")
    return add_id(out)


# --------------------------------------------------------------------------- #
# 3. Walking network (study area + 2 km)
# --------------------------------------------------------------------------- #
def graph_to_tables(G) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    nodes, edges = ox.graph_to_gdfs(G, nodes=True, edges=True, fill_edge_geometry=True)

    nodes = nodes.reset_index()
    nodes = nodes.rename(columns={"osmid": "node_id"})
    for col in ("street_count", "highway"):
        if col not in nodes.columns:
            nodes[col] = None
    nodes_out = gpd.GeoDataFrame(
        {
            "node_id": nodes["node_id"].astype("int64"),
            "lon": nodes["x"].astype(float),
            "lat": nodes["y"].astype(float),
            "street_count": pd.to_numeric(nodes["street_count"], errors="coerce").astype("Int64"),
            "highway": nodes["highway"].map(clean_text),
        },
        geometry=nodes.geometry,
        crs=nodes.crs,
    )

    edges = edges.reset_index()
    for col in ("osmid", "name", "highway", "oneway", "length"):
        if col not in edges.columns:
            edges[col] = None
    edges_out = gpd.GeoDataFrame(
        {
            "source": edges["u"].astype("int64"),     # from node_id
            "target": edges["v"].astype("int64"),     # to node_id
            "key": edges["key"].astype("int64"),
            "osm_way_id": edges["osmid"].map(clean_text),
            "name": edges["name"].map(clean_text),
            "highway": edges["highway"].map(clean_text),
            "oneway": edges["oneway"].map(lambda v: bool(v) if isinstance(v, bool) else None),
            "length_m": pd.to_numeric(edges["length"], errors="coerce").round(2),
        },
        geometry=edges.geometry,
        crs=edges.crs,
    )
    return nodes_out, edges_out


def download_walk_network(study_utm, reuse_local: bool):
    if reuse_local and NETWORK_GPKG.exists() and GRAPHML_FILE.exists():
        nodes = gpd.read_file(NETWORK_GPKG, layer="walk_nodes").to_crs(UTM43N)
        edges = gpd.read_file(NETWORK_GPKG, layer="walk_edges").to_crs(UTM43N)
        edges["geometry"] = edges.geometry.map(to_multi)
        log(f"  walk network: {len(nodes)} nodes / {len(edges)} edges reused from {NETWORK_GPKG.name}")
        return (add_id(nodes.drop(columns=["id"], errors="ignore")),
                add_id(edges.drop(columns=["id"], errors="ignore")))

    buffered_utm = study_utm.buffer(NETWORK_BUFFER_M)
    buffered_wgs84 = gpd.GeoSeries([buffered_utm], crs=UTM43N).to_crs(WGS84).iloc[0]
    log(f"  Downloading walk network for study area + {NETWORK_BUFFER_M / 1000:.0f} km "
        f"({buffered_utm.area / 1e6:.1f} km2) ...")
    t0 = time.perf_counter()
    try:
        G = ox.graph_from_polygon(buffered_wgs84, network_type="walk", simplify=True)
    except NETWORK_ERRORS as exc:
        raise NetworkUnavailable(f"Cannot reach Overpass API: {exc}") from exc
    log(f"  graph: {G.number_of_nodes()} nodes / {G.number_of_edges()} directed edges "
        f"({time.perf_counter() - t0:.1f} s)")

    ox.save_graphml(G, filepath=GRAPHML_FILE)
    log(f"  saved {GRAPHML_FILE.name}")

    nodes, edges = graph_to_tables(G)
    if NETWORK_GPKG.exists():
        NETWORK_GPKG.unlink()
    write_gpkg(nodes, NETWORK_GPKG, "walk_nodes")
    write_gpkg(edges, NETWORK_GPKG, "walk_edges")
    log(f"  saved {NETWORK_GPKG.name} (layers walk_nodes, walk_edges)")

    nodes = nodes.to_crs(UTM43N)
    edges = edges.to_crs(UTM43N)
    edges["geometry"] = edges.geometry.map(to_multi)
    return add_id(nodes), add_id(edges)


# --------------------------------------------------------------------------- #
# 4. PostGIS upload
# --------------------------------------------------------------------------- #
DROP_SPATIAL_INDEX_SQL = text("""
    SELECT ic.relname AS index_name
    FROM pg_index x
    JOIN pg_class ic ON ic.oid = x.indexrelid
    JOIN pg_class tc ON tc.oid = x.indrelid
    JOIN pg_namespace n ON n.oid = tc.relnamespace
    JOIN pg_am am ON am.oid = ic.relam
    WHERE n.nspname = 'public' AND tc.relname = :table
      AND am.amname IN ('gist', 'spgist', 'brin')
""")


def check_database(engine: Engine) -> None:
    with engine.connect() as conn:
        pg = conn.execute(text("SHOW server_version")).scalar()
        postgis = conn.execute(text("SELECT postgis_lib_version()")).scalar()
        try:
            pgr = conn.execute(text("SELECT pgr_version()")).scalar()
        except Exception:  # noqa: BLE001 - pgRouting is only needed for the bonus
            pgr = "not installed"
    log(f"  PostgreSQL {pg} | PostGIS {postgis} | pgRouting {pgr}")


def upload_table(gdf_utm: gpd.GeoDataFrame, base: str, engine: Engine) -> None:
    for srid in TARGET_SRIDS:
        table = f"{base}_{srid}"
        g = gdf_utm.to_crs(srid)
        if g.geometry.name != "geom":
            g = g.rename_geometry("geom")
        t0 = time.perf_counter()
        g.to_postgis(table, engine, schema="public", if_exists="replace", index=False, chunksize=10000)
        with engine.begin() as conn:
            for idx_name in conn.execute(DROP_SPATIAL_INDEX_SQL, {"table": table}).scalars().all():
                conn.execute(text(f'DROP INDEX IF EXISTS public."{idx_name}"'))
            conn.execute(text(f'ALTER TABLE public."{table}" ADD PRIMARY KEY (id)'))
            conn.execute(text(f'ANALYZE public."{table}"'))
        log(f"  {table:<20} {len(g):>8} rows  ({time.perf_counter() - t0:.1f} s)")


def verify_tables(engine: Engine) -> None:
    log("\nVerification (rows, SRID, geometry type, spatial indexes):")
    log(f"  {'table':<20} {'rows':>8} {'srid':>6}  {'geom_type':<16} gist_idx")
    with engine.connect() as conn:
        for base in TABLE_BASES:
            for srid in TARGET_SRIDS:
                table = f"{base}_{srid}"
                rows = conn.execute(text(f'SELECT COUNT(*) FROM public."{table}"')).scalar()
                srid_db, gtype = conn.execute(text(
                    f'SELECT ST_SRID(geom), GeometryType(geom) FROM public."{table}" LIMIT 1'
                )).one()
                n_idx = len(conn.execute(DROP_SPATIAL_INDEX_SQL, {"table": table}).all())
                log(f"  {table:<20} {rows:>8} {srid_db:>6}  {gtype:<16} {n_idx}")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Phase 1 ETL: OSM -> PostGIS (EPSG:4326 + EPSG:32643).")
    p.add_argument("--reuse-local", action="store_true",
                   help="Reuse kochi_buildings.gpkg / kochi_walk_2km.gpkg if they exist "
                        "instead of downloading again.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    configure_osmnx()
    t_start = time.perf_counter()

    cfg = db_settings()
    log(f"osmnx {ox.__version__} | geopandas {gpd.__version__}")
    log(f"PostGIS target: {cfg['username']}@{cfg['host']}:{cfg['port']}/{cfg['database']}")
    engine = make_engine(cfg)
    check_database(engine)

    log("\n[1/4] Loading Step-0 inputs")
    study = load_study_area()
    study_utm = study.geometry.iloc[0]
    healthcare = load_healthcare(study_utm)

    log("\n[2/4] Buildings")
    buildings = download_buildings(study_utm, args.reuse_local)

    log("\n[3/4] Walking network")
    walk_nodes, walk_edges = download_walk_network(study_utm, args.reuse_local)

    log("\n[4/4] Uploading to PostGIS (no GiST indexes)")
    for base, gdf in (
        ("study_area", study),
        ("healthcare", healthcare),
        ("buildings", buildings),
        ("walk_nodes", walk_nodes),
        ("walk_edges", walk_edges),
    ):
        upload_table(gdf, base, engine)

    verify_tables(engine)

    log("\nHealthcare by type:")
    for typ, n in healthcare["type"].value_counts().sort_index().items():
        log(f"  {typ:<24} {n}")
    log(f"\nPhase 1 ETL finished in {time.perf_counter() - t_start:.1f} s")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except NetworkUnavailable as err:
        print(f"\nNETWORK ERROR: {err}\nCheck your internet connection and re-run "
              "(add --reuse-local to skip parts already downloaded).", file=sys.stderr)
        sys.exit(3)
