-- =============================================================================
-- Phase 1 - Q1 to Q3  |  Kochi Healthcare WebGIS
-- Tables come from analysis/phase1_etl.py (study_area_*, healthcare_*, buildings_*).
--
-- Run inside the container (the ./sql folder is mounted at /sql):
--   docker exec -i kochi_postgis psql -U webgis -d kochi_health -v ON_ERROR_STOP=1 -f /sql/phase1_q1_q3.sql
--
-- The file is safe to re-run: Q2 drops its two GiST indexes first so the
-- "before index" plan is always measured without them.
-- =============================================================================

\pset pager off
\pset null '(null)'
\timing on

\echo
\echo '=== Environment ==='
SELECT version() AS postgres_version, postgis_full_version() AS postgis;


-- =============================================================================
-- Q1  One area, four areas
-- -----------------------------------------------------------------------------
-- The same polygon measured four ways:
--   a) ST_Area on geometry(4326)   -> square DEGREES (not a real area)
--   b) ST_Area on geography        -> m2 on the WGS84 spheroid (reference value)
--   c) ST_Area in EPSG:32643 (UTM) -> m2 in a low-distortion metric projection
--   d) ST_Area in EPSG:3857        -> m2 in Web Mercator (inflated by sec^2(lat))
-- Then compares the 3857/UTM ratio with 1 / cos^2(latitude of centroid).
--
-- Why the two will NOT match exactly (expect a gap of roughly 0.6-0.7 % here):
--   1. Web Mercator applies SPHERE formulas (radius a = 6378137 m) to
--      ELLIPSOID latitudes. Near the equator the ellipsoid's north-south
--      radius of curvature M is ~0.6 % smaller than a, so 3857 stretches
--      north-south distances by ~0.6 % beyond sec(lat). Its true area factor
--      on WGS84 is  (1 - e2*sin2(lat))^2 / ((1 - e2) * cos2(lat)),  e2 = 0.00669438
--      which is shown below as expected_ellipsoidal (compare with 3857/geography).
--   2. UTM is not perfectly true-scale either: at 76.3 E (1.3 deg from the
--      central meridian 75 E) its scale is ~0.99985, so UTM areas are ~0.03 %
--      smaller than the geography area.
-- =============================================================================
\echo
\echo '=== Q1: one area, four areas ==='

WITH a AS (
    SELECT geom FROM study_area_4326 LIMIT 1
),
m AS (
    SELECT
        ST_Area(geom)                                   AS area_4326_deg2,
        ST_Area(geom::geography)                        AS area_geog_m2,
        ST_Area(ST_Transform(geom, 32643))              AS area_utm_m2,
        ST_Area(ST_Transform(geom, 3857))               AS area_3857_m2,
        ST_Y(ST_Centroid(geom))                         AS centroid_lat
    FROM a
)
SELECT
    ROUND(area_4326_deg2::numeric, 8)                               AS "a) 4326 area (deg2)",
    ROUND((area_geog_m2 / 1e6)::numeric, 4)                         AS "b) geography (km2)",
    ROUND((area_utm_m2  / 1e6)::numeric, 4)                         AS "c) UTM 32643 (km2)",
    ROUND((area_3857_m2 / 1e6)::numeric, 4)                         AS "d) 3857 (km2)",
    ROUND(centroid_lat::numeric, 5)                                 AS centroid_lat,
    ROUND((area_3857_m2 / area_utm_m2)::numeric, 6)                 AS ratio_3857_over_utm,
    ROUND((1 / POWER(COS(RADIANS(centroid_lat)), 2))::numeric, 6)   AS expected_sec2_lat,
    ROUND((100 * ((area_3857_m2 / area_utm_m2)
                  - 1 / POWER(COS(RADIANS(centroid_lat)), 2))
                / (1 / POWER(COS(RADIANS(centroid_lat)), 2)))::numeric, 4)
                                                                    AS ratio_vs_expected_pct,
    ROUND((area_3857_m2 / area_geog_m2)::numeric, 6)                AS ratio_3857_over_geog,
    ROUND((POWER(1 - 0.00669437999014 * POWER(SIN(RADIANS(centroid_lat)), 2), 2)
           / ((1 - 0.00669437999014) * POWER(COS(RADIANS(centroid_lat)), 2)))::numeric, 6)
                                                                    AS expected_ellipsoidal,
    ROUND((100 * (area_utm_m2  - area_geog_m2) / area_geog_m2)::numeric, 4) AS utm_vs_geog_pct,
    ROUND((100 * (area_3857_m2 - area_geog_m2) / area_geog_m2)::numeric, 4) AS m3857_vs_geog_pct
