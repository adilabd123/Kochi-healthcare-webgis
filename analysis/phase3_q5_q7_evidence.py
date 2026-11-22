import json, os, urllib.parse, urllib.request, xml.etree.ElementTree as ET
import geopandas as gpd, h3, networkx as nx, numpy as np, osmnx as ox, pandas as pd
import matplotlib.pyplot as plt
from dotenv import load_dotenv
from shapely.geometry import Point, Polygon
from sqlalchemy import create_engine, text

load_dotenv()
PG_HOST = os.getenv("POSTGRES_HOST", "127.0.0.1")
PG_PORT = os.getenv("POSTGRES_PORT", "15432")
PG_DB = os.getenv("POSTGRES_DB", "kochi_gis")
PG_USER = os.getenv("POSTGRES_USER", "gisuser")
PG_PASS = os.getenv("POSTGRES_PASSWORD", "gispass")
engine = create_engine(f"postgresql+psycopg2://{PG_USER}:{PG_PASS}@{PG_HOST}:{PG_PORT}/{PG_DB}")

# --- Q5: WFS 2.0 GML 3.2 vs GeoJSON ---
base_wfs = "http://localhost:8088/geoserver/kochi/wfs"
common_params = {
    "service": "WFS",
    "version": "2.0.0",
    "request": "GetFeature",
    "typeNames": "kochi:healthcare",
    "srsName": "urn:ogc:def:crs:EPSG::4326",
    "CQL_FILTER": "name IS NOT NULL AND name <> ''",
}
url_gml = f"{base_wfs}?{urllib.parse.urlencode({**common_params, 'outputFormat': 'application/gml+xml; version=3.2'})}"
url_json = f"{base_wfs}?{urllib.parse.urlencode({**common_params, 'outputFormat': 'application/json'})}"

gml_bytes = urllib.request.urlopen(url_gml).read()
json_bytes = urllib.request.urlopen(url_json).read()
root = ET.fromstring(gml_bytes)
num_matched = root.attrib.get("numberMatched")
pos_el = root.find(".//{http://www.opengis.net/gml/3.2}pos")
gml_first_pos = pos_el.text.strip() if pos_el is not None else "N/A"
gj = json.loads(json_bytes.decode("utf-8"))
json_first_coords = gj["features"][0]["geometry"]["coordinates"]

print("=== Q5: WFS 2.0 NAMED SERVICES ===")
print("URL (GeoJSON):", url_json)
print("URL (GML 3.2):", url_gml)
print(f"numberMatched: {num_matched}")
print(f"GML 3.2 size: {len(gml_bytes)/1024:.2f} KB ({len(gml_bytes)} bytes) | first <gml:pos> (lat lon): {gml_first_pos}")
print(f"GeoJSON size: {len(json_bytes)/1024:.2f} KB ({len(json_bytes)} bytes) | first coordinates [lon, lat]: {json_first_coords}")

# --- Load PostGIS layers & OSMnx graph ---
area_32643 = gpd.read_postgis("SELECT * FROM study_area_32643", engine, geom_col="geom")
area_4326 = area_32643.to_crs(4326)
fac_32643 = gpd.read_postgis("SELECT * FROM healthcare_32643", engine, geom_col="geom")
bld_32643 = gpd.read_postgis("SELECT * FROM building_access_32643", engine, geom_col="geom")
bld_32643["footprint_m2"] = bld_32643.geometry.area

G = ox.load_graphml("analysis/kochi_walk_2km.graphml")
G_proj = ox.project_graph(G, to_crs="EPSG:32643")
fac_nodes = ox.nearest_nodes(G_proj, fac_32643.geometry.x.values, fac_32643.geometry.y.values)
fac_32643["node"] = fac_nodes
node_to_fac = {}
for idx, row in fac_32643.iterrows():
    node_to_fac.setdefault(row["node"], []).append(idx)

