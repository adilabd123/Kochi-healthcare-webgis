#!/usr/bin/env python3
"""
Q12 - Export the Kochi layers from PostGIS into one static PMTiles archive
==========================================================================

Plain-language idea
    A PMTiles file is a "zip of map tiles" with an index at the front. A web
    browser can ask GitHub Pages for just the bytes of one tile (an HTTP Range
    request), so the map works with no tile server, database or API at all.

How it works (pure Python + PostGIS, no tippecanoe needed on Windows)
    1. Connect to the Docker Compose PostGIS (default 127.0.0.1:15432, user
       postgres, database kochi_health; password from .env / environment).
    2. For every XYZ tile z10-z16 covering the study area (+ ~2 km for the
       service areas), PostGIS builds one Mapbox Vector Tile containing all
       layers with ST_AsMVTGeom / ST_AsMVT.
    3. Tiles are gzip-compressed and written in tile-id order with the
       `pmtiles` Python writer -> docs/tiles/kochi.pmtiles.
    4. docs/tiles/kochi_static.json stores the extent, the Phase 2 coverage
       table and per-layer counts for the client's static (offline) mode.

Layers in the archive (source-layer names used by client/app.js)
    study_area, service_areas, hospital_gaps, h3_res8, h3_res9, buildings, facilities

Usage (PowerShell, from the repository root)
    python pmtiles\\export_pmtiles.py
    python pmtiles\\export_pmtiles.py --max-zoom 17
    python pmtiles\\export_pmtiles.py --host 127.0.0.1 --port 15432 --user postgres --db kochi_health
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
from pmtiles.tile import Compression, TileType, zxy_to_tileid
from pmtiles.writer import Writer

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
OUT_DIR = ROOT / "docs" / "tiles"
PMTILES_OUT = OUT_DIR / "kochi.pmtiles"
STATIC_JSON = OUT_DIR / "kochi_static.json"

EXTENT = 4096
BUFFER = 64

# (source-layer, table/view, attribute columns, min zoom, max zoom)
LAYERS = [
    ("study_area", "study_area_4326", ["id"], 10, 16),
    ("service_areas", "service_areas_4326",
     ["id", "facility_set", "speed_kmh", "minutes", "distance_m", "pct_buildings"], 10, 16),
    ("hospital_gaps", "v_hospital_access_gaps_4326",
     ["id", "gap_class", "hospital_dist_m", "nearest_hospital"], 13, 16),
    ("h3_res8", "h3_res8_4326",
     ["h3_index", "res", "net_dist_m", "walk_min_4p8", "bld_count", "bld_area_m2", "served_15min"], 10, 16),
    ("h3_res9", "h3_res9_4326",
     ["h3_index", "res", "net_dist_m", "walk_min_4p8", "bld_count", "bld_area_m2", "served_15min"], 10, 16),
    ("buildings", "building_access_4326",
     ["id", "name", "building", "all_dist_m", "hospital_dist_m", "clinical_dist_m"], 13, 16),
    ("facilities", "facility_access_4326",
     ["id", "osm_type", "osm_id", "name", "type", "is_hospital", "is_clinical", "hospital_dist_m",
      "hospital_t4p8_min", "hospital_t3p6_min", "all_catch15_4p8", "snap_m"], 10, 16),
]


# --------------------------------------------------------------------------- #
def load_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if path.exists():
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                    v = v[1:-1]
                out[k.strip()] = v
    return out


def parse_args() -> argparse.Namespace:
    f = load_env(ENV_FILE)

    def d(key: str, fallback: str) -> str:
        return os.environ.get(key) or f.get(key) or fallback

    p = argparse.ArgumentParser(description="PostGIS -> static PMTiles archive for GitHub Pages")
    p.add_argument("--host", default=d("POSTGRES_HOST", "127.0.0.1"))
    p.add_argument("--port", default=d("POSTGIS_PORT", "15432"))
    p.add_argument("--user", default=d("POSTGRES_USER", "postgres"))
    p.add_argument("--db", default=d("POSTGRES_DB", "kochi_health"))
    p.add_argument("--password", default=d("POSTGRES_PASSWORD", ""))
    p.add_argument("--min-zoom", type=int, default=10)
    p.add_argument("--max-zoom", type=int, default=16)
    p.add_argument("--pad-deg", type=float, default=0.02,
                   help="Padding around the study area extent (degrees, ~2 km) for service areas")
    p.add_argument("--out", type=Path, default=PMTILES_OUT)
    return p.parse_args()


def lonlat_to_tile(lon: float, lat: float, z: int) -> tuple[int, int]:
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    r = math.radians(max(min(lat, 85.0511), -85.0511))
    y = int((1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi) / 2.0 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def existing_layers(cur) -> list[tuple]:
    keep = []
    for layer in LAYERS:
        name, table, cols, zmin, zmax = layer
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{table}",))
        if not cur.fetchone()[0]:
            print(f"  ! {table} not found - layer '{name}' skipped")
            continue
        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name=%s", (table,))
        have = {r[0] for r in cur.fetchall()}
        missing = [c for c in cols if c not in have]
        if missing:
            print(f"  ! {table}: columns {missing} missing - they are left out of '{name}'")
        cols = [c for c in cols if c in have]
        cur.execute(f'SELECT COUNT(*) FROM public."{table}"')
        n = cur.fetchone()[0]
        print(f"  {name:<14} <- {table:<30} {n:>6} features, z{zmin}-{zmax}, fields {cols}")
        keep.append((name, table, cols, zmin, zmax, n))
    return keep


def tile_sql(layers: list[tuple], z: int) -> str | None:
    parts = []
    for name, table, cols, zmin, zmax, _ in layers:
        if not (zmin <= z <= zmax):
            continue
        col_sql = ", ".join(f't."{c}"' for c in cols)
        parts.append(
            f"COALESCE((SELECT ST_AsMVT(q, '{name}', {EXTENT}, 'geom') FROM ("
            f"SELECT {col_sql}, ST_AsMVTGeom(ST_Transform(t.geom, 3857), b.e3857, {EXTENT}, {BUFFER}, true) AS geom "
            f'FROM public."{table}" t, b WHERE t.geom && b.e4326'
            f") q WHERE q.geom IS NOT NULL), ''::bytea)")
    if not parts:
        return None
    return ("WITH b AS (SELECT ST_TileEnvelope(%(z)s, %(x)s, %(y)s) AS e3857, "
            "ST_Transform(ST_TileEnvelope(%(z)s, %(x)s, %(y)s), 4326) AS e4326) SELECT "
            + " || ".join(parts))


def field_types(cur, table: str, cols: list[str]) -> dict[str, str]:
    cur.execute("SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name=%s", (table,))
    types = dict(cur.fetchall())
    out = {}
    for c in cols:
        t = types.get(c, "")
        out[c] = ("Boolean" if t == "boolean" else
                  "Number" if t in ("integer", "bigint", "smallint", "double precision", "real", "numeric")
                  else "String")
    return out


def main() -> int:
    a = parse_args()
    if not a.password:
        raise SystemExit("POSTGRES_PASSWORD not set (.env or environment, or --password).")
    print(f"PostGIS {a.user}@{a.host}:{a.port}/{a.db}")
    conn = psycopg2.connect(host=a.host, port=int(a.port), user=a.user, password=a.password,
                            dbname=a.db, connect_timeout=10)
    conn.set_session(readonly=True, autocommit=True)
    cur = conn.cursor()

    print("\n[1/4] Layers")
    layers = existing_layers(cur)
    if not layers:
        raise SystemExit("No layers found - run the Phase 1-3 scripts first.")

    cur.execute("SELECT ST_XMin(e), ST_YMin(e), ST_XMax(e), ST_YMax(e), "
                "ST_X(ST_Centroid(g)), ST_Y(ST_Centroid(g)) "
                "FROM (SELECT ST_Extent(geom)::geometry e, ST_Union(geom) g FROM study_area_4326) s")
    minx, miny, maxx, maxy, clon, clat = cur.fetchone()
    pminx, pminy, pmaxx, pmaxy = minx - a.pad_deg, miny - a.pad_deg, maxx + a.pad_deg, maxy + a.pad_deg

    print(f"\n[2/4] Tiles z{a.min_zoom}-{a.max_zoom} over {pminx:.4f},{pminy:.4f},{pmaxx:.4f},{pmaxy:.4f}")
    t0 = time.perf_counter()
    tiles: list[tuple[int, bytes]] = []
    per_zoom: dict[int, dict] = {}
    raw_total = 0
    for z in range(a.min_zoom, a.max_zoom + 1):
        sql = tile_sql(layers, z)
        x0, y0 = lonlat_to_tile(pminx, pmaxy, z)
        x1, y1 = lonlat_to_tile(pmaxx, pminy, z)
        n_req = n_tiles = zbytes = 0
        if sql:
            for x in range(x0, x1 + 1):
                for y in range(y0, y1 + 1):
                    n_req += 1
                    cur.execute(sql, {"z": z, "x": x, "y": y})
                    mvt = bytes(cur.fetchone()[0] or b"")
                    if not mvt:
                        continue
                    raw_total += len(mvt)
                    data = gzip.compress(mvt, compresslevel=9, mtime=0)
                    tiles.append((zxy_to_tileid(z, x, y), data))
                    n_tiles += 1
                    zbytes += len(data)
        per_zoom[z] = {"tiles_checked": n_req, "tiles_written": n_tiles, "gzip_bytes": zbytes}
        print(f"  z{z:<2} x {x0}-{x1}  y {y0}-{y1}: {n_tiles:>4}/{n_req:<4} non-empty tiles, {zbytes / 1024:8.1f} KB")
    print(f"  {len(tiles)} tiles, {raw_total / 1e6:.2f} MB raw MVT in {time.perf_counter() - t0:.1f} s")

    print("\n[3/4] Writing PMTiles archive")
    tiles.sort(key=lambda t: t[0])
    vector_layers = []
    for name, table, cols, zmin, zmax, n in layers:
        vector_layers.append({"id": name, "description": f"public.{table}",
                              "minzoom": max(zmin, a.min_zoom), "maxzoom": min(zmax, a.max_zoom),
                              "fields": field_types(cur, table, cols)})
    a.out.parent.mkdir(parents=True, exist_ok=True)
    with open(a.out, "wb") as fh:
        w = Writer(fh)
        for tile_id, data in tiles:
            w.write_tile(tile_id, data)
        w.finalize(
            {
                "tile_type": TileType.MVT,
                "tile_compression": Compression.GZIP,
                "min_lon_e7": int(pminx * 1e7), "min_lat_e7": int(pminy * 1e7),
                "max_lon_e7": int(pmaxx * 1e7), "max_lat_e7": int(pmaxy * 1e7),
                "center_zoom": 14,
                "center_lon_e7": int(clon * 1e7), "center_lat_e7": int(clat * 1e7),
            },
            {
                "name": "Kochi Healthcare WebGIS",
                "description": "Walking access to healthcare - Edappally, Palarivattom, Kaloor (Kochi)",
                "attribution": "© OpenStreetMap contributors",
                "type": "overlay",
                "format": "pbf",
                "vector_layers": vector_layers,
                "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
        )
    size = a.out.stat().st_size
    print(f"  {a.out.relative_to(ROOT)}: {size / 1e6:.2f} MB")

    print("\n[4/4] Static client config")
    cur.execute("SELECT to_regclass('public.v_coverage_summary') IS NOT NULL")
    coverage = []
    if cur.fetchone()[0]:
        cur.execute("SELECT facility_set, speed_kmh, minutes, pct_buildings FROM v_coverage_summary")
        coverage = [{"facility_set": r[0], "speed_kmh": float(r[1]), "minutes": int(r[2]),
                     "pct_buildings": float(r[3])} for r in cur.fetchall()]
    static = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pmtiles": a.out.name,
        "pmtiles_bytes": size,
        "tiles_written": len(tiles),
        "zoom": [a.min_zoom, a.max_zoom],
        "bbox": [minx, miny, maxx, maxy],
        "center": {"lat": clat, "lon": clon},
        "layers": {name: {"features": n, "minzoom": zmin, "maxzoom": zmax}
                   for name, _, _, zmin, zmax, n in layers},
        "per_zoom": per_zoom,
        "coverage": coverage,
    }
    STATIC_JSON.write_text(json.dumps(static, indent=2), encoding="utf-8")
    print(f"  {STATIC_JSON.relative_to(ROOT)} ({len(coverage)} coverage rows)")
    conn.close()

    # Read the archive back to prove it is valid.
    from pmtiles.reader import MmapSource, Reader
    with open(a.out, "rb") as fh:
        r = Reader(MmapSource(fh))
        h = r.header()
        md = r.metadata()
        z = min(max(14, a.min_zoom), a.max_zoom)
        cx, cy = lonlat_to_tile(clon, clat, z)
        probe = r.get(z, cx, cy)
    print(f"\nVerified: PMTiles v{h.get('version', 3)}, zoom {h['min_zoom']}-{h['max_zoom']}, "
          f"{h['addressed_tiles_count']} tiles, layers {[v['id'] for v in md['vector_layers']]}; "
          f"centre tile {z}/{cx}/{cy} = {len(probe) if probe else 0} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
