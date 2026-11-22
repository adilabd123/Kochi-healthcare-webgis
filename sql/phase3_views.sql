-- =============================================================================
-- Phase 3 - Publishing helpers  |  Kochi Healthcare WebGIS
-- Requires the tables written by phase1_etl.py and phase2_network.py.
--
-- Run inside the container (./sql is mounted at /sql):
--   docker exec -i kochi_postgis psql -U webgis -d kochi_health -v ON_ERROR_STOP=1 -f /sql/phase3_views.sql
--
-- Safe to re-run. Creates:
--   1. Spatial (GiST) and attribute (B-tree) indexes for serving and routing
--   2. View  public.v_hospital_access_gaps_4326  (GeoServer layer)
--   3. View  public.v_coverage_summary           (API / reporting, no geometry)
--   4. Function public.building_walk_time(z, x, y, facility_set, speed_kmh)
--      -> Mapbox Vector Tile layer served by pg_tileserv
-- =============================================================================

\pset pager off
\timing on

-- -----------------------------------------------------------------------------
-- 1. Indexes (Phase 1 left the base tables unindexed for the Q2 benchmark)
-- -----------------------------------------------------------------------------
\echo '=== 1. Indexes ==='
CREATE INDEX IF NOT EXISTS study_area_4326_geom_gist   ON study_area_4326  USING GIST (geom);
CREATE INDEX IF NOT EXISTS study_area_32643_geom_gist  ON study_area_32643 USING GIST (geom);
CREATE INDEX IF NOT EXISTS healthcare_4326_geom_gist   ON healthcare_4326  USING GIST (geom);
CREATE INDEX IF NOT EXISTS healthcare_32643_geom_gist  ON healthcare_32643 USING GIST (geom);
CREATE INDEX IF NOT EXISTS buildings_4326_geom_gist    ON buildings_4326   USING GIST (geom);
CREATE INDEX IF NOT EXISTS buildings_32643_geom_gist   ON buildings_32643  USING GIST (geom);
CREATE INDEX IF NOT EXISTS walk_nodes_4326_geom_gist   ON walk_nodes_4326  USING GIST (geom);
CREATE INDEX IF NOT EXISTS walk_nodes_32643_geom_gist  ON walk_nodes_32643 USING GIST (geom);
CREATE INDEX IF NOT EXISTS walk_edges_4326_geom_gist   ON walk_edges_4326  USING GIST (geom);
CREATE INDEX IF NOT EXISTS walk_edges_32643_geom_gist  ON walk_edges_32643 USING GIST (geom);

-- Routing lookups (pgRouting joins on node ids)
CREATE INDEX IF NOT EXISTS walk_nodes_32643_node_id_idx ON walk_nodes_32643 (node_id);
CREATE INDEX IF NOT EXISTS walk_edges_32643_source_idx  ON walk_edges_32643 (source);
CREATE INDEX IF NOT EXISTS walk_edges_32643_target_idx  ON walk_edges_32643 (target);
CREATE INDEX IF NOT EXISTS node_access_32643_node_id_idx ON node_access_32643 (node_id);

-- Attribute filters used by GeoServer CQL, pg_featureserv and the API
CREATE INDEX IF NOT EXISTS facility_access_32643_type_idx ON facility_access_32643 (type);
CREATE INDEX IF NOT EXISTS facility_access_4326_type_idx  ON facility_access_4326 (type);
CREATE INDEX IF NOT EXISTS service_areas_4326_filter_idx  ON service_areas_4326 (facility_set, speed_kmh, minutes);

ANALYZE study_area_4326; ANALYZE study_area_32643;
ANALYZE healthcare_4326; ANALYZE healthcare_32643;
ANALYZE buildings_4326;  ANALYZE buildings_32643;
ANALYZE walk_nodes_4326; ANALYZE walk_nodes_32643;
ANALYZE walk_edges_4326; ANALYZE walk_edges_32643;
ANALYZE node_access_32643; ANALYZE facility_access_32643; ANALYZE facility_access_4326;
ANALYZE service_areas_4326;

-- -----------------------------------------------------------------------------
-- 2. Elderly hospital-access gap: buildings more than 15 minutes from the
--    nearest hospital-tagged facility at 3.6 km/h (i.e. > 900 m network walk).
-- -----------------------------------------------------------------------------
\echo '=== 2. View v_hospital_access_gaps_4326 ==='
CREATE OR REPLACE VIEW public.v_hospital_access_gaps_4326 AS
SELECT
    b.id,
    b.osm_id,
    b.building,
    b.name,
    b.area_m2,
    b.hospital_dist_m,
    b.hospital_t4p8_min,
    b.hospital_t3p6_min,
    b.hospital_fac_id,
    f.name AS nearest_hospital,
    CASE WHEN b.hospital_t4p8_min > 15 THEN 'gap_at_4.8_and_3.6'
         ELSE 'gap_at_3.6_only' END AS gap_class,
    b.geom