# Multi-source Dijkstra from all facility nodes
dist_all, paths_all = nx.multi_source_dijkstra(G_proj, set(fac_nodes), weight="length")

# --- Q6: 30 random points straight-line vs walking network ---
rng = np.random.default_rng(42)
minx, miny, maxx, maxy = area_32643.total_bounds
poly = area_32643.geometry.union_all()
pts = []
while len(pts) < 30:
    rx, ry = rng.uniform(minx, maxx), rng.uniform(miny, maxy)
    p = Point(rx, ry)
    if poly.contains(p):
        pts.append(p)

q6_rows = []
for i, p in enumerate(pts, 1):
    dists_sl = fac_32643.geometry.distance(p)
    sl_idx = int(dists_sl.idxmin())
    sl_dist = float(dists_sl.loc[sl_idx])
    sl_name = fac_32643.loc[sl_idx, "name"] or "(unnamed)"

    p_node = ox.nearest_nodes(G_proj, p.x, p.y)
    snap_m = float(p.distance(Point(G_proj.nodes[p_node]["x"], G_proj.nodes[p_node]["y"])))
    net_graph_m = float(dist_all.get(p_node, np.nan))
    path_nodes = paths_all.get(p_node, [])
    src_node = path_nodes[0] if path_nodes else None
    net_idx = node_to_fac[src_node][0] if src_node in node_to_fac else sl_idx
    net_snap_fac_m = float(fac_32643.geometry.loc[net_idx].distance(Point(G_proj.nodes[src_node]["x"], G_proj.nodes[src_node]["y"]))) if src_node else 0.0
    net_dist = snap_m + net_graph_m + net_snap_fac_m
    net_name = fac_32643.loc[net_idx, "name"] or "(unnamed)"
    ratio = net_dist / sl_dist if sl_dist > 0 else np.nan
    q6_rows.append({
        "pt_id": i, "x": p.x, "y": p.y, "sl_idx": sl_idx, "sl_name": sl_name, "sl_m": sl_dist,
        "net_idx": net_idx, "net_name": net_name, "net_m": net_dist, "ratio": ratio,
        "changed": sl_idx != net_idx, "path_nodes": path_nodes
    })

q6_df = pd.DataFrame(q6_rows)
worst = q6_df.loc[q6_df["ratio"].idxmax()]
worst_4326 = gpd.GeoSeries([Point(worst["x"], worst["y"])], crs=32643).to_crs(4326).iloc[0]

print("\n=== Q6: 30 RANDOM POINTS (STRAIGHT-LINE VS NETWORK) ===")
print(f"Different nearest facility count: {int(q6_df['changed'].sum())} / 30 ({q6_df['changed'].mean()*100:.1f}%)")
print(f"Median network/straight ratio: {q6_df['ratio'].median():.3f}")
print(f"Max network/straight ratio: {worst['ratio']:.3f} (Point #{int(worst['pt_id'])} at {worst_4326.y:.6f}N, {worst_4326.x:.6f}E)")
print(f"  Straight-line nearest: {worst['sl_name']} ({worst['sl_m']:.1f} m)")
print(f"  Network nearest:       {worst['net_name']} ({worst['net_m']:.1f} m)")

# Plot worst case for Q6
os.makedirs("analysis/phase3_evidence", exist_ok=True)
fig, ax = plt.subplots(figsize=(7, 7))
edges_gdf = ox.graph_to_gdfs(G_proj, nodes=False)
buf = Point(worst["x"], worst["y"]).buffer(1200)
edges_gdf[edges_gdf.intersects(buf)].plot(ax=ax, color="#cbd5e1", linewidth=0.8)
area_32643.boundary.plot(ax=ax, color="#0f172a", linewidth=1.2)
if worst["path_nodes"]:
    route_pts = [Point(G_proj.nodes[n]["x"], G_proj.nodes[n]["y"]) for n in worst["path_nodes"]]
    gpd.GeoSeries([Polygon()]).plot(ax=ax)
    ax.plot([pt.x for pt in route_pts], [pt.y for pt in route_pts], color="#dc2626", linewidth=2.5, label=f"Walk route ({worst['net_m']:.0f} m)")