FROM m;

-- Consistency check: the stored UTM table should give the same area as (c).
SELECT ROUND((ST_Area(geom) / 1e6)::numeric, 4) AS stored_study_area_32643_km2
FROM study_area_32643;


-- =============================================================================
-- Q2  Indexes and query plans
-- -----------------------------------------------------------------------------
-- Question: how many buildings lie within 500 m of each healthcare point?
-- Three runs, all on EPSG:32643 geometry (units = metres):
--   Run A  ST_DWithin, NO GiST index           -> expect Seq Scan on buildings
--   Run B  ST_DWithin, WITH GiST indexes       -> expect Index Scan using ..._gist
--   Run C  ST_Distance < 500, WITH GiST index  -> expect Seq Scan again
--          (ST_Distance has no index-aware bounding-box step, so the planner
--           cannot use the index and must compute every distance)
-- Execution times include caching effects; re-run the file to see warm-cache
-- numbers, and report the "Execution Time" line of each plan.
-- =============================================================================
\echo
\echo '=== Q2: preparing (dropping Q2 indexes if they exist, refreshing stats) ==='
DROP INDEX IF EXISTS buildings_32643_geom_gist;
DROP INDEX IF EXISTS healthcare_32643_geom_gist;
ANALYZE buildings_32643;
ANALYZE healthcare_32643;

SELECT
    (SELECT COUNT(*) FROM healthcare_32643) AS healthcare_points,
    (SELECT COUNT(*) FROM buildings_32643)  AS buildings,
    (SELECT COUNT(*) FROM pg_indexes
      WHERE tablename IN ('buildings_32643', 'healthcare_32643')
        AND indexdef ILIKE '%USING gist%')  AS gist_indexes_present;

\echo
\echo '--- Q2 Run A: ST_DWithin WITHOUT GiST index ---'
EXPLAIN (ANALYZE, BUFFERS)
SELECT h.id, h.name, COUNT(b.id) AS buildings_500m
FROM healthcare_32643 h
LEFT JOIN buildings_32643 b
       ON ST_DWithin(h.geom, b.geom, 500)
GROUP BY h.id, h.name;

\echo
\echo '--- Q2: creating GiST indexes ---'
CREATE INDEX buildings_32643_geom_gist  ON buildings_32643  USING GIST (geom);
CREATE INDEX healthcare_32643_geom_gist ON healthcare_32643 USING GIST (geom);
ANALYZE buildings_32643;
ANALYZE healthcare_32643;

SELECT indexname, tablename, indexdef
FROM pg_indexes
WHERE indexname IN ('buildings_32643_geom_gist', 'healthcare_32643_geom_gist');

\echo
\echo '--- Q2 Run B: ST_DWithin WITH GiST index ---'
EXPLAIN (ANALYZE, BUFFERS)
SELECT h.id, h.name, COUNT(b.id) AS buildings_500m
FROM healthcare_32643 h
LEFT JOIN buildings_32643 b
       ON ST_DWithin(h.geom, b.geom, 500)
GROUP BY h.id, h.name;

\echo
\echo '--- Q2 Run C: ST_Distance < 500 WITH GiST index present ---'
EXPLAIN (ANALYZE, BUFFERS)
SELECT h.id, h.name, COUNT(b.id) AS buildings_500m
FROM healthcare_32643 h
LEFT JOIN buildings_32643 b
       ON ST_Distance(h.geom, b.geom) < 500
GROUP BY h.id, h.name;

