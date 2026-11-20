# ANSWERS — Kochi Healthcare WebGIS

Study area: Edappally, Palarivattom and Kaloor wards, Kochi Municipal Corporation, Kerala.
Category: Healthcare (`amenity=hospital|clinic|doctors`, `healthcare=*`). Metric CRS: UTM 43N (`EPSG:32643`).

## Data and environment

| Item | Value |
|---|---|
| Study area | 20.8000 km² in EPSG:32643. This is exactly the 4.0 km × 5.2 km fallback rectangle around 10.008° N, 76.302° E from `step0_verify_kochi.py`, so the boundary approximates the three wards rather than following official ward lines. |
| Healthcare points | 93 (OSM via Overpass, deduplicated; polygons converted to centroids) |
| Buildings | 5,321 footprints (representative point inside study area) |
| Walk network (study area + 2 km) | 11,665 nodes, 27,936 directed edges |
| Database | PostgreSQL 16, PostGIS 3.5.2, pgRouting 3.8.0 (Docker, host port 5433) |
| Raw outputs | `analysis/phase1_sql_run1.txt` (cold cache), `analysis/phase1_sql_run2.txt` (warm cache) |

---

## Q1 — One area, four areas

### Results

| Method | SQL | Result |
|---|---|---|
| a) Geometry in EPSG:4326 | `ST_Area(geom)` | **0.00171574 deg²** |
| b) Geography (WGS84 spheroid) | `ST_Area(geom::geography)` | **20.8062 km²** (reference) |
| c) UTM 43N | `ST_Area(ST_Transform(geom, 32643))` | **20.8000 km²** (−0.0296 % vs geography) |
| d) Web Mercator | `ST_Area(ST_Transform(geom, 3857))` | **21.5900 km²** (+3.7672 % vs geography) |

| Ratio check (centroid latitude 10.00800° N) | Value |
|---|---|
| 3857 area ÷ UTM area | **1.037979** |
| Textbook prediction 1 / cos²(lat) | **1.031142** |
| Gap between the two | **+0.6630 %** |
| 3857 area ÷ geography area | **1.037672** |
| Ellipsoidal prediction (1 − e²sin²φ)² / ((1 − e²)cos²φ) | **1.037672** |

### Interpretation

**(a) Square degrees are not an area.** A degree is an angle, so the size of one "square degree" on the ground depends on where you are. At 10° N one degree of latitude is 110.61 km and one degree of longitude is 109.64 km, so 1 deg² ≈ 12,126.7 km² *here*. That makes 0.00171574 × 12,126.7 ≈ 20.806 km², which agrees with the geography result. At 60° N the same square degree would be about half that size. Never report `ST_Area` on `geometry(4326)` as an area.

**(b) Geography is the reference.** PostGIS computes it on the WGS84 ellipsoid, with no projection involved.

**(c) UTM is almost exact.** Our area lies at 76.3° E, only 1.3° from the zone's central meridian (75° E). UTM's scale factor there is about 0.99985, so areas shrink by about 0.03 % (k² ≈ 0.999704). That is why UTM shows −0.0296 %. For a 20 km² study area the error is about 0.006 km², which is negligible.

**(d) Web Mercator inflates the area by 3.77 %, not the 3.11 % that 1/cos² predicts.** The usual explanation, that Mercator scale grows with 1/cos(latitude), is exact only on a sphere. EPSG:3857 takes latitudes measured on the WGS84 *ellipsoid* and pushes them through the *sphere* formulas with radius a = 6,378,137 m:

- East–west, the scale is a / (N·cos φ), where N is the ellipsoid's east–west radius of curvature. At 10° N, a/N = 0.99990, which is almost no extra effect.
- North–south, the scale is a / (M·cos φ), where M is the north–south (meridional) radius of curvature. Near the equator M is about 0.64 % smaller than a (a/M = 1.00643). Every metre walked north is therefore drawn about 0.64 % longer than pure 1/cos φ would draw it.
- Multiplying the two gives an area factor a² / (M·N·cos²φ) = (1 − e²sin²φ)² / ((1 − e²)cos²φ). At our centroid that is sec²φ × 1.006332 = 1.031142 × 1.006332 = **1.037672**, matching our measured 3857/geography ratio to all six decimals.