sl_pt = fac_32643.geometry.loc[int(worst["sl_idx"])]
net_pt = fac_32643.geometry.loc[int(worst["net_idx"])]
ax.plot([worst["x"], sl_pt.x], [worst["y"], sl_pt.y], "--", color="#2563eb", linewidth=2, label=f"Straight line ({worst['sl_m']:.0f} m)")
ax.scatter([worst["x"]], [worst["y"]], color="#111827", s=70, zorder=5, label=f"Random Pt #{int(worst['pt_id'])}")
ax.scatter([sl_pt.x], [sl_pt.y], color="#2563eb", s=80, zorder=5, label=f"SL: {worst['sl_name']}")
ax.scatter([net_pt.x], [net_pt.y], color="#dc2626", s=80, zorder=5, label=f"Net: {worst['net_name']}")
ax.set_xlim(worst["x"] - 950, worst["x"] + 950)
ax.set_ylim(worst["y"] - 950, worst["y"] + 950)
ax.set_title(f"Q6 Worst Detour Ratio: {worst['ratio']:.2f}x (Point #{int(worst['pt_id'])})")
ax.legend(loc="lower left", fontsize=8)
plt.tight_layout()
plt.savefig("analysis/phase3_evidence/q6_worst_barrier.png", dpi=160)
plt.close()

# --- Q7: H3 Res 8 & 9 + MAUP ---
poly_4326 = area_4326.geometry.union_all()
poly_buf_4326 = area_32643.buffer(600).to_crs(4326).geometry.union_all()
ext = poly_buf_4326.exterior.coords if hasattr(poly_buf_4326, "exterior") else list(poly_buf_4326.geoms)[0].exterior.coords
latlng_poly = h3.LatLngPoly([(lat, lon) for lon, lat in ext])

# Straight-line 1200m buffer around all healthcare facilities
buf1200_union = fac_32643.buffer(1200).union_all()
bld_32643["in_buf_1200"] = bld_32643.geometry.centroid.within(buf1200_union)
bld_32643["in_net_1200"] = bld_32643["all_dist_m"] <= 1200.0
tot_area = bld_32643["footprint_m2"].sum()
net_beyond_pct = (1.0 - bld_32643.loc[bld_32643["in_net_1200"], "footprint_m2"].sum() / tot_area) * 100
buf_beyond_pct = (1.0 - bld_32643.loc[bld_32643["in_buf_1200"], "footprint_m2"].sum() / tot_area) * 100

print("\n=== Q7: COVERAGE & MAUP (15 MIN = 1200 M @ 4.8 KM/H, BUILDING AREA PROXY) ===")
print(f"Building-footprint proxy beyond 1200 m (Network): {net_beyond_pct:.2f}%")
print(f"Building-footprint proxy beyond 1200 m (Buffer):  {buf_beyond_pct:.2f}%")

