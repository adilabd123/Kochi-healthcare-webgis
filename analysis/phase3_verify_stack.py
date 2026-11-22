#!/usr/bin/env python3
"""
Phase 3 - End-to-end stack verification
=======================================

Checks every service from the Windows host, the same way the browser sees it,
and writes analysis/phase3_stack_check.json with the real responses.

    PostGIS        localhost:<POSTGIS_PORT>  views, function, indexes, pgRouting
    pg_tileserv    localhost:<PG_TILESERV_PORT>   function layer + one real tile
    pg_featureserv localhost:<PG_FEATURESERV_PORT> collections + item count
    GeoServer      localhost:<GEOSERVER_PORT>  WMS capabilities, GetMap, WFS count
    API            localhost:<API_PORT>     health, config, coverage, nearest,
                                            isochrone, GeoServer proxy, client page

Exit code 0 when every check passes, 1 otherwise.

Usage
    python analysis/phase3_verify_stack.py
    python analysis/phase3_verify_stack.py --lat 10.0247 --lon 76.3084
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from sqlalchemy import text

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from phase1_etl import ENV_FILE, db_settings, load_env_file, make_engine  # noqa: E402

OUT_JSON = SCRIPT_DIR / "phase3_stack_check.json"
WS = "kochi"


def cfg_value(key: str, default: str) -> str:
    return os.environ.get(key) or load_env_file(ENV_FILE).get(key) or default


def tile_xy(lat: float, lon: float, z: int) -> tuple[int, int]:
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    r = math.radians(lat)
    y = int((1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi) / 2.0 * n)
    return x, y


class Checker:
    def __init__(self):
        self.results: list[dict] = []
        self.http = requests.Session()
        self.http.trust_env = False          # never send localhost calls through a proxy

    def check(self, name: str, fn):
        t0 = time.perf_counter()
        try:
            detail = fn()
            ok = True
        except Exception as exc:  # noqa: BLE001 - report every failure, keep going
            detail = f"{type(exc).__name__}: {exc}"
            ok = False
        ms = round((time.perf_counter() - t0) * 1000, 1)
        self.results.append({"check": name, "ok": ok, "ms": ms, "detail": detail})
        mark = "PASS" if ok else "FAIL"
        short = json.dumps(detail, ensure_ascii=False, default=str)
        print(f"  [{mark}] {name:<44} {ms:>8} ms  {short[:110]}")

    def get(self, url: str, **kw) -> requests.Response:
        r = self.http.get(url, timeout=60, **kw)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code} for {r.url}: {r.text[:200]}")
        return r


def main() -> int:
    p = argparse.ArgumentParser(description="Verify the Kochi Healthcare WebGIS stack end to end.")
    p.add_argument("--lat", type=float, default=10.008, help="Test origin latitude (default study centre)")
    p.add_argument("--lon", type=float, default=76.302, help="Test origin longitude")
    args = p.parse_args()

    tileserv = f"http://localhost:{cfg_value('PG_TILESERV_PORT', '7800')}"
    featureserv = f"http://localhost:{cfg_value('PG_FEATURESERV_PORT', '9000')}"
    geoserver = f"http://localhost:{cfg_value('GEOSERVER_PORT', '8088')}/geoserver"
    api = f"http://localhost:{cfg_value('API_PORT', '8000')}"
    c = Checker()
    engine = make_engine(db_settings())

    print("PostGIS")

    def db_objects():
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT postgis_lib_version() AS postgis, pgr_version() AS pgrouting,
                  to_regclass('public.v_hospital_access_gaps_4326') IS NOT NULL AS gap_view,
                  to_regclass('public.v_coverage_summary') IS NOT NULL AS coverage_view,
                  to_regprocedure('public.building_walk_time(integer,integer,integer,text,double precision)')
                      IS NOT NULL AS tile_function,
                  (SELECT COUNT(*) FROM pg_indexes WHERE schemaname='public'
                     AND indexdef ILIKE '%USING gist%') AS gist_indexes,
                  (SELECT COUNT(*) FROM v_hospital_access_gaps_4326) AS elderly_gap_buildings,
                  (SELECT COUNT(*) FROM v_coverage_summary) AS coverage_rows
            """)).mappings().one()
        d = dict(row)
        missing = [k for k in ("gap_view", "coverage_view", "tile_function") if not d[k]]
        if missing:
            raise RuntimeError(f"missing {missing} - run sql/phase3_views.sql")
        return d
    c.check("postgis: views, function, indexes", db_objects)

    print("\npg_tileserv")
    x, y = tile_xy(args.lat, args.lon, 15)

    def ts_index():
        j = c.get(f"{tileserv}/index.json").json()
        if isinstance(j, dict):
            names = list(j.keys())
        else:
            names = [item.get("id") or item.get("name") for item in j if isinstance(item, dict)]
        if "public.building_walk_time" not in names:
            raise RuntimeError(f"function layer not listed; got {names[:10]}")
        return {"layers": len(names), "function_layer": "public.building_walk_time"}
    c.check("pg_tileserv: index lists function layer", ts_index)

    def ts_tile(fs: str, sp: float):
        def run():
            r = c.get(f"{tileserv}/public.building_walk_time/15/{x}/{y}.pbf",
                      params={"facility_set": fs, "speed_kmh": sp})
            if len(r.content) == 0:
                raise RuntimeError("empty tile")
            return {"z/x/y": f"15/{x}/{y}", "bytes": len(r.content),
                    "content_type": r.headers.get("Content-Type")}
        return run
    c.check("pg_tileserv: tile all @4.8", ts_tile("all", 4.8))
    c.check("pg_tileserv: tile hospital @3.6", ts_tile("hospital", 3.6))

    print("\npg_featureserv")

    def fs_collections():
        j = c.get(f"{featureserv}/collections.json").json()
        ids = [col.get("id") for col in j.get("collections", [])]
        need = ["public.facility_access_4326", "public.service_areas_4326",
                "public.building_access_4326", "public.node_access_4326"]
        miss = [n for n in need if n not in ids]
        if miss:
            raise RuntimeError(f"missing collections {miss}")
        return {"collections": len(ids)}
    c.check("pg_featureserv: collections", fs_collections)

    def fs_items():
        j = c.get(f"{featureserv}/collections/public.facility_access_4326/items.json",
                  params={"limit": 1000}).json()
        n = len(j.get("features", []))
        hosp = sum(1 for f in j["features"] if f["properties"].get("type") == "hospital")
        return {"features": n, "hospital": hosp}
    c.check("pg_featureserv: facility items", fs_items)

    print("\nGeoServer")

    def gs_caps():
        r = c.get(f"{geoserver}/{WS}/wms", params={"service": "WMS", "version": "1.3.0",
                                                   "request": "GetCapabilities"})
        layers = ["study_area", "healthcare", "service_areas", "building_access", "walk_edges",
                  "hospital_access_gaps"]
        found = [l for l in layers if f"{l}</Name>" in r.text]
        if len(found) != len(layers):
            raise RuntimeError(f"only {found} advertised - run phase3_publish_geoserver.py")
        return {"layers": found}
    c.check("geoserver: WMS 1.3.0 capabilities", gs_caps)

    def gs_map():
        d = 0.01
        r = c.get(f"{geoserver}/{WS}/wms", params={
            "service": "WMS", "version": "1.1.1", "request": "GetMap",
            "layers": f"{WS}:service_areas", "styles": "", "srs": "EPSG:4326",
            "bbox": f"{args.lon - d},{args.lat - d},{args.lon + d},{args.lat + d}",
            "width": 256, "height": 256, "format": "image/png", "transparent": "true",
            "CQL_FILTER": "facility_set='all' AND speed_kmh=4.8"})
        if not r.headers.get("Content-Type", "").startswith("image/png"):
            raise RuntimeError(f"not an image: {r.text[:200]}")
        return {"bytes": len(r.content)}
    c.check("geoserver: WMS GetMap service_areas + CQL", gs_map)

    def gs_wfs():
        j = c.get(f"{geoserver}/{WS}/wfs", params={
            "service": "WFS", "version": "2.0.0", "request": "GetFeature",
            "typeNames": f"{WS}:healthcare", "outputFormat": "application/json",
            "CQL_FILTER": "type='hospital'"}).json()
        return {"hospital_features": len(j.get("features", []))}
    c.check("geoserver: WFS 2.0 hospitals (CQL)", gs_wfs)

    print("\nAPI")
    c.check("api: health", lambda: c.get(f"{api}/api/health").json())
    c.check("api: config", lambda: {k: v for k, v in c.get(f"{api}/api/config").json().items()
                                    if k in ("geoserver_wms_url", "tileserv_url", "featureserv_url")})

    def api_cov():
        rows = c.get(f"{api}/api/stats/coverage").json()
        pick = next(r for r in rows if r["facility_set"] == "hospital"
                    and float(r["speed_kmh"]) == 3.6 and int(r["minutes"]) == 15)
        return {"rows": len(rows), "hospital_3.6_15min_pct_buildings": pick["pct_buildings"]}
    c.check("api: coverage table", api_cov)

    for fs in ("all", "hospital"):
        def api_nearest(fs=fs):
            j = c.get(f"{api}/api/nearest", params={"lat": args.lat, "lon": args.lon,
                                                    "facility_set": fs, "k": 3, "speed_kmh": 4.8}).json()
            top = [{"name": f["properties"]["name"], "type": f["properties"]["type"],
                    "network_m": f["properties"]["network_m"], "walk_min": f["properties"]["walk_min"],
                    "straight_m": f["properties"]["straight_m"]} for f in j["results"]["features"]]
            return {"snap_m": j["origin"]["snap_m"], "elapsed_ms": j["elapsed_ms"], "top3": top,
                    "route": bool(j.get("route"))}
        c.check(f"api: nearest {fs} (pgRouting)", api_nearest)

    for minutes, sp in ((15, 4.8), (15, 3.6)):
        def api_iso(minutes=minutes, sp=sp):
            pr = c.get(f"{api}/api/isochrone", params={"lat": args.lat, "lon": args.lon,
                                                       "minutes": minutes, "speed_kmh": sp}).json()["properties"]
            return {k: pr[k] for k in ("distance_budget_m", "area_km2", "buildings", "facilities",
                                       "hospitals", "elapsed_ms")}
        c.check(f"api: isochrone {minutes} min @{sp}", api_iso)

    def api_proxy():
        j = c.get(f"{api}/geoserver/{WS}/wfs", params={
            "service": "WFS", "version": "2.0.0", "request": "GetFeature",
            "typeNames": f"{WS}:healthcare", "outputFormat": "application/json", "count": 5}).json()
        return {"features": len(j.get("features", []))}
    c.check("api: GeoServer WFS proxy", api_proxy)

    def api_client():
        r = c.get(f"{api}/")
        if "Kochi Healthcare WebGIS" not in r.text:
            raise RuntimeError("client page not served")
        return {"bytes": len(r.content)}
    c.check("api: client page", api_client)

    passed = sum(r["ok"] for r in c.results)
    summary = {"generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "origin": {"lat": args.lat, "lon": args.lon},
               "passed": passed, "total": len(c.results), "checks": c.results}
    OUT_JSON.write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\n{passed}/{len(c.results)} checks passed. Saved {OUT_JSON.name}")
    return 0 if passed == len(c.results) else 1


if __name__ == "__main__":
    sys.exit(main())
