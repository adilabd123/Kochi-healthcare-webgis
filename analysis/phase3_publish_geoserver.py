#!/usr/bin/env python3
"""
Phase 3 - Publish the PostGIS layers in GeoServer and collect OGC evidence
==========================================================================

Analogy: GeoServer is a print shop. PostGIS holds the raw data; GeoServer
needs (1) a "workspace" (a folder), (2) a "datastore" (the connection to the
database), (3) one "feature type/layer" per table, and (4) a "style" (SLD)
saying how to draw each layer. This script sets all of that up through the
GeoServer REST API, then sends real WMS/WFS requests and saves what comes back.

GeoServer itself connects to the database through the Docker network, so the
datastore host is the service name "postgis" and port 5432. This script runs on
Windows and talks to GeoServer on http://localhost:<GEOSERVER_PORT>/geoserver.

Steps (all idempotent - safe to re-run)
    1. workspace  kochi
    2. datastore  kochi_postgis  (host=postgis, port=5432)
    3. styles     uploaded from sld/*.sld
    4. layers     study_area, healthcare, service_areas, building_access,
                  walk_edges, hospital_access_gaps (view)
    5. evidence   analysis/phase3_evidence/
                    wms111_epsg4326.png            WMS 1.1.1, BBOX lon,lat
                    wms130_epsg4326.png            WMS 1.3.0, BBOX lat,lon (correct)
                    wms130_epsg4326_wrong_axis.png WMS 1.3.0 with lon,lat (wrong)
                    wms130_crs84.png               WMS 1.3.0, CRS:84 lon,lat
                    service_areas_hospital_3p6.png CQL_FILTER demo
                  analysis/phase3_geoserver_checks.json (status codes, counts, sizes)

Usage
    python analysis/phase3_publish_geoserver.py
    python analysis/phase3_publish_geoserver.py --evidence-only
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from phase1_etl import load_env_file, ENV_FILE  # noqa: E402

PROJECT_ROOT = SCRIPT_DIR.parent
SLD_DIR = PROJECT_ROOT / "sld"
EVIDENCE_DIR = SCRIPT_DIR / "phase3_evidence"
CHECKS_JSON = SCRIPT_DIR / "phase3_geoserver_checks.json"

WORKSPACE = "kochi"
DATASTORE = "kochi_postgis"

# (layer name, PostGIS table/view, default style, title)
LAYERS = [
    ("study_area", "study_area_4326", "study_area",
     "Study area - Edappally, Palarivattom, Kaloor"),
    ("healthcare", "facility_access_4326", "healthcare_points",
     "Healthcare facilities with walking-access attributes"),
    ("service_areas", "service_areas_4326", "service_areas",
     "Walking service areas 5/10/15 min (all, hospital, clinical; 4.8 and 3.6 km/h)"),
    ("building_access", "building_access_4326", "building_access",
     "Buildings with network walking time to nearest healthcare"),
    ("walk_edges", "walk_edges_4326", "walk_edges",
     "OSMnx walk network (study area + 2 km)"),
    ("hospital_access_gaps", "v_hospital_access_gaps_4326", "hospital_access_gaps",
     "Buildings > 15 min from a hospital-tagged facility at 3.6 km/h"),
]


# --------------------------------------------------------------------------- #
# Settings and HTTP helpers
# --------------------------------------------------------------------------- #
def settings() -> dict:
    import os
    f = load_env_file(ENV_FILE)

    def get(k, d=None):
        return os.environ.get(k) or f.get(k) or d

    port = get("GEOSERVER_PORT", "8088")
    return {
        "base": get("GEOSERVER_URL", f"http://localhost:{port}/geoserver").rstrip("/"),
        "user": get("GEOSERVER_ADMIN_USER", "admin"),
        "password": get("GEOSERVER_ADMIN_PASSWORD"),
        "db": get("POSTGRES_DB", "kochi_health"),
        "db_user": get("POSTGRES_USER", "webgis"),
        "db_password": get("POSTGRES_PASSWORD"),
        # What GeoServer (inside Docker) uses to reach PostGIS:
        "db_host_internal": get("GEOSERVER_DB_HOST", "postgis"),
        "db_port_internal": get("GEOSERVER_DB_PORT", "5432"),
    }


class GeoServer:
    def __init__(self, cfg: dict):
        if not cfg["password"]:
            raise SystemExit(f"GEOSERVER_ADMIN_PASSWORD missing in {ENV_FILE}")
        self.base = cfg["base"]
        self.rest = f"{self.base}/rest"
        self.s = requests.Session()
        self.s.auth = (cfg["user"], cfg["password"])
        self.s.headers.update({"Accept": "application/json"})

    def req(self, method: str, path: str, ok=(200, 201), **kw) -> requests.Response:
        url = path if path.startswith("http") else f"{self.rest}{path}"
        r = self.s.request(method, url, timeout=120, **kw)
        if r.status_code not in ok:
            raise RuntimeError(f"{method} {url} -> HTTP {r.status_code}: {r.text[:500]}")
        return r

    def exists(self, path: str) -> bool:
        r = self.s.get(f"{self.rest}{path}", timeout=60)
        if r.status_code == 200:
            return True
        if r.status_code == 404:
            return False
        raise RuntimeError(f"GET {path} -> HTTP {r.status_code}: {r.text[:300]}")


def wait_for_geoserver(gs: GeoServer, timeout_s: int = 300) -> str:
    t_end = time.time() + timeout_s
    last = ""
    while time.time() < t_end:
        try:
            r = gs.s.get(f"{gs.rest}/about/version.json", timeout=10)
            if r.status_code == 200:
                for res in r.json()["about"]["resource"]:
                    if res.get("@name") == "GeoServer":
                        return str(res.get("Version"))
                return "unknown"
            last = f"HTTP {r.status_code}"
            if r.status_code == 401:
                raise SystemExit("GeoServer rejected the admin credentials from .env (HTTP 401).")
        except requests.RequestException as exc:
            last = type(exc).__name__
        time.sleep(5)
    raise SystemExit(f"GeoServer not reachable at {gs.base} after {timeout_s} s ({last}).")


# --------------------------------------------------------------------------- #
# Publishing
# --------------------------------------------------------------------------- #
def ensure_workspace(gs: GeoServer) -> None:
    if gs.exists(f"/workspaces/{WORKSPACE}.json"):
        print(f"  workspace {WORKSPACE}: exists")
        return
    gs.req("POST", "/workspaces", json={"workspace": {"name": WORKSPACE}})
    print(f"  workspace {WORKSPACE}: created")


def ensure_datastore(gs: GeoServer, cfg: dict) -> None:
    params = {
        "host": cfg["db_host_internal"],
        "port": cfg["db_port_internal"],
        "database": cfg["db"],
        "schema": "public",
        "user": cfg["db_user"],
        "passwd": cfg["db_password"],
        "dbtype": "postgis",
        "Expose primary keys": "true",
        "Loose bbox": "true",
        "Estimated extends": "false",
        "validate connections": "true",
        "max connections": "10",
    }
    body = {"dataStore": {
        "name": DATASTORE,
        "description": "Kochi healthcare PostGIS (Docker service 'postgis')",
        "enabled": True,
        "connectionParameters": {"entry": [{"@key": k, "$": v} for k, v in params.items()]},
    }}
    path = f"/workspaces/{WORKSPACE}/datastores"
    if gs.exists(f"{path}/{DATASTORE}.json"):
        gs.req("PUT", f"{path}/{DATASTORE}", json=body)
        print(f"  datastore {DATASTORE}: updated (host={params['host']}:{params['port']})")
    else:
        gs.req("POST", path, json=body)
        print(f"  datastore {DATASTORE}: created (host={params['host']}:{params['port']})")


def ensure_style(gs: GeoServer, name: str) -> None:
    sld_file = SLD_DIR / f"{name}.sld"
    if not sld_file.exists():
        raise SystemExit(f"Missing style file {sld_file}")
    body = sld_file.read_bytes()
    headers = {"Content-Type": "application/vnd.ogc.sld+xml"}
    path = f"/workspaces/{WORKSPACE}/styles"
    if gs.exists(f"{path}/{name}.json"):
        gs.req("PUT", f"{path}/{name}", data=body, headers=headers)
        print(f"  style {name}: updated")
    else:
        gs.req("POST", f"{path}?name={name}", data=body, headers=headers)
        print(f"  style {name}: created")


def ensure_layer(gs: GeoServer, name: str, native: str, style: str, title: str) -> None:
    ft_path = f"/workspaces/{WORKSPACE}/datastores/{DATASTORE}/featuretypes"
    body = {"featureType": {
        "name": name, "nativeName": native, "title": title,
        "abstract": f"PostGIS table/view public.{native}",
        "srs": "EPSG:4326", "projectionPolicy": "FORCE_DECLARED", "enabled": True,
    }}
    if gs.exists(f"{ft_path}/{name}.json"):
        action = "updated"
    else:
        gs.req("POST", ft_path, json=body)
        action = "published"
    # Recompute the bounding boxes from the data every time.
    gs.req("PUT", f"{ft_path}/{name}?recalculate=nativebbox,latlonbbox",
           json={"featureType": {"name": name, "title": title, "enabled": True}})
    gs.req("PUT", f"/layers/{WORKSPACE}:{name}",
           json={"layer": {"defaultStyle": {"name": style, "workspace": WORKSPACE}}})
    print(f"  layer {WORKSPACE}:{name:<22} <- public.{native:<28} style {style} ({action})")


def layer_bbox(gs: GeoServer, name: str) -> list[float]:
    r = gs.req("GET", f"/workspaces/{WORKSPACE}/datastores/{DATASTORE}/featuretypes/{name}.json")
    bb = r.json()["featureType"]["latLonBoundingBox"]
    return [float(bb["minx"]), float(bb["miny"]), float(bb["maxx"]), float(bb["maxy"])]


# --------------------------------------------------------------------------- #
# Evidence (real requests, real responses saved to disk)
# --------------------------------------------------------------------------- #
def save_map(gs: GeoServer, params: dict, filename: str) -> dict:
    url = f"{gs.base}/{WORKSPACE}/wms"
    r = gs.s.get(url, params=params, timeout=120, headers={"Accept": "*/*"})
    ctype = r.headers.get("Content-Type", "")
    out = EVIDENCE_DIR / filename
    if ctype.startswith("image/"):
        out.write_bytes(r.content)
    else:
        out = out.with_suffix(".xml")
        out.write_bytes(r.content)
    return {"file": out.name, "http": r.status_code, "content_type": ctype, "bytes": len(r.content),
            "request": {k: v for k, v in params.items()}}


def collect_evidence(gs: GeoServer) -> dict:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    minx, miny, maxx, maxy = layer_bbox(gs, "study_area")
    pad_x, pad_y = (maxx - minx) * 0.05, (maxy - miny) * 0.05
    minx, miny, maxx, maxy = minx - pad_x, miny - pad_y, maxx + pad_x, maxy + pad_y
    width = 600
    height = int(round(width * (maxy - miny) / (maxx - minx)))
    layers = f"{WORKSPACE}:study_area,{WORKSPACE}:building_access,{WORKSPACE}:healthcare"
    common = {"service": "WMS", "request": "GetMap", "layers": layers, "styles": "",
              "format": "image/png", "transparent": "true", "width": width, "height": height}
    lonlat = f"{minx},{miny},{maxx},{maxy}"
    latlon = f"{miny},{minx},{maxy},{maxx}"

    checks: dict = {"bbox_lonlat": [minx, miny, maxx, maxy], "wms": {}, "wfs": {}, "capabilities": {}}
    checks["wms"]["v111_epsg4326_lonlat"] = save_map(
        gs, {**common, "version": "1.1.1", "srs": "EPSG:4326", "bbox": lonlat}, "wms111_epsg4326.png")
    checks["wms"]["v130_epsg4326_latlon"] = save_map(
        gs, {**common, "version": "1.3.0", "crs": "EPSG:4326", "bbox": latlon}, "wms130_epsg4326.png")
    checks["wms"]["v130_epsg4326_WRONG_lonlat"] = save_map(
        gs, {**common, "version": "1.3.0", "crs": "EPSG:4326", "bbox": lonlat},
        "wms130_epsg4326_wrong_axis.png")
    checks["wms"]["v130_crs84_lonlat"] = save_map(
        gs, {**common, "version": "1.3.0", "crs": "CRS:84", "bbox": lonlat}, "wms130_crs84.png")
    checks["wms"]["cql_service_areas_hospital_3p6"] = save_map(
        gs, {**common, "version": "1.1.1", "srs": "EPSG:4326", "bbox": lonlat,
             "layers": f"{WORKSPACE}:study_area,{WORKSPACE}:service_areas,{WORKSPACE}:healthcare",
             "CQL_FILTER": "INCLUDE;facility_set='hospital' AND speed_kmh=3.6;type='hospital'"},
        "service_areas_hospital_3p6.png")

    # WFS 2.0 with a CQL filter (GeoServer vendor parameter)
    wfs = f"{gs.base}/{WORKSPACE}/wfs"
    r = gs.s.get(wfs, params={"service": "WFS", "version": "2.0.0", "request": "GetFeature",
                              "typeNames": f"{WORKSPACE}:healthcare", "outputFormat": "application/json",
                              "CQL_FILTER": "type='hospital'"}, timeout=120)
    j = r.json() if r.status_code == 200 else {}
    checks["wfs"]["cql_type_hospital"] = {"http": r.status_code, "features": len(j.get("features", [])),
                                         "numberMatched": j.get("numberMatched", j.get("totalFeatures"))}

    # WFS 2.0 with an OGC Filter Encoding 2.0 XML filter (POST)
    fes = f"""<?xml version="1.0" encoding="UTF-8"?>
