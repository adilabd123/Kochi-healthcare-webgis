#!/usr/bin/env python3
"""
Phase 4 - Copernicus GLO-30 DEM -> clipped GeoTIFF -> GeoServer WMS + WCS
=========================================================================

Plain-language idea
    The Copernicus DEM is published as 1 x 1 degree GeoTIFF "tiles". Our study
    area (lat 9.98-10.03) straddles the 10 N line, so it needs two tiles:
    N09 E076 and N10 E076. The script downloads both, stitches them together
    (mosaic), cuts out the study area plus a small buffer (clip), and saves
    analysis/kochi_dem.tif. It then uploads that file to GeoServer, which serves
    it two ways:
      * WMS  -> a coloured picture of elevation (for the map client)
      * WCS  -> the actual elevation numbers as a GeoTIFF (for analysis)

Source
    https://copernicus-dem-30m.s3.amazonaws.com/
    Copernicus_DSM_COG_10_N09_00_E076_00_DEM/Copernicus_DSM_COG_10_N09_00_E076_00_DEM.tif
    Copernicus_DSM_COG_10_N10_00_E076_00_DEM/Copernicus_DSM_COG_10_N10_00_E076_00_DEM.tif
    GLO-30 is a surface model (DSM: includes buildings and trees), heights in metres
    above the EGM2008 geoid, EPSG:4326, ~30 m (1 arc-second) cells.
    Produced using Copernicus WorldDEM-30 (c) DLR e.V. 2010-2014 and (c) Airbus
    Defence and Space GmbH 2014-2018, provided under COPERNICUS by the European Union and ESA.

Outputs
    analysis/dem_cache/*.tif                     downloaded source tiles (re-used)
    analysis/kochi_dem.tif                       clipped mosaic (float32, deflate, nodata -9999)
    sld/dem_elevation.sld                        colour ramp built from the real value range
    GeoServer kochi:dem (store kochi_dem)        WMS + WCS layer
    analysis/phase4_evidence/dem_wms.png         WMS GetMap
    analysis/phase4_evidence/dem_wcs_subset.tif  WCS 2.0.1 GetCoverage (subset)
    analysis/phase4_dem_checks.json              all checks with real responses

Usage
    python analysis/phase4_dem_geoserver.py
    python analysis/phase4_dem_geoserver.py --buffer-m 500 --skip-publish
    python analysis/phase4_dem_geoserver.py --checks-only
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

import geopandas as gpd
import numpy as np
import rasterio
import requests
from rasterio.io import MemoryFile
from rasterio.mask import mask as rio_mask
from rasterio.merge import merge as rio_merge
from rasterio.windows import from_bounds
from shapely.geometry import mapping
from shapely.ops import unary_union

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
ENV_FILE = PROJECT_ROOT / ".env"
STUDY_AREA = SCRIPT_DIR / "study_area.geojson"
CACHE_DIR = SCRIPT_DIR / "dem_cache"
DEM_OUT = SCRIPT_DIR / "kochi_dem.tif"
SLD_OUT = PROJECT_ROOT / "sld" / "dem_elevation.sld"
EVIDENCE_DIR = SCRIPT_DIR / "phase4_evidence"
CHECKS_JSON = SCRIPT_DIR / "phase4_dem_checks.json"

BUCKET = "https://copernicus-dem-30m.s3.amazonaws.com"
WORKSPACE = "kochi"
STORE = "kochi_dem"
LAYER = "dem"
STYLE = "dem_elevation"
NODATA = -9999.0
UTM = "EPSG:32643"


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1]
            values[k.strip()] = v
    return values


def settings() -> dict[str, str]:
    f = load_env(ENV_FILE)

    def get(k: str, d: str) -> str:
        return os.environ.get(k) or f.get(k) or d

    port = get("GEOSERVER_PORT", "8088")
    return {
        "base": get("GEOSERVER_URL", f"http://localhost:{port}/geoserver").rstrip("/"),
        "user": get("GEOSERVER_ADMIN_USER", "admin"),
        "password": get("GEOSERVER_ADMIN_PASSWORD", "geoserver"),
    }


def log(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------- #
# 1. Study area and tile list
# --------------------------------------------------------------------------- #
def study_geometry(buffer_m: float):
    if not STUDY_AREA.exists():
        raise SystemExit(f"Missing {STUDY_AREA}; run analysis/step0_verify_kochi.py first.")
    gdf = gpd.read_file(STUDY_AREA)
    if gdf.crs is None:
        gdf = gdf.set_crs(4326)
    utm = gdf.to_crs(UTM)
    study_utm = unary_union(list(utm.geometry))
    buffered = gpd.GeoSeries([study_utm.buffer(buffer_m)], crs=UTM).to_crs(4326).iloc[0]
    study = gpd.GeoSeries([study_utm], crs=UTM).to_crs(4326).iloc[0]
    return study, buffered


def tile_name(lat: int, lon: int) -> str:
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"Copernicus_DSM_COG_10_{ns}{abs(lat):02d}_00_{ew}{abs(lon):03d}_00_DEM"


def tiles_for(bounds: tuple[float, float, float, float]) -> list[str]:
    minx, miny, maxx, maxy = bounds
    names = []
    for lat in range(math.floor(miny), math.floor(maxy) + 1):
        for lon in range(math.floor(minx), math.floor(maxx) + 1):
            names.append(tile_name(lat, lon))
    return names


def download_tile(name: str) -> Path | None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out = CACHE_DIR / f"{name}.tif"
    if out.exists() and out.stat().st_size > 0:
        log(f"  {name}: cached ({out.stat().st_size / 1e6:.1f} MB)")
        return out
    url = f"{BUCKET}/{name}/{name}.tif"
    log(f"  {name}: downloading {url}")
    t0 = time.perf_counter()
    try:
        with requests.get(url, stream=True, timeout=(15, 300)) as r:
            if r.status_code == 404:
                log(f"  {name}: not in bucket (ocean-only tile) - skipped")
                return None
            r.raise_for_status()
            total = int(r.headers.get("Content-Length", 0))
            tmp = out.with_suffix(".part")
            done = 0
            with open(tmp, "wb") as fh:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    fh.write(chunk)
                    done += len(chunk)
                    if total:
                        print(f"\r    {done / 1e6:6.1f} / {total / 1e6:.1f} MB", end="", flush=True)
            print()
            tmp.replace(out)
    except requests.RequestException as exc:
        raise SystemExit(f"Download failed for {url}: {exc}") from exc
    log(f"  {name}: {out.stat().st_size / 1e6:.1f} MB in {time.perf_counter() - t0:.1f} s")
    return out


# --------------------------------------------------------------------------- #
# 2. Mosaic + clip
# --------------------------------------------------------------------------- #
def build_dem(tile_paths: list[Path], buffered, study) -> dict:
    srcs = [rasterio.open(p) for p in tile_paths]
    try:
        for s in srcs:
            if s.crs is None or s.crs.to_epsg() != 4326:
                raise SystemExit(f"Unexpected CRS {s.crs} in {s.name}")
        minx, miny, maxx, maxy = buffered.bounds
        mosaic, transform = rio_merge(srcs, bounds=(minx, miny, maxx, maxy), nodata=NODATA,
                                      dtype="float32")
        profile = srcs[0].profile.copy()
    finally:
        for s in srcs:
            s.close()

    profile.update(driver="GTiff", height=mosaic.shape[1], width=mosaic.shape[2], count=1,
                   transform=transform, dtype="float32", nodata=NODATA, crs="EPSG:4326")
    with MemoryFile() as mem:
        with mem.open(**profile) as tmp:
            tmp.write(mosaic.astype("float32"))
        with mem.open() as tmp:
            clipped, clip_transform = rio_mask(tmp, [mapping(buffered)], crop=True,
                                               nodata=NODATA, filled=True)

    out_profile = {
        "driver": "GTiff", "dtype": "float32", "count": 1, "crs": "EPSG:4326",
        "height": clipped.shape[1], "width": clipped.shape[2], "transform": clip_transform,
        "nodata": NODATA, "compress": "deflate", "predictor": 3,
        "tiled": True, "blockxsize": 256, "blockysize": 256,
    }
    with rasterio.open(DEM_OUT, "w", **out_profile) as dst:
        dst.write(clipped.astype("float32"))
        dst.update_tags(SOURCE="Copernicus GLO-30 DSM (COG) via copernicus-dem-30m.s3.amazonaws.com",
                        VERTICAL_DATUM="EGM2008 geoid, metres", STUDY_AREA="Edappally+Palarivattom+Kaloor")
        dst.set_band_description(1, "elevation_m")

    stats = raster_stats(DEM_OUT, study)
    return stats


def raster_stats(path: Path, study=None) -> dict:
    with rasterio.open(path) as ds:
        arr = ds.read(1, masked=True)
        valid = arr.compressed()
        res_x, res_y = ds.res
        lat_c = (ds.bounds.top + ds.bounds.bottom) / 2
        cell_m = (res_x * 111320 * math.cos(math.radians(lat_c)), res_y * 110574)
        out = {
            "file": path.name,
            "size_bytes": path.stat().st_size,
            "width": ds.width, "height": ds.height, "crs": str(ds.crs),
            "bounds": [round(v, 6) for v in ds.bounds],
            "cell_deg": [res_x, res_y],
            "cell_m_approx": [round(cell_m[0], 1), round(cell_m[1], 1)],
            "valid_cells": int(valid.size),
            "min_m": round(float(valid.min()), 2) if valid.size else None,
            "max_m": round(float(valid.max()), 2) if valid.size else None,
            "mean_m": round(float(valid.mean()), 2) if valid.size else None,
            "p02_m": round(float(np.percentile(valid, 2)), 2) if valid.size else None,
            "p98_m": round(float(np.percentile(valid, 98)), 2) if valid.size else None,
        }
        if study is not None:
            c = study.centroid
            val = next(ds.sample([(c.x, c.y)]))[0]
            out["centroid"] = {"lat": round(c.y, 6), "lon": round(c.x, 6),
                               "elevation_m": None if val == NODATA else round(float(val), 2)}
            inside, _ = rio_mask(ds, [mapping(study)], crop=True, nodata=NODATA, filled=False)
            v = inside.compressed()
            out["inside_study_area"] = {
                "cells": int(v.size),
                "min_m": round(float(v.min()), 2), "max_m": round(float(v.max()), 2),
                "mean_m": round(float(v.mean()), 2),
                "pct_below_5m": round(100.0 * float((v < 5).mean()), 2),
            }
    return out


# --------------------------------------------------------------------------- #
# 3. SLD colour ramp from the real value range
# --------------------------------------------------------------------------- #
def write_sld(stats: dict) -> str:
    lo = math.floor(stats["p02_m"])
    hi = math.ceil(stats["p98_m"])
    if hi - lo < 4:
        hi = lo + 4
    colors = ["#1a9850", "#91cf60", "#d9ef8b", "#fee08b", "#fc8d59", "#d73027"]
    steps = [lo + (hi - lo) * i / (len(colors) - 1) for i in range(len(colors))]
    entries = "\n".join(
        f'              <ColorMapEntry color="{c}" quantity="{q:.1f}" label="{q:.0f} m" opacity="0.75"/>'
        for c, q in zip(colors, steps))
    sld = f"""<?xml version="1.0" encoding="UTF-8"?>
