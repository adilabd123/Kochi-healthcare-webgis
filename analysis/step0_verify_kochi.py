#!/usr/bin/env python3
"""
Step 0 - Verify the Kochi study area and Healthcare feature count.

Study area : Edappally + Palarivattom + Kaloor wards, Kochi Municipal Corporation
Category   : Healthcare -> amenity=hospital | amenity=clinic | amenity=doctors | healthcare=*
CRS        : EPSG:4326 for storage/output, EPSG:32643 (UTM 43N) for all metric work

What it does
  1. Geocodes each ward from OpenStreetMap (Nominatim via osmnx) and checks the
     result is a plausible ward polygon (area and distance from the study centre).
  2. If all three wards are found, dissolves them into one study-area polygon.
     If any ward is missing or implausible, falls back to a metric rectangle
     around 10.008 N, 76.302 E (any ward polygons that *were* found are unioned
     into it so nothing is lost).
  3. Downloads Healthcare features from Overpass, converts every feature to a
     representative point, keeps only points inside the study area, and removes
     duplicates (same OSM element; or same normalised name within 50 m, which is
     the usual "hospital drawn as building AND as node" case).
  4. Prints the exact count and exits with code 0 if >= 15, else code 2
     (code 3 = Nominatim/Overpass unreachable; no fallback is applied then).
  5. Writes analysis/study_area.geojson and analysis/healthcare_features.geojson
     (both EPSG:4326).

Usage
  python analysis/step0_verify_kochi.py
  python analysis/step0_verify_kochi.py --force-bbox
  python analysis/step0_verify_kochi.py --half-width-km 2.2 --half-height-km 2.8
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import geopandas as gpd
import osmnx as ox
import pandas as pd
import requests
from shapely.geometry import Point, box
from shapely.ops import unary_union

# --------------------------------------------------------------------------- #
# Fixed project parameters
# --------------------------------------------------------------------------- #
SCRIPT_DIR = Path(__file__).resolve().parent
WGS84 = "EPSG:4326"
UTM43N = "EPSG:32643"

CENTER_LAT = 10.008
CENTER_LON = 76.302

MIN_FEATURES = 15

# Each ward is tried with several spellings / parent-area names, in order.
WARD_QUERIES: dict[str, list[str]] = {
    "Edappally": [
        "Edappally, Kochi, Kerala, India",
        "Edappally, Ernakulam, Kerala, India",
        "Edapally, Kochi, Kerala, India",
    ],
    "Palarivattom": [
        "Palarivattom, Kochi, Kerala, India",
        "Palarivattom, Ernakulam, Kerala, India",
    ],
    "Kaloor": [
        "Kaloor, Kochi, Kerala, India",
        "Kaloor, Ernakulam, Kerala, India",
    ],
}

# A city ward in Kochi is a few km^2. Anything far outside this range is
# a wrong match (a single building, or a whole block/taluk).
WARD_MIN_KM2 = 0.2
WARD_MAX_KM2 = 25.0
WARD_MAX_CENTROID_DIST_KM = 6.0

HEALTH_TAGS = {
    "amenity": ["hospital", "clinic", "doctors"],
    "healthcare": True,  # healthcare=*  (any value)
}
AMENITY_VALUES = set(HEALTH_TAGS["amenity"])

DEDUP_DISTANCE_M = 50.0

# Optional tag columns copied into the output if Overpass returned them.
EXTRA_TAG_COLUMNS = [
    "healthcare:speciality",
    "operator",
    "operator:type",
    "emergency",
    "beds",
    "opening_hours",
    "phone",
    "addr:street",
    "addr:postcode",
]


# --------------------------------------------------------------------------- #
# osmnx configuration (works with osmnx 1.x and 2.x)
# --------------------------------------------------------------------------- #
def configure_osmnx() -> None:
    cache_dir = SCRIPT_DIR / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    ox.settings.use_cache = True
    ox.settings.cache_folder = str(cache_dir)
    ox.settings.log_console = False
    for attr in ("requests_timeout", "timeout"):  # 2.x name, 1.x name
        if hasattr(ox.settings, attr):
            setattr(ox.settings, attr, 180)


class NetworkUnavailable(RuntimeError):
    """Raised when Nominatim/Overpass cannot be reached (never silently fall back)."""


NETWORK_ERRORS = (requests.exceptions.ConnectionError, requests.exceptions.Timeout)


def is_insufficient_response(exc: Exception) -> bool:
    """osmnx raises InsufficientResponseError when Overpass/Nominatim return nothing."""
    return type(exc).__name__ == "InsufficientResponseError"


# --------------------------------------------------------------------------- #
# Study-area construction
# --------------------------------------------------------------------------- #
def center_point_utm() -> Point:
    pt = gpd.GeoSeries([Point(CENTER_LON, CENTER_LAT)], crs=WGS84).to_crs(UTM43N)
    return pt.iloc[0]


def validate_ward_polygon(geom_utm, center_utm: Point) -> tuple[bool, str]:
    """Return (is_valid, reason) for a candidate ward geometry in UTM metres."""
    if geom_utm is None or geom_utm.is_empty:
        return False, "empty geometry"
    if geom_utm.geom_type not in ("Polygon", "MultiPolygon"):
        return False, f"geometry is {geom_utm.geom_type}, not a polygon"
    area_km2 = geom_utm.area / 1e6
    if not (WARD_MIN_KM2 <= area_km2 <= WARD_MAX_KM2):
        return False, f"area {area_km2:.2f} km2 outside [{WARD_MIN_KM2}, {WARD_MAX_KM2}]"
    dist_km = geom_utm.centroid.distance(center_utm) / 1000.0
    if dist_km > WARD_MAX_CENTROID_DIST_KM:
        return False, f"centroid {dist_km:.1f} km from study centre"
    return True, f"ok ({area_km2:.2f} km2, centroid {dist_km:.2f} km from centre)"


def fetch_ward_polygon(ward: str, queries: list[str], center_utm: Point):
    """Try each query for a ward; return (geometry_utm, query_used) or (None, None)."""
    for query in queries:
        try:
            gdf = ox.geocode_to_gdf(query)
        except NETWORK_ERRORS as exc:
            raise NetworkUnavailable(f"Cannot reach Nominatim: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - Nominatim/osmnx raise several types
            print(f"    [{ward}] '{query}' -> no polygon ({type(exc).__name__}: {exc})")
            continue
        if gdf.empty:
            print(f"    [{ward}] '{query}' -> empty result")
            continue
        geom_utm = gdf.to_crs(UTM43N).geometry.iloc[0]
        ok, reason = validate_ward_polygon(geom_utm, center_utm)
        print(f"    [{ward}] '{query}' -> {reason}")
        if ok:
            return geom_utm, query
    return None, None


def fallback_rectangle_utm(half_width_km: float, half_height_km: float):
    c = center_point_utm()
    hw, hh = half_width_km * 1000.0, half_height_km * 1000.0
    return box(c.x - hw, c.y - hh, c.x + hw, c.y + hh)


def build_study_area(force_bbox: bool, half_width_km: float, half_height_km: float):
    """Return (study_area_utm_geometry, source_label, wards_found list)."""
    center_utm = center_point_utm()
    found: dict[str, object] = {}

    if not force_bbox:
        print("Geocoding ward polygons from OSM ...")
        for ward, queries in WARD_QUERIES.items():
            geom, _ = fetch_ward_polygon(ward, queries, center_utm)
            if geom is not None:
                found[ward] = geom

    if len(found) == len(WARD_QUERIES):
        # Small +/- buffer closes slivers between adjacent ward polygons.
        merged = unary_union(list(found.values())).buffer(5).buffer(-5)
        return merged, "osm_ward_polygons", list(found.keys())

    rect = fallback_rectangle_utm(half_width_km, half_height_km)
    if force_bbox:
        print("--force-bbox set: using fallback rectangle.")
        return rect, "bbox_fallback", []

    missing = [w for w in WARD_QUERIES if w not in found]
    print(f"Ward polygons incomplete (missing: {', '.join(missing)}). Using fallback rectangle "
          f"{2 * half_width_km:.1f} km x {2 * half_height_km:.1f} km around "
          f"{CENTER_LAT} N, {CENTER_LON} E.")
    if found:
        merged = unary_union([rect, *found.values()])
        return merged, "bbox_fallback+partial_osm", list(found.keys())
    return rect, "bbox_fallback", []


# --------------------------------------------------------------------------- #
# Healthcare features
# --------------------------------------------------------------------------- #
def normalise_name(value) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"[^a-z0-9]", "", value.lower())


def classify(row) -> str:
    amenity = row.get("amenity")
    if isinstance(amenity, str) and amenity in AMENITY_VALUES:
        return amenity
    healthcare = row.get("healthcare")
    if isinstance(healthcare, str) and healthcare.strip():
        return f"healthcare={healthcare.strip()}"
    return "other"


def standardise_index(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """osmnx 2.x index = (element, id); osmnx 1.x index = (element_type, osmid)."""
    gdf = gdf.reset_index()
    rename = {"element": "osm_type", "element_type": "osm_type", "id": "osm_id", "osmid": "osm_id"}
    gdf = gdf.rename(columns={k: v for k, v in rename.items() if k in gdf.columns})
    if "osm_type" not in gdf.columns or "osm_id" not in gdf.columns:
        raise RuntimeError(f"Unexpected osmnx feature columns: {list(gdf.columns)[:12]}")
    return gdf


def download_healthcare(study_utm) -> gpd.GeoDataFrame:
    study_wgs84 = gpd.GeoSeries([study_utm], crs=UTM43N).to_crs(WGS84).iloc[0]
    print("Downloading Healthcare features from Overpass ...")
    try:
        raw = ox.features_from_polygon(study_wgs84, tags=HEALTH_TAGS)
    except NETWORK_ERRORS as exc:
        raise NetworkUnavailable(f"Cannot reach Overpass API: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        if is_insufficient_response(exc):
            print("  Overpass returned no matching features.")
            return gpd.GeoDataFrame(geometry=[], crs=UTM43N)
        raise
    print(f"  Raw features returned by Overpass: {len(raw)}")
    return standardise_index(raw).to_crs(UTM43N)


def tag_count(row, cols) -> int:
    return sum(1 for c in cols if isinstance(row.get(c), str) and row.get(c).strip())


def deduplicate(gdf: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, int, int]:
    """Return (deduplicated gdf, n_dropped_same_element, n_dropped_same_name_nearby)."""
    before = len(gdf)
    gdf = gdf.drop_duplicates(subset=["osm_type", "osm_id"]).copy()
    dropped_element = before - len(gdf)

    tag_cols = [c for c in gdf.columns if c not in ("geometry", "osm_type", "osm_id")]
    gdf["_name_norm"] = gdf["name"].map(normalise_name) if "name" in gdf.columns else ""
    gdf["_ntags"] = gdf.apply(lambda r: tag_count(r, tag_cols), axis=1)

    drop_idx: set = set()
    named = gdf[gdf["_name_norm"] != ""]
    for _, group in named.groupby("_name_norm"):
        if len(group) < 2:
            continue
        group = group.sort_values("_ntags", ascending=False)  # keep best-tagged copy
        kept_points: list[Point] = []
        for idx, row in group.iterrows():
            pt = row.geometry
            if any(pt.distance(k) <= DEDUP_DISTANCE_M for k in kept_points):
                drop_idx.add(idx)
            else:
                kept_points.append(pt)

    gdf = gdf.drop(index=list(drop_idx)).drop(columns=["_name_norm", "_ntags"])
    return gdf, dropped_element, len(drop_idx)


def prepare_features(raw_utm: gpd.GeoDataFrame, study_utm) -> gpd.GeoDataFrame:
    if raw_utm.empty:
        return raw_utm

    gdf = raw_utm.copy()
    # Every feature (node, building outline, campus relation) -> one point inside it.
    gdf["geometry"] = gdf.geometry.representative_point()
    gdf = gdf.set_geometry("geometry", crs=UTM43N)

    inside = gdf.geometry.apply(study_utm.covers)
    print(f"  Features whose representative point lies inside the study area: {int(inside.sum())}")
    gdf = gdf[inside].copy()

    for col in ("name", "amenity", "healthcare"):
        if col not in gdf.columns:
            gdf[col] = None

    gdf, d_elem, d_name = deduplicate(gdf)
    print(f"  Duplicates removed: {d_elem} (same OSM element), {d_name} "
          f"(same name within {DEDUP_DISTANCE_M:.0f} m)")

    gdf["category"] = gdf.apply(classify, axis=1)
    keep = ["osm_type", "osm_id", "name", "category", "amenity", "healthcare"]
    keep += [c for c in EXTRA_TAG_COLUMNS if c in gdf.columns]
    out = gdf[keep + ["geometry"]].copy()

    # Clean types so the GeoJSON writer never sees lists/NaN mixtures.
    out["osm_id"] = out["osm_id"].astype("int64")
    for col in keep:
        if col != "osm_id":
            out[col] = out[col].map(lambda v: v if isinstance(v, str) and v.strip() else None)

    out = out.to_crs(WGS84)
    out["lon"] = out.geometry.x.round(7)
    out["lat"] = out.geometry.y.round(7)
    return out.sort_values(["category", "name"], na_position="last").reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def write_geojson(gdf: gpd.GeoDataFrame, path: Path) -> None:
    if path.exists():
        path.unlink()
    gdf.to_file(path, driver="GeoJSON")


def save_study_area(study_utm, source: str, wards_found: list[str], path: Path) -> float:
    area_km2 = round(study_utm.area / 1e6, 4)
    gdf = gpd.GeoDataFrame(
        {
            "name": ["Edappally + Palarivattom + Kaloor (Kochi Municipal Corporation)"],
            "source": [source],
            "wards_found": [", ".join(wards_found) if wards_found else "none"],
            "area_km2": [area_km2],
            "center_lat": [CENTER_LAT],
            "center_lon": [CENTER_LON],
        },
        geometry=[study_utm],
        crs=UTM43N,
    ).to_crs(WGS84)
    write_geojson(gdf, path)
    return area_km2


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Verify Kochi study area and Healthcare feature count.")
    p.add_argument("--force-bbox", action="store_true",
                   help="Skip ward geocoding and use the fallback rectangle.")
    p.add_argument("--half-width-km", type=float, default=2.0,
                   help="Fallback rectangle half-width (east-west), km. Default 2.0")
    p.add_argument("--half-height-km", type=float, default=2.6,
                   help="Fallback rectangle half-height (north-south), km. Default 2.6")
    p.add_argument("--out-dir", type=Path, default=SCRIPT_DIR,
                   help="Output folder. Default: the analysis/ folder")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    configure_osmnx()

    print(f"osmnx {ox.__version__} | geopandas {gpd.__version__} | pandas {pd.__version__}")
    print(f"Study centre: {CENTER_LAT} N, {CENTER_LON} E | metric CRS {UTM43N}\n")

    study_utm, source, wards_found = build_study_area(
        args.force_bbox, args.half_width_km, args.half_height_km
    )
    study_path = args.out_dir / "study_area.geojson"
    area_km2 = save_study_area(study_utm, source, wards_found, study_path)
    print(f"\nStudy area source : {source}")
    print(f"Wards from OSM    : {', '.join(wards_found) if wards_found else 'none'}")
    print(f"Study area        : {area_km2} km2 (computed in {UTM43N})")
    print(f"Saved             : {study_path}\n")

    raw = download_healthcare(study_utm)
    features = prepare_features(raw, study_utm)
    feat_path = args.out_dir / "healthcare_features.geojson"
    if not features.empty:
        write_geojson(features, feat_path)

    count = len(features)
    print("\nHealthcare features by category:")
    if count:
        for cat, n in features["category"].value_counts().sort_index().items():
            print(f"  {cat:<32} {n}")
        named = int(features["name"].notna().sum())
        print(f"  {'(named / unnamed)':<32} {named} / {count - named}")
        print(f"Saved             : {feat_path}")
    else:
        print("  (none)")

    print(f"\nDEDUPLICATED HEALTHCARE FEATURE COUNT = {count}")
    if count >= MIN_FEATURES:
        print(f"PASS: {count} >= {MIN_FEATURES}")
        return 0
    print(f"FAIL: {count} < {MIN_FEATURES}. Re-run with a larger fallback rectangle, e.g.\n"
          f"  python analysis/step0_verify_kochi.py --force-bbox --half-width-km 2.5 --half-height-km 3.0")
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except NetworkUnavailable as err:
        print(f"\nNETWORK ERROR: {err}\n"
              "Check your internet connection / VPN / proxy and re-run. "
              "No fallback was applied because the failure was a network problem, "
              "not missing OSM data.", file=sys.stderr)
        sys.exit(3)