<wfs:GetFeature service="WFS" version="2.0.0" outputFormat="application/json"
    xmlns:wfs="http://www.opengis.net/wfs/2.0" xmlns:fes="http://www.opengis.net/fes/2.0">
  <wfs:Query typeNames="healthcare">
    <fes:Filter>
      <fes:And>
        <fes:PropertyIsEqualTo>
          <fes:ValueReference>is_clinical</fes:ValueReference>
          <fes:Literal>true</fes:Literal>
        </fes:PropertyIsEqualTo>
        <fes:PropertyIsGreaterThan>
          <fes:ValueReference>hospital_t3p6_min</fes:ValueReference>
          <fes:Literal>10</fes:Literal>
        </fes:PropertyIsGreaterThan>
      </fes:And>
    </fes:Filter>
  </wfs:Query>
</wfs:GetFeature>"""
    r = gs.s.post(wfs, data=fes.encode(), headers={"Content-Type": "application/xml"}, timeout=120)
    j = r.json() if r.status_code == 200 and "json" in r.headers.get("Content-Type", "") else {}
    checks["wfs"]["fes_clinical_and_hospital_gt10min_at_3p6"] = {
        "http": r.status_code, "features": len(j.get("features", [])),
        "names": sorted({f["properties"].get("name") or "(unnamed)" for f in j.get("features", [])}),
        "error": None if j else r.text[:300]}
    (EVIDENCE_DIR / "wfs_fes_request.xml").write_text(fes, encoding="utf-8")

    # Capabilities: which layers each service advertises
    for svc, ver in (("WMS", "1.1.1"), ("WMS", "1.3.0"), ("WFS", "2.0.0")):
        r = gs.s.get(f"{gs.base}/{WORKSPACE}/ows",
                     params={"service": svc, "version": ver, "request": "GetCapabilities"},
                     timeout=120, headers={"Accept": "*/*"})
        text = r.text
        checks["capabilities"][f"{svc}_{ver}"] = {
            "http": r.status_code, "bytes": len(r.content),
            "layers_found": [n for n, *_ in LAYERS if f"{n}<" in text or f":{n}<" in text]}
    return checks


# --------------------------------------------------------------------------- #
def main() -> int:
    p = argparse.ArgumentParser(description="Publish Kochi layers in GeoServer and collect OGC evidence.")
    p.add_argument("--evidence-only", action="store_true", help="Skip publishing, only re-run checks.")
    args = p.parse_args()

    cfg = settings()
    gs = GeoServer(cfg)
    print(f"GeoServer: {gs.base}")
    version = wait_for_geoserver(gs)
    print(f"  version {version}")

    if not args.evidence_only:
        print("\n[1/4] Workspace and datastore")
        ensure_workspace(gs)
        ensure_datastore(gs, cfg)
        print("\n[2/4] Styles")
        for style in sorted({s for _, _, s, _ in LAYERS}):
            ensure_style(gs, style)
        print("\n[3/4] Layers")
        for name, native, style, title in LAYERS:
            ensure_layer(gs, name, native, style, title)

    print("\n[4/4] OGC evidence")
    checks = collect_evidence(gs)
    result = {"generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "geoserver": gs.base, "geoserver_version": version, "workspace": WORKSPACE, **checks}
    CHECKS_JSON.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    for key, v in checks["wms"].items():
        print(f"  WMS {key:<34} HTTP {v['http']}  {v['content_type']:<12} {v['bytes']:>8} bytes  -> {v['file']}")
    for key, v in checks["wfs"].items():
        print(f"  WFS {key:<34} HTTP {v['http']}  features={v['features']}")
    for key, v in checks["capabilities"].items():
        print(f"  {key:<38} HTTP {v['http']}  layers={len(v['layers_found'])}/{len(LAYERS)}")
    print(f"\nSaved {CHECKS_JSON.name} and images in {EVIDENCE_DIR.name}/")
    print("Compare wms130_epsg4326_wrong_axis.png with wms130_epsg4326.png: the wrong axis order "
          "is read as latitude 76.3 N, longitude 10.0 E (near Svalbard), so it comes back empty (small file).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