The 0.663 % gap in the 3857/UTM ratio therefore breaks down into two parts:

```
3857 / UTM = (3857 / geography) / (UTM / geography)
           = 1.037672 / 0.999704
           = 1.037979                      (measured: 1.037979)

1.037672 = 1.031142 (sphere sec²φ) × 1.006332 (ellipsoid term, ~0.63 %)
```

So about 0.63 % of the gap comes from Web Mercator not being conformal on the ellipsoid, and about 0.03 % from UTM's own scale factor. Because the study area is small (5.2 km north–south), the centroid latitude represents it well, and the formula matches without any area-weighting.

**Conclusion.** Use EPSG:3857 only to *display* tiles. Measure areas, distances and buffers in EPSG:32643, or with `geography`. Even near the equator, measuring in 3857 would overstate our study area by 0.78 km² and every catchment area by 3.8 %.

---

## Q2 — Indexes and query plans

Query: number of buildings within 500 m of each of the 93 healthcare points, on EPSG:32643 geometry (metres).

### Results

| Run | Predicate | Index state | Plan for `buildings_32643` | Cold (run 1) | Warm (run 2) |
|---|---|---|---|---|---|
| A | `ST_DWithin(h.geom, b.geom, 500)` | no GiST | **Seq Scan** (JIT used) | 6,588.086 ms | 509.386 ms |
| B | `ST_DWithin(h.geom, b.geom, 500)` | GiST on both tables | **Index Scan using `buildings_32643_geom_gist`**, index condition `geom && st_expand(h.geom, 500)` | 29.182 ms | 39.122 ms |
| C | `ST_Distance(h.geom, b.geom) < 500` | GiST present | **Seq Scan** (JIT used) | 432.218 ms | 368.529 ms |

Correctness check: both predicates return **23,076** building–facility pairs, an average of 248.1 buildings within 500 m per facility. The facility with the most buildings within 500 m is **Rite Cure Health Services (551)**.

### Interpretation

**Run A, no index: a brute-force comparison of every pair.** Without an index the only way to answer is a nested loop: for each of the 93 points, read all 5,321 buildings and test the distance. That is 93 × 5,321 = **494,853** exact geometry tests, of which only 23,076 (4.7 %) succeed. The planner's cost estimate for this plan is high enough to cross PostgreSQL's JIT threshold, so it also compiled the expressions to machine code. The cold run (6.6 s) paid for reading the table from disk plus JIT compilation. The warm run (0.51 s) still did all 494,853 tests.

**Run B, GiST index: a cheap filter first, then the exact test on a few candidates.** `ST_DWithin` is index-aware. PostGIS rewrites it into two steps, as the plan shows:

