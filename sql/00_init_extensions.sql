-- Executed automatically by the postgis container the FIRST time its data
-- volume is initialised (mounted into /docker-entrypoint-initdb.d).
-- Safe to re-run manually:  docker compose exec postgis psql -U webgis -d kochi_health -f /sql/00_init_extensions.sql

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pgrouting;

-- Quick confirmation in the container log.
SELECT postgis_full_version() AS postgis_version;
SELECT pgr_version()          AS pgrouting_version;
