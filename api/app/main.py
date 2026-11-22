"""
Kochi Healthcare WebGIS - API
=============================

Upgrade of the earlier nearby-services FastAPI app. It still geocodes with
Nominatim, but "nearby" now means *walking distance on the street network*
(pgRouting) instead of straight-line distance.

Endpoints
    GET  /api/health                 database, PostGIS, pgRouting and GeoServer status
    GET  /api/config                 public service URLs + study-area extent for the client
    GET  /api/geocode?q=...          Nominatim search biased to the study area
    GET  /api/nearest?lat&lon        k nearest facilities by network walking distance + route
    GET  /api/isochrone?lat&lon      walking service area from any point (pgr_drivingDistance)
    GET  /api/stats/coverage         Phase-2 coverage table (v_coverage_summary)
    GET  /api/facilities/{id}        one facility with its accessibility attributes
    GET|POST /geoserver/{ows path}   same-origin proxy for GeoServer OWS (WMS/WFS/WCS)
    GET  /                           the Leaflet client (client/)

Inside Docker the API reaches PostGIS at host "postgis" and GeoServer at
"geoserver:8080". The PUBLIC_* URLs are only handed to the browser.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from contextlib import asynccontextmanager
from enum import Enum
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def env(key: str, default: str) -> str:
    value = os.environ.get(key, "").strip()
    return value or default


DB_HOST = env("POSTGRES_HOST", "localhost")
DB_PORT = int(env("POSTGRES_PORT", "5433"))
DB_NAME = env("POSTGRES_DB", "kochi_health")
DB_USER = env("POSTGRES_USER", "webgis")
DB_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "")

GEOSERVER_INTERNAL_URL = env("GEOSERVER_INTERNAL_URL", "http://localhost:8088/geoserver").rstrip("/")
PUBLIC_GEOSERVER_URL = env("PUBLIC_GEOSERVER_URL", "http://localhost:8088/geoserver").rstrip("/")
PUBLIC_TILESERV_URL = env("PUBLIC_TILESERV_URL", "http://localhost:7800").rstrip("/")
PUBLIC_FEATURESERV_URL = env("PUBLIC_FEATURESERV_URL", "http://localhost:9000").rstrip("/")
GEOSERVER_WORKSPACE = env("GEOSERVER_WORKSPACE", "kochi")

NOMINATIM_URL = env("NOMINATIM_URL", "https://nominatim.openstreetmap.org").rstrip("/")
NOMINATIM_USER_AGENT = env("NOMINATIM_USER_AGENT", "kochi-healthcare-webgis/1.0 (student project)")

CLIENT_DIR = Path(env("CLIENT_DIR", str(Path(__file__).resolve().parents[2] / "client")))

UTM = 32643
MAX_SNAP_M = 500.0          # a clicked point further than this from the walk network is rejected
SERVICE_BUFFER_M = 50.0     # same buffer as Phase 2 service areas
OWS_PATH = re.compile(r"^(?:[A-Za-z0-9_\-]+/)?(?:ows|wms|wfs|wcs)$")


class FacilitySet(str, Enum):
    all = "all"
    hospital = "hospital"
    clinical = "clinical"


SET_FILTER = {
    FacilitySet.all: sql.SQL("TRUE"),
    FacilitySet.hospital: sql.SQL("f.is_hospital"),
    FacilitySet.clinical: sql.SQL("f.is_clinical"),
}


def conninfo() -> str:
    if not DB_PASSWORD:
        raise RuntimeError("POSTGRES_PASSWORD is not set")
    return (f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} user={DB_USER} "
            f"password={DB_PASSWORD} connect_timeout=10 application_name=kochi_api")


pool: ConnectionPool | None = None          # opened in lifespan()
http_client: httpx.AsyncClient | None = None
_geocode_cache: dict[str, Any] = {}
_geocode_lock = asyncio.Lock()
_last_geocode_call = 0.0


@asynccontextmanager
async def lifespan(_: FastAPI):
    global pool, http_client
    pool = ConnectionPool(conninfo=conninfo(), min_size=1, max_size=8, open=True,
                          kwargs={"row_factory": dict_row, "autocommit": True})
    http_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=False)
    try:
        yield
    finally:
        await http_client.aclose()
        pool.close()


app = FastAPI(title="Kochi Healthcare WebGIS API", version="1.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Database helpers
# --------------------------------------------------------------------------- #
def fetch_all(query, params: dict | None = None) -> list[dict]:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(query, params or {})
        return cur.fetchall()


def fetch_one(query, params: dict | None = None) -> dict | None:
    rows = fetch_all(query, params)
    return rows[0] if rows else None


def metres_per_minute(speed_kmh: float) -> float:
    return speed_kmh * 1000.0 / 60.0


def snap_point(lat: float, lon: float) -> dict:
    """Nearest walk node to a WGS84 point (KNN on the GiST index)."""
    row = fetch_one(
        """
        WITH o AS (SELECT ST_Transform(ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326), 32643) AS g)
        SELECT n.node_id,
               ST_Distance(n.geom, o.g) AS snap_m,
               ST_X(o.g) AS ox, ST_Y(o.g) AS oy
        FROM o, LATERAL (
            SELECT node_id, geom FROM walk_nodes_32643
            ORDER BY geom <-> o.g
            LIMIT 1
        ) n
        """,
        {"lat": lat, "lon": lon},
    )
    if row is None:
        raise HTTPException(503, "walk_nodes_32643 is empty - run phase1_etl.py")
    if row["snap_m"] > MAX_SNAP_M:
        raise HTTPException(
            422, f"Point is {row['snap_m']:.0f} m from the walk network (max {MAX_SNAP_M:.0f} m). "
                 "Pick a point inside the study area.")
    return row


def edges_sql(x: float, y: float, radius_m: float) -> str:
    """pgRouting edge query restricted to a square around the origin (numbers only)."""
    return (
        "SELECT id, source, target, length_m AS cost FROM walk_edges_32643 "
        f"WHERE geom && ST_Expand(ST_SetSRID(ST_MakePoint({x:.3f}, {y:.3f}), 32643), {radius_m:.0f})"
    )


# --------------------------------------------------------------------------- #
# Routes: status and configuration
# --------------------------------------------------------------------------- #
@app.get("/api/health")
async def health() -> dict:
    db = await asyncio.to_thread(
        fetch_one,
        "SELECT postgis_lib_version() AS postgis, pgr_version() AS pgrouting, "
        "(SELECT COUNT(*) FROM facility_access_4326) AS facilities, "
        "(SELECT COUNT(*) FROM walk_edges_32643) AS walk_edges",
    )
    geoserver = "unreachable"
    try:
        r = await http_client.get(f"{GEOSERVER_INTERNAL_URL}/ows",
                                  params={"service": "WMS", "version": "1.3.0", "request": "GetCapabilities"})
        geoserver = "ok" if r.status_code == 200 else f"HTTP {r.status_code}"
    except httpx.HTTPError as exc:
        geoserver = f"unreachable ({type(exc).__name__})"
    return {"status": "ok", "database": db, "geoserver": geoserver}


@app.get("/api/config")
def config() -> dict:
    ext = fetch_one(
        "SELECT ST_XMin(e) AS minx, ST_YMin(e) AS miny, ST_XMax(e) AS maxx, ST_YMax(e) AS maxy, "
        "ST_Y(ST_Centroid(g)) AS clat, ST_X(ST_Centroid(g)) AS clon "
        "FROM (SELECT ST_Extent(geom)::geometry AS e, ST_Union(geom) AS g FROM study_area_4326) s"
    )
    return {
        "center": {"lat": ext["clat"], "lon": ext["clon"]},
        "bbox": [ext["minx"], ext["miny"], ext["maxx"], ext["maxy"]],
        "geoserver_url": PUBLIC_GEOSERVER_URL,
        "geoserver_wms_url": f"{PUBLIC_GEOSERVER_URL}/{GEOSERVER_WORKSPACE}/wms",
        "geoserver_proxy_wfs_url": f"/geoserver/{GEOSERVER_WORKSPACE}/wfs",
        "workspace": GEOSERVER_WORKSPACE,
        "tileserv_url": PUBLIC_TILESERV_URL,
        "featureserv_url": PUBLIC_FEATURESERV_URL,
        "speeds_kmh": [4.8, 3.6],
        "minutes": [5, 10, 15],
        "facility_sets": [s.value for s in FacilitySet],
    }


@app.get("/api/stats/coverage")
def coverage(facility_set: FacilitySet | None = None) -> list[dict]:
    if facility_set is None:
        return fetch_all("SELECT * FROM v_coverage_summary")
    return fetch_all("SELECT * FROM v_coverage_summary WHERE facility_set = %(s)s",
                     {"s": facility_set.value})


@app.get("/api/facilities/{facility_id}")
def facility(facility_id: int) -> dict:
    row = fetch_one(
        "SELECT f.*, ST_AsGeoJSON(f.geom)::json AS geometry_json "
        "FROM facility_access_4326 f WHERE f.id = %(id)s",
        {"id": facility_id},
    )
    if row is None:
        raise HTTPException(404, "Facility not found")
    row.pop("geom", None)
    geometry = row.pop("geometry_json")
    return {"type": "Feature", "id": facility_id, "geometry": geometry, "properties": row}


# --------------------------------------------------------------------------- #
# Routes: geocoding (the original nearby-services feature)
# --------------------------------------------------------------------------- #
@app.get("/api/geocode")
async def geocode(q: str = Query(..., min_length=3, max_length=200)) -> list[dict]:
    global _last_geocode_call
    key = q.strip().lower()
    if key in _geocode_cache:
        return _geocode_cache[key]

    ext = await asyncio.to_thread(config)
    minx, miny, maxx, maxy = ext["bbox"]
    pad = 0.05
    params = {
        "q": q, "format": "jsonv2", "limit": 8, "countrycodes": "in", "addressdetails": 0,
        "viewbox": f"{minx - pad},{maxy + pad},{maxx + pad},{miny - pad}", "bounded": 0,
    }
    async with _geocode_lock:              # Nominatim policy: max 1 request per second
        wait = 1.0 - (time.monotonic() - _last_geocode_call)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            r = await http_client.get(f"{NOMINATIM_URL}/search", params=params,
                                      headers={"User-Agent": NOMINATIM_USER_AGENT})
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"Nominatim unreachable: {exc}") from exc
        finally:
            _last_geocode_call = time.monotonic()
    if r.status_code != 200:
        raise HTTPException(502, f"Nominatim returned HTTP {r.status_code}")
    results = [
        {"name": item.get("display_name"), "lat": float(item["lat"]), "lon": float(item["lon"]),
         "category": item.get("category"), "type": item.get("type")}
        for item in r.json()
    ]
    _geocode_cache[key] = results
    return results


# --------------------------------------------------------------------------- #
# Routes: network analysis (pgRouting)
# --------------------------------------------------------------------------- #
@app.get("/api/nearest")
def nearest(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    facility_set: FacilitySet = FacilitySet.all,
    k: int = Query(5, ge=1, le=20),
    speed_kmh: float = Query(4.8, ge=1.0, le=8.0),
) -> dict:
    t0 = time.perf_counter()
    origin = snap_point(lat, lon)
    start = int(origin["node_id"])
    access_col = sql.Identifier(f"{facility_set.value}_fac_id")

    # Candidates: the 3k nearest by straight line, plus the true network-nearest
    # facility already known from Phase 2 (node_access), so it is never missed.
    candidates = fetch_all(
        sql.SQL("""
        WITH o AS (SELECT ST_SetSRID(ST_MakePoint(%(ox)s, %(oy)s), 32643) AS g),
        cand_ids AS (
            (SELECT f.id FROM facility_access_32643 f, o
             WHERE {set_filter}
             ORDER BY f.geom <-> o.g
             LIMIT %(n)s)
            UNION
            (SELECT n.{access_col} FROM node_access_32643 n
             WHERE n.node_id = %(start)s AND n.{access_col} IS NOT NULL)
        )
        SELECT f.id, f.name, f.type, f.node_id, f.snap_m,
               ST_Distance(f.geom, o.g) AS straight_m,
               ST_Y(ST_Transform(f.geom, 4326)) AS lat,
               ST_X(ST_Transform(f.geom, 4326)) AS lon
        FROM facility_access_32643 f
        JOIN cand_ids c ON c.id = f.id, o
        """).format(set_filter=SET_FILTER[facility_set], access_col=access_col),
        {"ox": origin["ox"], "oy": origin["oy"], "n": k * 3, "start": start},
    )
    if not candidates:
        raise HTTPException(404, f"No facilities in set '{facility_set.value}'")

    radius = min(8000.0, max(c["straight_m"] for c in candidates) * 2.0 + 1500.0)
    esql = edges_sql(origin["ox"], origin["oy"], radius)
    targets = sorted({int(c["node_id"]) for c in candidates if int(c["node_id"]) != start})
    costs: dict[int, float] = {start: 0.0}
    if targets:
        for row in fetch_all(
            "SELECT end_vid, agg_cost FROM pgr_dijkstraCost(%(esql)s, %(start)s::bigint, "
            "%(targets)s::bigint[], directed := false)",
            {"esql": esql, "start": start, "targets": targets},
        ):
            costs[int(row["end_vid"])] = float(row["agg_cost"])

    mpm = metres_per_minute(speed_kmh)
    results = []
    for c in candidates:
        net = costs.get(int(c["node_id"]))
        if net is None:
            continue
        total = origin["snap_m"] + net + c["snap_m"]
        results.append({**c, "network_m": round(total, 1), "walk_min": round(total / mpm, 1),
                        "straight_m": round(c["straight_m"], 1),
                        "detour_ratio": round(total / c["straight_m"], 2) if c["straight_m"] > 0 else None})
    results.sort(key=lambda r: r["network_m"])
    results = results[:k]
    if not results:
        raise HTTPException(404, "No facility reachable on the walk network from this point")

    best = results[0]
    route = None
    if int(best["node_id"]) != start:
        r = fetch_one(
            """
            SELECT ST_AsGeoJSON(ST_Transform(ST_Multi(ST_Union(e.geom)), 4326))::json AS geometry,
                   SUM(d.cost) AS length_m
            FROM pgr_dijkstra(%(esql)s, %(start)s::bigint, %(end)s::bigint, directed := false) d
            JOIN walk_edges_32643 e ON e.id = d.edge
            """,
            {"esql": esql, "start": start, "end": int(best["node_id"])},
        )
        if r and r["geometry"]:
            route = {"type": "Feature", "geometry": r["geometry"],
                     "properties": {"to_id": best["id"], "to_name": best["name"],
                                    "network_length_m": round(float(r["length_m"]), 1)}}

    features = [{
        "type": "Feature",
        "id": res["id"],
        "geometry": {"type": "Point", "coordinates": [res["lon"], res["lat"]]},
        "properties": {k_: v for k_, v in res.items() if k_ not in ("lat", "lon")},
    } for res in results]
    return {
        "origin": {"lat": lat, "lon": lon, "snap_node": start, "snap_m": round(origin["snap_m"], 1)},
        "facility_set": facility_set.value,
        "speed_kmh": speed_kmh,
        "results": {"type": "FeatureCollection", "features": features},
        "route": route,
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
    }


@app.get("/api/isochrone")
def isochrone(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    minutes: float = Query(15, gt=0, le=30),
    speed_kmh: float = Query(4.8, ge=1.0, le=8.0),
) -> dict:
    t0 = time.perf_counter()
    origin = snap_point(lat, lon)
    budget = metres_per_minute(speed_kmh) * minutes - origin["snap_m"]
    if budget <= 0:
        raise HTTPException(422, "Time budget is used up walking to the network")
    esql = edges_sql(origin["ox"], origin["oy"], budget + 200)

    row = fetch_one(
        """
        WITH dd AS (
            SELECT node, agg_cost
            FROM pgr_drivingDistance(%(esql)s, %(start)s::bigint, %(budget)s, directed := false)
        ),
        reached AS (
            SELECT e.geom
            FROM walk_edges_32643 e
            JOIN dd a ON a.node = e.source
            JOIN dd b ON b.node = e.target
            WHERE e.geom && ST_Expand(ST_SetSRID(ST_MakePoint(%(ox)s, %(oy)s), 32643), %(budget)s + 200)
              AND LEAST(a.agg_cost, b.agg_cost) + e.length_m <= %(budget)s + e.length_m * 0.5
        ),
        poly AS (
            SELECT ST_Buffer(ST_Union(geom), %(buf)s, 'quad_segs=4') AS g FROM reached
        )
        SELECT
            ST_AsGeoJSON(ST_Transform(p.g, 4326))::json AS geometry,
            ST_Area(p.g) / 1e6 AS area_km2,
            (SELECT COUNT(*) FROM building_access_32643 b
              WHERE ST_Intersects(p.g, ST_PointOnSurface(b.geom)))  AS buildings,
            (SELECT COUNT(*) FROM facility_access_32643 f
              WHERE ST_Intersects(p.g, f.geom))                     AS facilities,
            (SELECT COUNT(*) FROM facility_access_32643 f
              WHERE f.is_hospital AND ST_Intersects(p.g, f.geom))   AS hospitals,
            (SELECT COUNT(*) FROM dd)                               AS reached_nodes
        FROM poly p
        """,
        {"esql": esql, "start": int(origin["node_id"]), "budget": budget,
         "ox": origin["ox"], "oy": origin["oy"], "buf": SERVICE_BUFFER_M},
    )
    if row is None or row["geometry"] is None:
        raise HTTPException(404, "No reachable street segments within this time")
    return {
        "type": "Feature",
        "geometry": row["geometry"],
        "properties": {
            "minutes": minutes, "speed_kmh": speed_kmh, "distance_budget_m": round(budget, 1),
            "snap_m": round(origin["snap_m"], 1), "area_km2": round(row["area_km2"], 4),
            "buildings": row["buildings"], "facilities": row["facilities"],
            "hospitals": row["hospitals"], "reached_nodes": row["reached_nodes"],
            "method": "pgr_drivingDistance; street segments whose two end nodes and midpoint "
                      f"are reachable, buffered {SERVICE_BUFFER_M:.0f} m",
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        },
    }


# --------------------------------------------------------------------------- #
# GeoServer OWS proxy (same origin for WFS fetch() calls, no CORS needed)
# --------------------------------------------------------------------------- #
@app.api_route("/geoserver/{path:path}", methods=["GET", "POST"])
async def geoserver_proxy(path: str, request: Request) -> Response:
    if not OWS_PATH.match(path):
        raise HTTPException(404, "Only OWS endpoints (ows, wms, wfs, wcs) are proxied")
    headers = {}
    if "content-type" in request.headers:
        headers["Content-Type"] = request.headers["content-type"]
    try:
        upstream = await http_client.request(
            request.method, f"{GEOSERVER_INTERNAL_URL}/{path}",
            params=request.query_params, content=await request.body(), headers=headers)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"GeoServer unreachable: {exc}") from exc
    passthrough = {k: v for k, v in upstream.headers.items()
                   if k.lower() in ("content-type", "content-disposition", "cache-control")}
    return Response(content=upstream.content, status_code=upstream.status_code, headers=passthrough)


@app.exception_handler(RuntimeError)
async def runtime_error_handler(_: Request, exc: RuntimeError) -> JSONResponse:
    return JSONResponse(status_code=500, content={"detail": str(exc)})


# --------------------------------------------------------------------------- #
# Static client (mounted last so /api and /geoserver win)
# --------------------------------------------------------------------------- #
if CLIENT_DIR.exists():
    app.mount("/", StaticFiles(directory=str(CLIENT_DIR), html=True), name="client")