1. `b.geom && ST_Expand(h.geom, 500)`: the GiST index (an R-tree over each building's bounding box) returns only buildings whose box overlaps a 1 km × 1 km square around the facility.
2. The exact distance test then runs on just those candidates.

This works like a library catalogue: look up the right shelf first, then read only the books on it. The warm time fell from 509.4 ms to 39.1 ms (**13.0× faster**). Cold, 6,588 ms became 29.2 ms (**about 226×**), because the index also avoids reading the whole table from disk and the plan is cheap enough that JIT no longer kicks in. Run B was slightly *faster* cold (29.2 ms) than warm (39.1 ms). At tens of milliseconds, normal run-to-run variation on a laptop running Docker is larger than that difference, so B should be reported as "about 30–40 ms". Taking the median of several repeats would be the more rigorous measure.

**Run C, `ST_Distance < 500` with the index present: the index goes unused.** `ST_Distance` only returns a number. It carries no bounding-box hint, so the planner cannot turn `distance < 500` into an `&&` index condition. Having the index changes nothing: the plan returns to a Seq Scan and computes all 494,853 distances (368.5 ms warm, **9.4× slower than B**). C ran somewhat faster than A (368.5 vs 509.4 ms warm), but both are full scans at the same order of magnitude, and the gap likely reflects JIT and caching overheads rather than any plan difference. The `JIT:` timing block at the end of each plan in `phase1_sql_run2.txt` shows how much of each run was compilation.

**Lessons**
- Write proximity queries as `ST_DWithin(a, b, r)`, never `ST_Distance(a, b) < r`. Both return the same 23,076 pairs, but only `ST_DWithin` can use an index.
- An index only helps if the predicate can use it. Run C shows that having the index is not enough.
- Run `ANALYZE` after bulk loads and after creating indexes so the planner has up-to-date row estimates.

---

## Q3 — Data quality audit

### Q3a — Missing names

**5 of 93 healthcare points (5.38 %) have no `name` tag.**

| Type | Without name | Total | % |
|---|---|---|---|
| hospital | 1 | 32 | 3.1 % |
| pharmacy | 2 | 28 | 7.1 % |
| clinic | 1 | 16 | 6.3 % |
| dentist | 1 | 3 | 33.3 % |
| all other types | 0 | 14 | 0 % |
| **Total** | **5** | **93** | **5.38 %** |

Unnamed points still count in the accessibility analysis, but they cannot be labelled, searched or matched against official lists. A name-based geocoder such as Nominatim would never return them.

### Q3b — Points closer than 25 m

**6 pairs of healthcare points lie within 25 m of each other (EPSG:32643).**

| # | Point A | Point B | Distance | Assessment |
|---|---|---|---|---|
| 1 | Vaidyaratnam | Kerala Medicals | 10.1 m | Different businesses (Ayurveda outlet and a medical shop), probably neighbouring shops. Co-location, not a duplicate. |
| 2 | Dr Sunny's Dental Clinic | Sevana Medicines | 18.0 m | Clinic beside a pharmacy. Co-location. |
| 3 | Park Dental Clinic | Bio-Tech Diagnostic Centre | 21.9 m | Clinic beside a lab. Co-location. |
| 4 | Babu Medicals | Devaindia Generic Pharmacy | 21.9 m | Two pharmacies side by side. Plausible, worth a check. |
| 5 | Primary Health Centre, Thammanam | Urban primary Health Centre, Thammanam | 22.7 m | **Probable duplicate.** Both names refer to a public health centre in Thammanam, and the two points are 22.7 m apart. Likely the same facility mapped twice under two names. |
| 6 | Lisie Cancer Center | Medical Emporium English Medicines | 23.0 m | Specialist hospital facility with a pharmacy beside it. Co-location. |

Five of the six pairs are a clinic or hospital next to a pharmacy, lab or second pharmacy. That is the normal urban pattern of medical shops clustering around a care facility, so the two points are genuinely different businesses. Pair 5 is the exception. Its names differ, so the ETL's dedup rule (same normalised name within 50 m) could not merge them. If street imagery or the Kerala health department's facility list confirms a single centre, it should be merged in OSM or excluded here. Counting it twice inflates the hospital total by one and adds a duplicate catchment in Phase 2.

### Other quality observations
- **Category mixing.** The `type` column combines `amenity` values (hospital, clinic) with `healthcare` values (pharmacy, laboratory, alternative, centre). Pharmacies (28) are 30 % of the dataset because `healthcare=*` includes them. They are places to buy medicine, not to get treated. Phase 2 therefore reports a *clinical* subset (excluding pharmacy and laboratory) alongside *all* healthcare.
- **Vague tags.** Two features are only tagged `healthcare=yes`, and two are `healthcare=centre`. Neither says what service is offered.
- **"Hospital" may be over-tagged.** 32 points tagged hospital in 20.8 km² is a lot. The sample already shows a primary health centre carrying the hospital tag (Q3c), so some of these are likely clinics or health centres tagged `amenity=hospital`. Hospital-only results in Phase 2 should be read with that in mind.

### Q3c — Five-row verification sample

| # | Name | Type | Lat | Lon | Verified? | Source used | Notes |
|---|---|---|---|---|---|---|---|
| 1 | Janamithra Medicine and Healthcare | pharmacy | 9.996825 | 76.308011 | [PASTE REAL OUTPUT HERE] | [PASTE REAL OUTPUT HERE] | [PASTE REAL OUTPUT HERE] |
| 2 | raha ayurveda | clinic | 10.026906 | 76.306629 | [PASTE REAL OUTPUT HERE] | [PASTE REAL OUTPUT HERE] | [PASTE REAL OUTPUT HERE] |
| 3 | New Care N Cure Medicals | pharmacy | 10.029319 | 76.305537 | [PASTE REAL OUTPUT HERE] | [PASTE REAL OUTPUT HERE] | [PASTE REAL OUTPUT HERE] |
| 4 | C.K.K.M Pharmacy | pharmacy | 10.001948 | 76.306538 | [PASTE REAL OUTPUT HERE] | [PASTE REAL OUTPUT HERE] | [PASTE REAL OUTPUT HERE] |
| 5 | Urban primary Health Centre, Thammanam | hospital | 9.989758 | 76.306587 | [PASTE REAL OUTPUT HERE] | [PASTE REAL OUTPUT HERE] | [PASTE REAL OUTPUT HERE] |

Check each coordinate in Google Street View or satellite imagery and, where possible, against an official or business listing. For row 5, also check whether the facility is really a hospital and whether "Primary Health Centre, Thammanam" is the same place (see Q3b, pair 5).

### Q3d — OSM Overpass extract vs earlier Nearby-Services (Nominatim) workflow

| Type | This project (Overpass) | Nearby-Services (Nominatim) |
|---|---|---|
| hospital | 32 | [PASTE REAL OUTPUT HERE] |
| pharmacy | 28 | [PASTE REAL OUTPUT HERE] |
| clinic | 16 | [PASTE REAL OUTPUT HERE] |
| alternative | 4 | [PASTE REAL OUTPUT HERE] |
| laboratory | 4 | [PASTE REAL OUTPUT HERE] |
| dentist | 3 | [PASTE REAL OUTPUT HERE] |
| centre | 2 | [PASTE REAL OUTPUT HERE] |
| yes | 2 | [PASTE REAL OUTPUT HERE] |
| hospice | 1 | [PASTE REAL OUTPUT HERE] |
| psychotherapist | 1 | [PASTE REAL OUTPUT HERE] |
| **Total** | **93** | [PASTE REAL OUTPUT HERE] |

Why the two counts should be expected to differ:

- **Different tools for different jobs.** Overpass *queries the OSM database by tag*: it returns every object inside the polygon carrying `amenity=hospital|clinic|doctors` or any `healthcare=*` tag, named or not. Nominatim is a *geocoder*: it ranks text matches (e.g. "hospital near Kaloor") by relevance and returns a limited number of results per request (its documented maximum `limit` is 40), so it is not designed to enumerate everything in an area.
- **Unnamed features.** The 5 unnamed points can appear in Overpass results but are hard or impossible to find by name search in Nominatim.
- **Tag coverage.** A search on hospital or clinic keywords would not return the 28 pharmacies, 4 laboratories or the `healthcare=alternative/centre/yes` features that `healthcare=*` captures.
- **Geometry.** Overpass returns hospital campuses as polygons, which this project converts to centroids and deduplicates. Nominatim returns one point per matched object.

For an exhaustive inventory, Overpass with explicit tags is the right source. Nominatim stays useful for what Nearby-Services used it for: turning a user's typed address into a coordinate.

---

*Study note S1 was corrected in this phase: Web Mercator area inflation at Kochi is 3.77 % against the WGS84 ellipsoid, not the 3.1 % that sec²φ alone predicts.*
