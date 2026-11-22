import os
import json
import subprocess
from sqlalchemy import create_engine, text

PG_HOST = os.getenv("POSTGRES_HOST", "127.0.0.1")
PG_PORT = os.getenv("POSTGRES_PORT", "5433")
PG_DB = os.getenv("POSTGRES_DB", "kochi_health")
PG_USER = os.getenv("POSTGRES_USER", "postgres")
PG_PASS = os.getenv("POSTGRES_PASSWORD", "postgres")

engine = create_engine(f"postgresql+psycopg2://{PG_USER}:{PG_PASS}@{PG_HOST}:{PG_PORT}/{PG_DB}")

os.makedirs("pmtiles/geojson", exist_ok=True)
os.makedirs("docs/tiles", exist_ok=True)

queries = {
    "facilities": """
        SELECT jsonb_build_object(
            'type', 'FeatureCollection',
            'features', COALESCE(jsonb_agg(ST_AsGeoJSON(t.*)::jsonb), '[]'::jsonb)
        )
        FROM (
            SELECT osm_id, osm_type, name, type, hospital_dist_m, hospital_t4p8_min, hospital_t3p6_min, all_catch15_4p8, snap_m, geom
            FROM public.facility_access_4326
        ) t;
    """,
    "h3_res8": """
        SELECT jsonb_build_object(
            'type', 'FeatureCollection',
            'features', COALESCE(jsonb_agg(ST_AsGeoJSON(t.*)::jsonb), '[]'::jsonb)
        )
        FROM (
            SELECT h3_index, res, net_dist_m, walk_min_4p8, bld_count, bld_area_m2, served_15min, geom
            FROM public.h3_res8_4326
        ) t;
    """,
    "h3_res9": """
        SELECT jsonb_build_object(
            'type', 'FeatureCollection',
            'features', COALESCE(jsonb_agg(ST_AsGeoJSON(t.*)::jsonb), '[]'::jsonb)
        )
        FROM (
            SELECT h3_index, res, net_dist_m, walk_min_4p8, bld_count, bld_area_m2, served_15min, geom
            FROM public.h3_res9_4326
        ) t;
    """
}

with engine.connect() as conn:
    for name, sql in queries.items():
        fc = conn.execute(text(sql)).scalar()
        out_gj = f"pmtiles/geojson/{name}.geojson"
        with open(out_gj, "w", encoding="utf-8") as f:
            json.dump(fc, f)
        out_pmtiles = f"docs/tiles/{name}.pmtiles"
        cmd = [
            "tippecanoe",
            "-o", out_pmtiles,
            "--force",
            "-Z", "10",
            "-z", "17",
            "--drop-densest-as-needed",
            "-l", name,
            out_gj
        ]
        print("Running:", " ".join(cmd))
        subprocess.run(cmd, check=True)