for res in (8, 9):
    cells = h3.polygon_to_cells(latlng_poly, res)
    rows = []
    for c in cells:
        lat, lon = h3.cell_to_latlng(c)
        boundary = h3.cell_to_boundary(c)
        cell_poly_4326 = Polygon([(lng, lt) for lt, lng in boundary])
        if not cell_poly_4326.intersects(poly_4326):
            continue
        pt_32643 = gpd.GeoSeries([Point(lon, lat)], crs=4326).to_crs(32643).iloc[0]
        c_node = ox.nearest_nodes(G_proj, pt_32643.x, pt_32643.y)
        snap = float(pt_32643.distance(Point(G_proj.nodes[c_node]["x"], G_proj.nodes[c_node]["y"])))
        ndist = snap + float(dist_all.get(c_node, np.nan))
        rows.append({"h3_index": c, "res": res, "net_dist_m": ndist, "walk_min_4p8": ndist / 80.0, "geom": cell_poly_4326})
    gdf_h3 = gpd.GeoDataFrame(rows, geometry="geom", crs=4326)
    gdf_h3_utm = gdf_h3.to_crs(32643).set_geometry("geom")
    bld_cent = gpd.GeoDataFrame({"footprint_m2": bld_32643["footprint_m2"], "geometry": bld_32643.geometry.centroid}, geometry="geometry", crs=32643)
    joined = gpd.sjoin(bld_cent, gdf_h3_utm[["h3_index", "geom"]], how="inner", predicate="within")
    agg = joined.groupby("h3_index")["footprint_m2"].agg(bld_count="count", bld_area_m2="sum").reset_index()
    gdf_h3 = gdf_h3.merge(agg, on="h3_index", how="left").fillna({"bld_count": 0, "bld_area_m2": 0.0})
    gdf_h3["bld_count"] = gdf_h3["bld_count"].astype(int)
    gdf_h3["served_15min"] = gdf_h3["net_dist_m"] <= 1200.0
    pop_cells = gdf_h3[gdf_h3["bld_count"] > 0]
    beyond_proxy = (1.0 - pop_cells.loc[pop_cells["served_15min"], "bld_area_m2"].sum() / pop_cells["bld_area_m2"].sum()) * 100
    beyond_cells_pct = (1.0 - pop_cells["served_15min"].mean()) * 100
    worst_cell = pop_cells.loc[pop_cells["net_dist_m"].idxmax()]
    print(f"H3 res {res}: {len(gdf_h3)} cells ({len(pop_cells)} populated) | Beyond 1200 m: {beyond_proxy:.2f}% of proxy area ({beyond_cells_pct:.2f}% of populated cells)")
    print(f"  Worst-served populated H3 res {res} cell: {worst_cell['h3_index']} ({worst_cell['net_dist_m']:.1f} m / {worst_cell['walk_min_4p8']:.1f} min, {worst_cell['bld_count']} buildings)")
    gdf_h3.to_postgis(f"h3_res{res}_4326", engine, if_exists="replace", index=False)

with engine.begin() as conn:
    conn.execute(text("CREATE INDEX IF NOT EXISTS h3_res8_4326_geom_gist ON h3_res8_4326 USING GIST (geom);"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS h3_res9_4326_geom_gist ON h3_res9_4326 USING GIST (geom);"))

# Restart pg_tileserv / pg_featureserv awareness note
summary = {
    "q5": {"numberMatched": num_matched, "gml_kb": round(len(gml_bytes)/1024, 2), "geojson_kb": round(len(json_bytes)/1024, 2), "gml_first_pos": gml_first_pos, "geojson_first_coords": json_first_coords},
    "q6": {"changed_count": int(q6_df["changed"].sum()), "median_ratio": round(float(q6_df["ratio"].median()), 3), "max_ratio": round(float(worst["ratio"]), 3), "worst_pt_id": int(worst["pt_id"]), "worst_lat": round(worst_4326.y, 6), "worst_lon": round(worst_4326.x, 6), "sl_name": worst["sl_name"], "sl_m": round(float(worst["sl_m"]), 1), "net_name": worst["net_name"], "net_m": round(float(worst["net_m"]), 1)},
    "q7": {"net_beyond_proxy_pct": round(float(net_beyond_pct), 2), "buf_beyond_proxy_pct": round(float(buf_beyond_pct), 2)}
}
with open("analysis/phase3_q5_q7_summary.json", "w", encoding="utf-8") as f:
    json.dump(summary, f, indent=2)
print("\nSaved analysis/phase3_q5_q7_summary.json, analysis/phase3_evidence/q6_worst_barrier.png, and PostGIS tables h3_res8_4326 & h3_res9_4326.")