FROM building_access_4326 b
LEFT JOIN facility_access_4326 f ON f.id = b.hospital_fac_id
WHERE b.hospital_t3p6_min > 15 OR b.hospital_dist_m IS NULL;

COMMENT ON VIEW public.v_hospital_access_gaps_4326 IS
  'Buildings > 15 min walk (3.6 km/h, 900 m) from the nearest hospital-tagged OSM facility';

-- -----------------------------------------------------------------------------
-- 3. Coverage summary without geometry (one row per set x speed x minutes)
-- -----------------------------------------------------------------------------
\echo '=== 3. View v_coverage_summary ==='
CREATE OR REPLACE VIEW public.v_coverage_summary AS
SELECT
    facility_set,
    speed_kmh,
    minutes,
    distance_m,
    buildings_within,
    pct_buildings,
    area_in_study_km2,
    pct_of_study_area,
    healthcare_points_within,
    pct_healthcare_points,
    area_km2 AS service_area_total_km2
FROM service_areas_4326
ORDER BY facility_set, speed_kmh DESC, minutes;

-- -----------------------------------------------------------------------------
-- 4. pg_tileserv function layer: buildings coloured by walking time
--    URL: http://localhost:7800/public.building_walk_time/{z}/{x}/{y}.pbf
--         ?facility_set=hospital&speed_kmh=3.6
--    Times are recomputed per request from the stored network distances, so
--    any speed between 1 and 8 km/h works, not only the two in Phase 2.
-- -----------------------------------------------------------------------------
\echo '=== 4. Function public.building_walk_time ==='
CREATE OR REPLACE FUNCTION public.building_walk_time(
    z integer,
    x integer,
    y integer,
    facility_set text DEFAULT 'all',
    speed_kmh double precision DEFAULT 4.8)
RETURNS bytea
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $func$
WITH params AS (
    SELECT
        CASE WHEN facility_set IN ('all', 'hospital', 'clinical')
             THEN facility_set ELSE 'all' END                          AS fs,
        GREATEST(1.0, LEAST(8.0, COALESCE(speed_kmh, 4.8))) * 1000.0 / 60.0 AS mpm
),
bounds AS (
    SELECT ST_TileEnvelope(z, x, y) AS geom3857,
           ST_Transform(ST_TileEnvelope(z, x, y), 4326) AS geom4326
),
src AS (
    SELECT
        b.id,
        b.name,
        b.building,
        CASE p.fs WHEN 'hospital' THEN b.hospital_dist_m
                  WHEN 'clinical' THEN b.clinical_dist_m
                  ELSE b.all_dist_m END                                AS dist_m,
        p.mpm,
        p.fs,
        b.geom
    FROM building_access_4326 b, params p, bounds
    WHERE b.geom && bounds.geom4326
),
mvtgeom AS (
    SELECT
        ST_AsMVTGeom(ST_Transform(s.geom, 3857), bounds.geom3857, 4096, 64, true) AS geom,
        s.id,
        s.name,
        s.building,
        s.fs                                                           AS facility_set,
        ROUND(s.dist_m::numeric, 0)                                    AS dist_m,
        ROUND((s.dist_m / s.mpm)::numeric, 1)                          AS walk_min,
        CASE WHEN s.dist_m IS NULL          THEN 'unreachable'
             WHEN s.dist_m / s.mpm <= 5     THEN '0-5 min'
             WHEN s.dist_m / s.mpm <= 10    THEN '5-10 min'
             WHEN s.dist_m / s.mpm <= 15    THEN '10-15 min'
             ELSE '>15 min' END                                        AS band
    FROM src s, bounds
)
SELECT ST_AsMVT(mvtgeom, 'building_walk_time', 4096, 'geom')
FROM mvtgeom
WHERE geom IS NOT NULL;
$func$;

COMMENT ON FUNCTION public.building_walk_time(integer, integer, integer, text, double precision) IS
  'Buildings coloured by network walking time to the nearest facility (all|hospital|clinical) at a chosen speed';

-- -----------------------------------------------------------------------------
-- Checks
-- -----------------------------------------------------------------------------
\echo '=== Checks ==='
SELECT COUNT(*) AS elderly_hospital_gap_buildings,
       COUNT(*) FILTER (WHERE gap_class = 'gap_at_4.8_and_3.6') AS also_gap_at_4p8
FROM public.v_hospital_access_gaps_4326;

SELECT * FROM public.v_coverage_summary;

-- One z15 tile over the study-area centre (10.008 N, 76.302 E -> x 23329, y 15468)
SELECT length(public.building_walk_time(15, 23329, 15468))                 AS tile_bytes_all_4p8,
       length(public.building_walk_time(15, 23329, 15468, 'hospital', 3.6)) AS tile_bytes_hospital_3p6;

SELECT tablename, COUNT(*) AS gist_indexes
FROM pg_indexes
WHERE schemaname = 'public' AND indexdef ILIKE '%USING gist%'
GROUP BY tablename
ORDER BY tablename;