<StyledLayerDescriptor version="1.0.0"
    xmlns="http://www.opengis.net/sld"
    xmlns:ogc="http://www.opengis.net/ogc"
    xmlns:xlink="http://www.w3.org/1999/xlink"
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    xsi:schemaLocation="http://www.opengis.net/sld http://schemas.opengis.net/sld/1.0.0/StyledLayerDescriptor.xsd">
  <NamedLayer>
    <Name>{STYLE}</Name>
    <UserStyle>
      <Name>{STYLE}</Name>
      <Title>Copernicus GLO-30 elevation (m, EGM2008)</Title>
      <Abstract>Ramp from the 2nd ({stats['p02_m']} m) to the 98th ({stats['p98_m']} m) percentile of analysis/kochi_dem.tif</Abstract>
      <FeatureTypeStyle>
        <Rule>
          <Name>elevation</Name>
          <RasterSymbolizer>
            <Opacity>1.0</Opacity>
            <ColorMap type="ramp">
              <ColorMapEntry color="#000000" quantity="{NODATA:.0f}" opacity="0.0"/>
{entries}
            </ColorMap>
          </RasterSymbolizer>
        </Rule>
      </FeatureTypeStyle>
    </UserStyle>
  </NamedLayer>
</StyledLayerDescriptor>
"""
    SLD_OUT.parent.mkdir(parents=True, exist_ok=True)
    SLD_OUT.write_text(sld, encoding="utf-8")
    return sld


# --------------------------------------------------------------------------- #
# 4. GeoServer REST
# --------------------------------------------------------------------------- #
class GeoServer:
    def __init__(self, cfg: dict):
        self.base = cfg["base"]
        self.rest = f"{self.base}/rest"
        self.s = requests.Session()
        self.s.trust_env = False
        self.s.auth = (cfg["user"], cfg["password"])

    def call(self, method: str, path: str, ok=(200, 201), **kw) -> requests.Response:
        url = path if path.startswith("http") else f"{self.rest}{path}"
        r = self.s.request(method, url, timeout=300, **kw)
        if r.status_code not in ok:
            raise RuntimeError(f"{method} {url} -> HTTP {r.status_code}: {r.text[:400]}")
        return r

    def exists(self, path: str) -> bool:
        r = self.s.get(f"{self.rest}{path}", timeout=60, headers={"Accept": "application/json"})
        if r.status_code in (200, 404):
            return r.status_code == 200
        raise RuntimeError(f"GET {path} -> HTTP {r.status_code}: {r.text[:300]}")


def wait_ready(gs: GeoServer) -> str:
    for _ in range(60):
        try:
            r = gs.s.get(f"{gs.rest}/about/version.json", timeout=10)
            if r.status_code == 401:
                raise SystemExit("GeoServer rejected the admin credentials (HTTP 401). Check .env.")
            if r.status_code == 200:
                for res in r.json()["about"]["resource"]:
                    if res.get("@name") == "GeoServer":
                        return str(res.get("Version"))
                return "unknown"
        except requests.RequestException:
            pass
        time.sleep(5)
    raise SystemExit(f"GeoServer not reachable at {gs.base}")


def publish(gs: GeoServer, sld: str) -> None:
    if not gs.exists(f"/workspaces/{WORKSPACE}.json"):
        gs.call("POST", "/workspaces", json={"workspace": {"name": WORKSPACE}})
        log(f"  workspace {WORKSPACE}: created")

    # Upload the GeoTIFF; GeoServer creates store + coverage + layer in one call.
    with open(DEM_OUT, "rb") as fh:
        gs.call("PUT", f"/workspaces/{WORKSPACE}/coveragestores/{STORE}/file.geotiff",
                params={"configure": "first", "coverageName": LAYER},
                data=fh, headers={"Content-Type": "image/tiff"})
    log(f"  coverage store {STORE}: uploaded {DEM_OUT.name} ({DEM_OUT.stat().st_size / 1e6:.2f} MB)")

    gs.call("PUT", f"/workspaces/{WORKSPACE}/coveragestores/{STORE}/coverages/{LAYER}",
            json={"coverage": {"name": LAYER, "title": "Copernicus GLO-30 DEM (m, EGM2008) - Kochi study area",
                               "abstract": "Clipped to study area + buffer. DSM: includes buildings and trees.",
                               "enabled": True}})

    headers = {"Content-Type": "application/vnd.ogc.sld+xml"}
    style_path = f"/workspaces/{WORKSPACE}/styles"
    if gs.exists(f"{style_path}/{STYLE}.json"):
        gs.call("PUT", f"{style_path}/{STYLE}", data=sld.encode("utf-8"), headers=headers)
        log(f"  style {STYLE}: updated")
    else:
        gs.call("POST", f"{style_path}?name={STYLE}", data=sld.encode("utf-8"), headers=headers)
        log(f"  style {STYLE}: created")
    gs.call("PUT", f"/layers/{WORKSPACE}:{LAYER}",
            json={"layer": {"defaultStyle": {"name": STYLE, "workspace": WORKSPACE}}})
    log(f"  layer {WORKSPACE}:{LAYER}: default style {STYLE}")


# --------------------------------------------------------------------------- #
# 5. Verification against the live server
# --------------------------------------------------------------------------- #
def checks(gs: GeoServer, study, local_stats: dict) -> dict:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    minx, miny, maxx, maxy = study.bounds
    out: dict = {}

    # WMS GetMap
    width = 600
    height = int(round(width * (maxy - miny) / (maxx - minx)))
    params = {"service": "WMS", "version": "1.1.1", "request": "GetMap",
              "layers": f"{WORKSPACE}:{LAYER},{WORKSPACE}:study_area", "styles": "",
              "srs": "EPSG:4326", "bbox": f"{minx},{miny},{maxx},{maxy}",
              "width": width, "height": height, "format": "image/png", "transparent": "true"}
    r = gs.s.get(f"{gs.base}/{WORKSPACE}/wms", params=params, timeout=120)
    ctype = r.headers.get("Content-Type", "")
    png = EVIDENCE_DIR / ("dem_wms.png" if ctype.startswith("image/") else "dem_wms_error.xml")
    png.write_bytes(r.content)
    out["wms_getmap"] = {"http": r.status_code, "content_type": ctype, "bytes": len(r.content),
                         "file": png.name, "ok": r.status_code == 200 and ctype.startswith("image/png")}

    # WMS GetFeatureInfo at the study centroid: elevation read by GeoServer
    c = study.centroid
    d = 0.0005
    r = gs.s.get(f"{gs.base}/{WORKSPACE}/wms", params={
        "service": "WMS", "version": "1.1.1", "request": "GetFeatureInfo",
        "layers": f"{WORKSPACE}:{LAYER}", "query_layers": f"{WORKSPACE}:{LAYER}", "styles": "",
        "srs": "EPSG:4326", "bbox": f"{c.x - d},{c.y - d},{c.x + d},{c.y + d}",
        "width": 101, "height": 101, "x": 50, "y": 50, "info_format": "application/json"},
        timeout=60)
    gfi_val = None
    if r.status_code == 200 and "json" in r.headers.get("Content-Type", ""):
        feats = r.json().get("features", [])
        if feats:
            props = feats[0].get("properties", {})
            gfi_val = next(iter(props.values()), None)
    out["wms_getfeatureinfo_centroid"] = {
        "http": r.status_code, "elevation_m_geoserver": gfi_val,
        "elevation_m_local": local_stats.get("centroid", {}).get("elevation_m")}

    # WCS 2.0.1 GetCapabilities + DescribeCoverage
    cov_id = f"{WORKSPACE}__{LAYER}"
    r = gs.s.get(f"{gs.base}/wcs", params={"service": "WCS", "version": "2.0.1",
                                           "request": "GetCapabilities"}, timeout=120)
    (EVIDENCE_DIR / "wcs_capabilities.xml").write_bytes(r.content)
    out["wcs_getcapabilities"] = {"http": r.status_code, "bytes": len(r.content),
                                  "coverage_id": cov_id, "listed": cov_id in r.text}
    r = gs.s.get(f"{gs.base}/wcs", params={"service": "WCS", "version": "2.0.1",
                                           "request": "DescribeCoverage", "coverageId": cov_id},
                 timeout=120)
    (EVIDENCE_DIR / "wcs_describecoverage.xml").write_bytes(r.content)
    out["wcs_describecoverage"] = {"http": r.status_code, "bytes": len(r.content)}

    # WCS 2.0.1 GetCoverage: a 1 km-ish window around the centroid, as GeoTIFF
    sx0, sx1 = c.x - 0.01, c.x + 0.01
    sy0, sy1 = c.y - 0.01, c.y + 0.01
    r = gs.s.get(f"{gs.base}/wcs", params=[
        ("service", "WCS"), ("version", "2.0.1"), ("request", "GetCoverage"),
        ("coverageId", cov_id), ("format", "image/tiff"),
        ("subset", f"Long({sx0},{sx1})"), ("subset", f"Lat({sy0},{sy1})")], timeout=180)
    ctype = r.headers.get("Content-Type", "")
    wcs_out = {"http": r.status_code, "content_type": ctype, "bytes": len(r.content),
               "subset_lonlat": [round(sx0, 6), round(sy0, 6), round(sx1, 6), round(sy1, 6)]}
    if r.status_code == 200 and "tiff" in ctype:
        tif = EVIDENCE_DIR / "dem_wcs_subset.tif"
        tif.write_bytes(r.content)
        with rasterio.open(tif) as ds:
            a = ds.read(1, masked=True)
            wcs_out.update({"file": tif.name, "width": ds.width, "height": ds.height,
                            "crs": str(ds.crs), "mean_m": round(float(a.mean()), 2),
                            "min_m": round(float(a.min()), 2), "max_m": round(float(a.max()), 2)})
        with rasterio.open(DEM_OUT) as ds:
            win = from_bounds(sx0, sy0, sx1, sy1, ds.transform)
            a = ds.read(1, window=win, masked=True)
            wcs_out["local_same_window_mean_m"] = round(float(a.mean()), 2)
    else:
        (EVIDENCE_DIR / "dem_wcs_error.xml").write_bytes(r.content)
        wcs_out["error"] = r.text[:300]
    out["wcs_getcoverage"] = wcs_out
    return out


# --------------------------------------------------------------------------- #
def main() -> int:
    p = argparse.ArgumentParser(description="Copernicus GLO-30 DEM for Kochi -> GeoServer WMS/WCS")
    p.add_argument("--buffer-m", type=float, default=500.0, help="Buffer around the study area (m)")
    p.add_argument("--skip-publish", action="store_true", help="Only build analysis/kochi_dem.tif")
    p.add_argument("--checks-only", action="store_true", help="Only re-run the GeoServer checks")
    args = p.parse_args()

    study, buffered = study_geometry(args.buffer_m)
    result: dict = {"generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "buffer_m": args.buffer_m}

    if args.checks_only:
        if not DEM_OUT.exists():
            raise SystemExit(f"{DEM_OUT} not found; run without --checks-only first.")
        local = raster_stats(DEM_OUT, study)
    else:
        names = tiles_for(buffered.bounds)
        log(f"[1/4] Copernicus GLO-30 tiles for bbox {tuple(round(v, 5) for v in buffered.bounds)}:")
        paths = [pt for pt in (download_tile(n) for n in names) if pt is not None]
        if not paths:
            raise SystemExit("No DEM tiles available for this area.")
        result["tiles"] = [pt.name for pt in paths]

        log(f"\n[2/4] Mosaic + clip (study area + {args.buffer_m:.0f} m)")
        local = build_dem(paths, buffered, study)
        log(f"  saved {DEM_OUT.name}: {local['width']} x {local['height']} cells "
            f"(~{local['cell_m_approx'][0]} x {local['cell_m_approx'][1]} m), "
            f"{local['size_bytes'] / 1e6:.2f} MB")
        log(f"  elevation (buffered extent): min {local['min_m']} / mean {local['mean_m']} / "
            f"max {local['max_m']} m; p2-p98 {local['p02_m']}-{local['p98_m']} m")
        ins = local["inside_study_area"]
        log(f"  inside study area: min {ins['min_m']} / mean {ins['mean_m']} / max {ins['max_m']} m; "
            f"{ins['pct_below_5m']}% of cells below 5 m")
        log(f"  centroid {local['centroid']['lat']}, {local['centroid']['lon']}: "
            f"{local['centroid']['elevation_m']} m")
        sld = write_sld(local)
        log(f"  saved {SLD_OUT.relative_to(PROJECT_ROOT)}")
    result["local_dem"] = local

    if args.skip_publish:
        CHECKS_JSON.write_text(json.dumps(result, indent=2), encoding="utf-8")
        log(f"\nPublishing skipped. Saved {CHECKS_JSON.name}")
        return 0

    gs = GeoServer(settings())
    log(f"\n[3/4] GeoServer {gs.base}")
    result["geoserver_version"] = wait_ready(gs)
    log(f"  version {result['geoserver_version']}")
    if not args.checks_only:
        publish(gs, sld)

    log("\n[4/4] Verification")
    result["checks"] = checks(gs, study, local)
    CHECKS_JSON.write_text(json.dumps(result, indent=2), encoding="utf-8")

    ck = result["checks"]
    log(f"  WMS GetMap          HTTP {ck['wms_getmap']['http']}  {ck['wms_getmap']['content_type']}  "
        f"{ck['wms_getmap']['bytes']} bytes -> {ck['wms_getmap']['file']}")
    g = ck["wms_getfeatureinfo_centroid"]
    log(f"  WMS GetFeatureInfo  HTTP {g['http']}  centroid elevation GeoServer={g['elevation_m_geoserver']} "
        f"m, local={g['elevation_m_local']} m")
    log(f"  WCS GetCapabilities HTTP {ck['wcs_getcapabilities']['http']}  "
        f"{ck['wcs_getcapabilities']['coverage_id']} listed={ck['wcs_getcapabilities']['listed']}")
    log(f"  WCS DescribeCoverage HTTP {ck['wcs_describecoverage']['http']}")
    w = ck["wcs_getcoverage"]
    if "file" in w:
        log(f"  WCS GetCoverage     HTTP {w['http']}  {w['width']}x{w['height']} {w['crs']}  "
            f"mean {w['mean_m']} m (local same window {w['local_same_window_mean_m']} m) -> {w['file']}")
    else:
        log(f"  WCS GetCoverage     HTTP {w['http']}  FAILED: {w.get('error')}")

    ok = (ck["wms_getmap"]["ok"] and ck["wcs_getcapabilities"]["listed"] and "file" in w)
    log(f"\n{'ALL DEM CHECKS PASSED' if ok else 'SOME DEM CHECKS FAILED'} - saved {CHECKS_JSON.name}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