\echo
\echo '--- Q2 result: top 10 healthcare points by buildings within 500 m ---'
SELECT h.id, h.name, h.type, COUNT(b.id) AS buildings_500m
FROM healthcare_32643 h
LEFT JOIN buildings_32643 b
       ON ST_DWithin(h.geom, b.geom, 500)
GROUP BY h.id, h.name, h.type
ORDER BY buildings_500m DESC, h.id
LIMIT 10;

\echo
\echo '--- Q2 check: both predicates must return identical totals ---'
SELECT
    (SELECT COUNT(*) FROM healthcare_32643 h
       JOIN buildings_32643 b ON ST_DWithin(h.geom, b.geom, 500))   AS pairs_st_dwithin,
    (SELECT COUNT(*) FROM healthcare_32643 h
       JOIN buildings_32643 b ON ST_Distance(h.geom, b.geom) < 500) AS pairs_st_distance;
-- Note: ST_DWithin is "distance <= 500" and ST_Distance < 500 is strict, so a
-- building at exactly 500.000 m would differ; in practice the totals match.


-- =============================================================================
-- Q3  Data quality audit
-- =============================================================================
\echo
\echo '=== Q3a: healthcare features with no name ==='
SELECT
    COUNT(*)                                                          AS total_features,
    COUNT(*) FILTER (WHERE name IS NULL OR btrim(name) = '')          AS without_name,
    ROUND(100.0 * COUNT(*) FILTER (WHERE name IS NULL OR btrim(name) = '')
          / NULLIF(COUNT(*), 0), 2)                                   AS pct_without_name
FROM healthcare_32643;

\echo
\echo '=== Q3a (by type) ==='
SELECT
    type,
    COUNT(*)                                                          AS features,
    COUNT(*) FILTER (WHERE name IS NULL OR btrim(name) = '')          AS without_name,
    ROUND(100.0 * COUNT(*) FILTER (WHERE name IS NULL OR btrim(name) = '')
          / COUNT(*), 1)                                              AS pct_without_name
FROM healthcare_32643
GROUP BY type
ORDER BY features DESC, type;

\echo
\echo '=== Q3b: pairs of healthcare points within 25 m (EPSG:32643) ==='
SELECT COUNT(*) AS pairs_within_25m
FROM healthcare_32643 a
JOIN healthcare_32643 b
  ON a.id < b.id
 AND ST_DWithin(a.geom, b.geom, 25);

\echo
\echo '--- Q3b detail (up to 20 closest pairs) ---'
SELECT
    a.id AS id_a, a.name AS name_a, a.type AS type_a,
    b.id AS id_b, b.name AS name_b, b.type AS type_b,
    ROUND(ST_Distance(a.geom, b.geom)::numeric, 1) AS distance_m
FROM healthcare_32643 a
JOIN healthcare_32643 b
  ON a.id < b.id
 AND ST_DWithin(a.geom, b.geom, 25)
ORDER BY distance_m, a.id
LIMIT 20;

\echo
\echo '=== Q3c: 5 sample healthcare rows for manual verification ==='
-- Deterministic sample (same 5 rows every run), named features first.
-- Open each maps_url / street imagery and confirm the facility exists here.
SELECT
    id,
    name,
    type,
    ROUND(ST_Y(geom)::numeric, 6) AS lat,
    ROUND(ST_X(geom)::numeric, 6) AS lon,
    'https://www.google.com/maps?q=' || ROUND(ST_Y(geom)::numeric, 6)
        || ',' || ROUND(ST_X(geom)::numeric, 6) AS maps_url
FROM healthcare_4326
ORDER BY (name IS NULL), md5(COALESCE(osm_type, '') || COALESCE(osm_id::text, id::text))
LIMIT 5;

\echo
\echo '=== Q3d: totals to compare with the earlier nearby-services (Nominatim) count ==='
SELECT type, COUNT(*) AS osm_overpass_count
FROM healthcare_4326
GROUP BY ROLLUP (type)
ORDER BY (type IS NULL), COUNT(*) DESC, type;
