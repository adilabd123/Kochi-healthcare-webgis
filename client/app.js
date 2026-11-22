/* Kochi Healthcare WebGIS - MapLibre GL JS client
 *
 * Two modes, chosen automatically at start-up:
 *   LIVE    /api/config answers (Docker Compose or kind). Layers come from
 *           pg_tileserv (MVT), pg_featureserv (GeoJSON), GeoServer (WMS/WFS,
 *           DEM) and the FastAPI (pgRouting nearest/isochrone, Nominatim).
 *   STATIC  /api/config is unreachable (e.g. GitHub Pages). Everything is read
 *           from tiles/kochi.pmtiles with HTTP Range requests; walking times
 *           are recomputed in the browser from the stored network distances.
 * Force a mode with ?mode=static or ?mode=live.
 */
(function () {
  "use strict";

  const BAND_COLORS = [
    "match",
    ["get", "band"],
    "0-5 min", "#15803d",
    "5-10 min", "#84cc16",
    "10-15 min", "#facc15",
    ">15 min", "#dc2626",
    "#6b7280"
  ];

  const TYPE_COLORS = {
    hospital: "#dc2626",
    clinic: "#2563eb",
    doctors: "#2563eb",
    dentist: "#2563eb",
    centre: "#2563eb",
    pharmacy: "#16a34a",
    laboratory: "#9333ea"
  };

  const H3_FILL = [
    "step",
    ["coalesce", ["get", "walk_min_4p8"], 0],
    "#15803d",
    5, "#84cc16",
    10, "#facc15",
    15, "#dc2626"
  ];

  const DIST_FIELD = { all: "all_dist_m", hospital: "hospital_dist_m", clinical: "clinical_dist_m" };
  const STATIC_CONFIG = "tiles/kochi_static.json";
  const STATIC_PMTILES = "tiles/kochi.pmtiles";
  const LIVE_PROBE_TIMEOUT_MS = 3000;

  // Each checkbox controls every layer id listed here; ids missing in the
  // current mode are simply skipped.
  const TOGGLES = {
    chkFacilities: ["facilities-circle"],
    chkServiceWms: ["service-wms", "service-static"],
    chkStudyWms: ["study-wms", "study-static"],
    chkGapsWms: ["gaps-wms", "gaps-static"],
    chkEdgesWms: ["edges-wms"],
    chkDem: ["dem-wms"],
    chkH3Res9: ["h3-r9-fill", "h3-r9-line"],
    chkH3Res8: ["h3-r8-fill", "h3-r8-line"]
  };
  const BUILDING_LAYERS = {
    mvt: ["buildings-mvt-fill", "buildings-mvt-line"],
    wms: ["buildings-wms"],
    wfs: ["buildings-wfs-fill", "buildings-wfs-line"]
  };
  const MODE_LABELS = { wms: "(a) Tiled WMS PNG", wfs: "(b) Single WFS GeoJSON", mvt: "(c) MVT pg_tileserv" };

  const $ = (id) => document.getElementById(id);
  const state = {
    mode: "live",
    cfg: null,
    staticMeta: null,
    origin: null,
    set: "all",
    speed: 4.8,
    minutes: 15,
    coverageRows: [],
    bldMode: "mvt",
    wfsLoaded: false,
    q9Rows: []
  };

  let map;
  let popup;
  // Every request MapLibre makes (incl. vector tiles fetched inside its Web
  // Worker, which the page's Resource Timing API cannot see) passes here.
  const requestLog = [];

  function esc(v) {
    return String(v ?? "").replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  function setStatus(msg, isError) {
    const el = $("status");
    el.textContent = msg;
    el.className = isError ? "error" : "";
  }

  async function getJSON(url, timeoutMs) {
    const ctrl = timeoutMs ? new AbortController() : null;
    const timer = ctrl ? setTimeout(() => ctrl.abort(), timeoutMs) : null;
    try {
      const r = await fetch(url, ctrl ? { signal: ctrl.signal } : undefined);
      if (!r.ok) {
        let detail = r.statusText;
        try { detail = (await r.json()).detail || detail; } catch (_) {}
        throw new Error(`${r.status}: ${detail}`);
      }
      return await r.json();
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  const metresPerMinute = () => state.speed * 1000 / 60;

  // ------------------------------------------------------------- live URLs
  function wmsRasterUrl(layers, cqlFilter, extra) {
    const u = new URL(state.cfg.geoserver_wms_url, window.location.origin);
    u.searchParams.set("service", "WMS");
    u.searchParams.set("version", "1.1.1");
    u.searchParams.set("request", "GetMap");
    u.searchParams.set("layers", layers);
    u.searchParams.set("styles", "");
    u.searchParams.set("format", "image/png");
    u.searchParams.set("transparent", "true");
    u.searchParams.set("srs", "EPSG:3857");
    u.searchParams.set("width", "256");
    u.searchParams.set("height", "256");
    u.searchParams.set("bbox", "{bbox-epsg-3857}");
    if (cqlFilter) u.searchParams.set("CQL_FILTER", cqlFilter);
    Object.entries(extra || {}).forEach(([k, v]) => u.searchParams.set(k, v));
    return decodeURIComponent(u.toString());
  }

  function serviceCql() {
    return `facility_set='${state.set}' AND speed_kmh=${state.speed}`;
  }

  function buildingsTileUrl(cacheBuster) {
    return `${state.cfg.tileserv_url}/public.building_walk_time/{z}/{x}/{y}.pbf` +
           `?facility_set=${encodeURIComponent(state.set)}&speed_kmh=${state.speed}` +
           (cacheBuster ? `&_m=${cacheBuster}` : "");
  }

  function h3TileUrl(res) {
    return `${state.cfg.tileserv_url}/public.h3_res${res}_4326/{z}/{x}/{y}.pbf`;
  }

  function buildingsWfsUrl(cacheBuster) {
    const p = new URLSearchParams({
      service: "WFS", version: "2.0.0", request: "GetFeature",
      typeNames: `${state.cfg.workspace}:building_access`,
      outputFormat: "application/json", srsName: "EPSG:4326"
    });
    if (cacheBuster) p.set("_m", cacheBuster);
    return `${state.cfg.geoserver_proxy_wfs_url}?${p}`;
  }

  // --------------------------------------------------------- map helpers
  function setVisibility(layerId, visible) {
    if (map && map.getLayer(layerId)) {
      map.setLayoutProperty(layerId, "visibility", visible ? "visible" : "none");
    }
  }

  function firstExisting(ids) {
    return ids.find((id) => map.getLayer(id));
  }

  function removeLayers(layerIds, sourceId) {
    layerIds.forEach((id) => { if (map.getLayer(id)) map.removeLayer(id); });
    if (sourceId && map.getSource(sourceId)) map.removeSource(sourceId);
  }

  function addWmsLayer(id, wmsLayers, cqlFilter, visible, opacity, beforeId, extra) {
    const srcId = `${id}-src`;
    map.addSource(srcId, {
      type: "raster",
      tiles: [wmsRasterUrl(wmsLayers, cqlFilter, extra)],
      tileSize: 256
    });
    map.addLayer({
      id,
      type: "raster",
      source: srcId,
      layout: { visibility: visible ? "visible" : "none" },
      paint: { "raster-opacity": opacity ?? 0.8 }
    }, beforeId && map.getLayer(beforeId) ? beforeId : undefined);
  }

  function buildingsVisible(mode) {
    return $("chkBuildings").checked && state.bldMode === mode;
  }

  function beforeOverlays() {
    return firstExisting(["facilities-circle", "result-fill", "wfs-circle", "origin-circle"]);
  }

  // ------------------------------------------------------ buildings (Q9)
  function rebuildBuildingsMvt(cacheBuster) {
    removeLayers(BUILDING_LAYERS.mvt, "buildings-mvt-src");
    const visible = buildingsVisible("mvt");
    const beforeLayer = beforeOverlays();

    if (state.mode === "live") {
      map.addSource("buildings-mvt-src", {
        type: "vector",
        tiles: [buildingsTileUrl(cacheBuster)],
        minzoom: 11,
        maxzoom: 20
      });
      map.addLayer({
        id: "buildings-mvt-fill",
        type: "fill",
        source: "buildings-mvt-src",
        "source-layer": "building_walk_time",
        layout: { visibility: visible ? "visible" : "none" },
        paint: { "fill-color": BAND_COLORS, "fill-opacity": 0.82 }
      }, beforeLayer);
      map.addLayer({
        id: "buildings-mvt-line",
        type: "line",
        source: "buildings-mvt-src",
        "source-layer": "building_walk_time",
        layout: { visibility: visible ? "visible" : "none" },
        paint: { "line-color": "#374151", "line-width": 0.4 }
      }, beforeLayer);
    } else {
      map.addLayer({
        id: "buildings-mvt-fill",
        type: "fill",
        source: "kochi",
        "source-layer": "buildings",
        layout: { visibility: visible ? "visible" : "none" },
        paint: { "fill-color": staticBuildingColor(), "fill-opacity": 0.82 }
      }, beforeLayer);
      map.addLayer({
        id: "buildings-mvt-line",
        type: "line",
        source: "kochi",
        "source-layer": "buildings",
        layout: { visibility: visible ? "visible" : "none" },
        paint: { "line-color": "#374151", "line-width": 0.4 }
      }, beforeLayer);
    }
  }

  function rebuildBuildingsWms(cacheBuster) {
    removeLayers(BUILDING_LAYERS.wms, "buildings-wms-src");
    addWmsLayer("buildings-wms", `${state.cfg.workspace}:building_access`, null,
      buildingsVisible("wms"), 0.85, beforeOverlays(), cacheBuster ? { _m: cacheBuster } : null);
  }

  async function fetchBuildingsWfs(cacheBuster) {
    const t0 = performance.now();
    const url = buildingsWfsUrl(cacheBuster);
    const r = await fetch(url);
    if (!r.ok) throw new Error(`WFS ${r.status}`);
    const buf = await r.arrayBuffer();
    const fc = JSON.parse(new TextDecoder().decode(buf));
    const entry = performance.getEntriesByName(new URL(url, window.location.href).href).pop();
    const bytes = (entry && entry.transferSize) || buf.byteLength;
    return { fc, bytes, ms: performance.now() - t0, url };
  }

  async function loadBuildingsWfs(cacheBuster) {
    const res = await fetchBuildingsWfs(cacheBuster);
    addBuildingsWfs(res.fc);
    return { features: res.fc.features.length, bytes: res.bytes, ms: res.ms };
  }

  function addBuildingsWfs(fc) {
    removeLayers(BUILDING_LAYERS.wfs, "buildings-wfs-src");
    const beforeLayer = beforeOverlays();
    map.addSource("buildings-wfs-src", { type: "geojson", data: fc });
    const vis = buildingsVisible("wfs") ? "visible" : "none";
    map.addLayer({
      id: "buildings-wfs-fill",
      type: "fill",
      source: "buildings-wfs-src",
      layout: { visibility: vis },
      paint: {
        "fill-color": ["match", ["get", "all_band4p8"],
          "0-5 min", "#15803d", "5-10 min", "#84cc16", "10-15 min", "#facc15", ">15 min", "#dc2626", "#6b7280"],
        "fill-opacity": 0.82
      }
    }, beforeLayer);
    map.addLayer({
      id: "buildings-wfs-line",
      type: "line",
      source: "buildings-wfs-src",
      layout: { visibility: vis },
      paint: { "line-color": "#374151", "line-width": 0.4 }
    }, beforeLayer);
    state.wfsLoaded = true;
  }

  async function applyBuildingMode() {
    Object.entries(BUILDING_LAYERS).forEach(([mode, ids]) =>
      ids.forEach((id) => setVisibility(id, buildingsVisible(mode))));
    if (state.mode === "live" && state.bldMode === "wfs" && !state.wfsLoaded && $("chkBuildings").checked) {
      setStatus("Loading all buildings with one WFS 2.0 GetFeature…");
      try {
        const res = await loadBuildingsWfs();
        setStatus(`WFS: ${res.features} buildings, ${(res.bytes / 1024).toFixed(1)} KB, ${Math.round(res.ms)} ms`);
      } catch (err) {
        setStatus(`WFS buildings failed: ${err.message}`, true);
      }
    }
  }

  // ------------------------------------------------------- static styling
  function staticBuildingColor() {
    const field = DIST_FIELD[state.set];
    return [
      "case",
      ["==", ["typeof", ["get", field]], "number"],
      ["step", ["/", ["get", field], metresPerMinute()],
        "#15803d", 5, "#84cc16", 10, "#facc15", 15, "#dc2626"],
      "#6b7280"
    ];
  }

  function staticServiceFilter() {
    return ["all",
      ["==", ["get", "facility_set"], state.set],
      ["==", ["get", "speed_kmh"], state.speed]];
  }

  // ------------------------------------------------------------ services
  function rebuildServiceWms() {
    const visible = $("chkServiceWms").checked;
    removeLayers(["service-wms"], "service-wms-src");
    addWmsLayer("service-wms", `${state.cfg.workspace}:service_areas`, serviceCql(), visible, 0.72,
      firstExisting(["buildings-wms", "buildings-wfs-fill", "buildings-mvt-fill", "gaps-wms", "h3-r8-fill"]));
  }

  function addH3Layers(res, visible) {
    const srcId = state.mode === "live" ? `h3-r${res}-src` : "kochi";
    const sourceLayer = state.mode === "live" ? `h3_res${res}_4326` : `h3_res${res}`;
    if (state.mode === "live") {
      map.addSource(srcId, { type: "vector", tiles: [h3TileUrl(res)], minzoom: 10, maxzoom: 18 });
    }
    map.addLayer({
      id: `h3-r${res}-fill`,
      type: "fill",
      source: srcId,
      "source-layer": sourceLayer,
      layout: { visibility: visible ? "visible" : "none" },
      paint: { "fill-color": H3_FILL, "fill-opacity": 0.35 }
    });
    map.addLayer({
      id: `h3-r${res}-line`,
      type: "line",
      source: srcId,
      "source-layer": sourceLayer,
      layout: { visibility: visible ? "visible" : "none" },
      paint: { "line-color": "#1e293b", "line-width": res === 9 ? 1.0 : 1.6 }
    });
  }

  function addOverlaySources() {
    const empty = { type: "FeatureCollection", features: [] };
    map.addSource("origin-src", { type: "geojson", data: empty });
    map.addSource("result-src", { type: "geojson", data: empty });
    map.addSource("wfs-src", { type: "geojson", data: empty });
    map.addLayer({
      id: "result-fill",
      type: "fill",
      source: "result-src",
      filter: ["==", ["geometry-type"], "Polygon"],
      paint: { "fill-color": "#a78bfa", "fill-opacity": 0.3 }
    });
    map.addLayer({
      id: "result-line",
      type: "line",
      source: "result-src",
      paint: { "line-color": "#7c3aed", "line-width": 3 }
    });
    map.addLayer({
      id: "wfs-circle",
      type: "circle",
      source: "wfs-src",
      paint: {
        "circle-radius": 10,
        "circle-color": "transparent",
        "circle-stroke-color": "#dc2626",
        "circle-stroke-width": 2.5
      }
    });
    map.addLayer({
      id: "origin-circle",
      type: "circle",
      source: "origin-src",
      paint: {
        "circle-radius": 8,
        "circle-color": "#0f172a",
        "circle-stroke-color": "#ffffff",
        "circle-stroke-width": 2
      }
    });
  }

  function facilityPaint() {
    return {
      "circle-radius": ["case", ["==", ["get", "type"], "hospital"], 7.5, 5.5],
      "circle-color": [
        "match", ["get", "type"],
        "hospital", TYPE_COLORS.hospital,
        "clinic", TYPE_COLORS.clinic,
        "doctors", TYPE_COLORS.doctors,
        "dentist", TYPE_COLORS.dentist,
        "centre", TYPE_COLORS.centre,
        "pharmacy", TYPE_COLORS.pharmacy,
        "laboratory", TYPE_COLORS.laboratory,
        "#6b7280"
      ],
      "circle-stroke-color": "#ffffff",
      "circle-stroke-width": 1.5
    };
  }

  async function loadFacilities() {
    const url = `${state.cfg.featureserv_url}/collections/public.facility_access_4326/items.json?limit=1000`;
    const fc = await getJSON(url);
    map.addSource("facilities-src", { type: "geojson", data: fc });
    map.addLayer({
      id: "facilities-circle",
      type: "circle",
      source: "facilities-src",
      layout: { visibility: $("chkFacilities").checked ? "visible" : "none" },
      paint: facilityPaint()
    }, firstExisting(["result-fill"]));
    return fc.features.length;
  }

  // -------------------------------------------------------- layer stacks
  function addLiveLayers(cfg) {
    const ws = cfg.workspace;
    addWmsLayer("dem-wms", `${ws}:dem`, null, $("chkDem").checked, 0.75);
    addWmsLayer("study-wms", `${ws}:study_area`, null, $("chkStudyWms").checked, 0.85);
    addWmsLayer("service-wms", `${ws}:service_areas`, serviceCql(), $("chkServiceWms").checked, 0.72);
    addWmsLayer("gaps-wms", `${ws}:hospital_access_gaps`, null, $("chkGapsWms").checked, 0.9);
    addWmsLayer("edges-wms", `${ws}:walk_edges`, null, $("chkEdgesWms").checked, 0.8);
    addH3Layers(8, $("chkH3Res8").checked);
    addH3Layers(9, $("chkH3Res9").checked);
  }

  function addStaticLayers() {
    map.addSource("kochi", {
      type: "vector",
      url: `pmtiles://${new URL(STATIC_PMTILES, window.location.href).href}`,
      attribution: "© OpenStreetMap contributors"
    });
    map.addLayer({
      id: "service-static",
      type: "fill",
      source: "kochi",
      "source-layer": "service_areas",
      filter: staticServiceFilter(),
      layout: {
        visibility: $("chkServiceWms").checked ? "visible" : "none",
        "fill-sort-key": ["-", 0, ["get", "minutes"]]
      },
      paint: {
        "fill-color": ["match", ["get", "minutes"], 5, "#f87171", 10, "#fdba74", "#fde68a"],
        "fill-opacity": 0.45,
        "fill-outline-color": "#b45309"
      }
    });
    map.addLayer({
      id: "gaps-static",
      type: "fill",
      source: "kochi",
      "source-layer": "hospital_gaps",
      layout: { visibility: $("chkGapsWms").checked ? "visible" : "none" },
      paint: {
        "fill-color": ["match", ["get", "gap_class"], "gap_at_4.8_and_3.6", "#7f1d1d", "#f97316"],
        "fill-opacity": 0.85
      }
    });
    addH3Layers(8, $("chkH3Res8").checked);
    addH3Layers(9, $("chkH3Res9").checked);
    map.addLayer({
      id: "study-static",
      type: "line",
      source: "kochi",
      "source-layer": "study_area",
      layout: { visibility: $("chkStudyWms").checked ? "visible" : "none" },
      paint: { "line-color": "#111827", "line-width": 2.5, "line-dasharray": [4, 2] }
    });
    map.addLayer({
      id: "facilities-circle",
      type: "circle",
      source: "kochi",
      "source-layer": "facilities",
      layout: { visibility: $("chkFacilities").checked ? "visible" : "none" },
      paint: facilityPaint()
    });
  }

  // ---------------------------------------------------------------- map
  async function initMap(cfg) {
    popup = new maplibregl.Popup({ closeButton: true, maxWidth: "320px" });

    map = new maplibregl.Map({
      container: "map",
      style: {
        version: 8,
        sources: {
          osm: {
            type: "raster",
            tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
            tileSize: 256,
            attribution: "© OpenStreetMap contributors"
          }
        },
        layers: [
          { id: "osm-base", type: "raster", source: "osm" }
        ]
      },
      center: [cfg.center.lon, cfg.center.lat],
      zoom: 14,
      hash: false,
      transformRequest: (url) => {
        requestLog.push({ url, t: performance.now() });
        if (requestLog.length > 5000) requestLog.splice(0, 1000);
        return { url };
      }
    });
    window.kochi = { map, state };       // handy in DevTools: kochi.map.getStyle().layers

    map.addControl(new maplibregl.NavigationControl(), "top-right");
    map.addControl(new maplibregl.ScaleControl({ maxWidth: 120, unit: "metric" }), "bottom-right");

    await new Promise((resolve) => map.on("load", resolve));

    if (state.mode === "live") {
      addLiveLayers(cfg);
      addOverlaySources();
      try {
        await loadFacilities();
      } catch (err) {
        setStatus(`pg_featureserv: ${err.message}`, true);
      }
      rebuildBuildingsMvt();
      rebuildBuildingsWms();
    } else {
      addStaticLayers();
      addOverlaySources();
      rebuildBuildingsMvt();
    }

    if (cfg.bbox) {
      const [minx, miny, maxx, maxy] = cfg.bbox;
      map.fitBounds([[minx, miny], [maxx, maxy]], { padding: 24, duration: 0 });
    }

    const onBuildingClick = (e) => {
      if (!e.features || !e.features.length) return;
      const p = e.features[0].properties;
      let dist = p.dist_m;
      let walk = p.walk_min;
      let band = p.band;
      let set = p.facility_set;
      if (state.mode === "static") {
        dist = p[DIST_FIELD[state.set]];
        set = state.set;
        if (typeof dist === "number") {
          walk = (dist / metresPerMinute()).toFixed(1);
          band = walk <= 5 ? "0-5 min" : walk <= 10 ? "5-10 min" : walk <= 15 ? "10-15 min" : ">15 min";
          dist = Math.round(dist);
        } else {
          walk = "–"; band = "unreachable"; dist = "–";
        }
      }
      popup.setLngLat(e.lngLat).setHTML(
        `<div class="popup"><h3>${esc(p.name || "Building")}</h3><table>` +
        `<tr><td>Type</td><td>${esc(p.building)}</td></tr>` +
        `<tr><td>Facility set</td><td>${esc(set)}</td></tr>` +
        `<tr><td>Network distance</td><td>${esc(dist)} m</td></tr>` +
        `<tr><td>Walk @ ${state.speed} km/h</td><td><b>${esc(walk)} min</b> (${esc(band)})</td></tr>` +
        `</table></div>`
      ).addTo(map);
    };
    map.on("click", "buildings-mvt-fill", onBuildingClick);
    map.on("click", "buildings-wfs-fill", (e) => {
      if (!e.features || !e.features.length) return;
      const p = e.features[0].properties;
      popup.setLngLat(e.lngLat).setHTML(
        `<div class="popup"><h3>${esc(p.name || "Building")}</h3><table>` +
        `<tr><td>Type</td><td>${esc(p.building)}</td></tr>` +
        `<tr><td>To nearest facility (all)</td><td>${esc(p.all_dist_m)} m</td></tr>` +
        `<tr><td>Walk @ 4.8 km/h</td><td><b>${esc(p.all_t4p8_min)} min</b> (${esc(p.all_band4p8)})</td></tr>` +
        `<tr><td>Delivered by</td><td>WFS 2.0 GeoJSON</td></tr></table></div>`
      ).addTo(map);
    });

    map.on("click", "facilities-circle", (e) => {
      if (!e.features || !e.features.length) return;
      const p = e.features[0].properties;
      popup.setLngLat(e.lngLat).setHTML(
        `<div class="popup"><h3>${esc(p.name || "(unnamed)")}</h3><table>` +
        `<tr><td>Type</td><td>${esc(p.type)}</td></tr>` +
        `<tr><td>To nearest hospital</td><td>${esc(p.hospital_dist_m)} m · ` +
        `${esc(p.hospital_t4p8_min)} min @4.8 · ${esc(p.hospital_t3p6_min)} min @3.6</td></tr>` +
        `<tr><td>15-min catchment (all)</td><td>${esc(p.all_catch15_4p8 ?? "–")} buildings</td></tr>` +
        `<tr><td>Snap to network</td><td>${esc(p.snap_m)} m</td></tr>` +
        `<tr><td>OSM</td><td>${esc(p.osm_type)} ${esc(p.osm_id)}</td></tr></table></div>`
      ).addTo(map);
    });

    map.on("click", (e) => {
      const hits = map.queryRenderedFeatures(e.point, {
        layers: ["facilities-circle", "buildings-mvt-fill", "buildings-wfs-fill"].filter((id) => map.getLayer(id))
      });
      if (hits.length || state.mode !== "live") return;
      setOrigin(e.lngLat.lat, e.lngLat.lng, "Map click");
    });

    wireLayerToggles();
  }

  function wireLayerToggles() {
    Object.entries(TOGGLES).forEach(([chk, ids]) => {
      $(chk).onchange = (e) => ids.forEach((id) => setVisibility(id, e.target.checked));
    });
    $("chkBuildings").onchange = () => applyBuildingMode();
    document.querySelectorAll('input[name="bldMode"]').forEach((radio) => {
      radio.onchange = (e) => {
        if (!e.target.checked) return;
        state.bldMode = e.target.value;
        applyBuildingMode();
      };
    });
  }

  function refreshScenario() {
    if (!map || !map.isStyleLoaded()) return;
    if (state.mode === "live") {
      rebuildServiceWms();
      rebuildBuildingsMvt();
    } else {
      if (map.getLayer("buildings-mvt-fill")) {
        map.setPaintProperty("buildings-mvt-fill", "fill-color", staticBuildingColor());
      }
      if (map.getLayer("service-static")) map.setFilter("service-static", staticServiceFilter());
    }
    renderCoverage();
  }

  // ------------------------------------------------------------ routing
  function setOrigin(lat, lon, label) {
    state.origin = { lat, lon };
    map.getSource("origin-src").setData({
      type: "FeatureCollection",
      features: [{
        type: "Feature",
        geometry: { type: "Point", coordinates: [lon, lat] },
        properties: { label }
      }]
    });
    $("originInfo").textContent = `${label}: ${lat.toFixed(5)}, ${lon.toFixed(5)}`;
    $("btnNearest").disabled = false;
    $("btnIsochrone").disabled = false;
  }

  async function onSearch(ev) {
    ev.preventDefault();
    const q = $("searchInput").value.trim();
    const list = $("searchResults");
    list.innerHTML = "<li>Searching…</li>";
    try {
      const results = await getJSON(`/api/geocode?q=${encodeURIComponent(q)}`);
      list.innerHTML = "";
      if (!results.length) { list.innerHTML = "<li>No results.</li>"; return; }
      results.forEach((r) => {
        const li = document.createElement("li");
        li.textContent = r.name;
        li.onclick = () => {
          setOrigin(r.lat, r.lon, r.name.split(",")[0]);
          map.flyTo({ center: [r.lon, r.lat], zoom: 16 });
        };
        list.appendChild(li);
      });
    } catch (err) {
      list.innerHTML = `<li class="error">${esc(err.message)}</li>`;
    }
  }

  async function onNearest() {
    if (!state.origin) return;
    const { lat, lon } = state.origin;
    const list = $("nearestList");
    list.innerHTML = "<li>Routing on the walk network…</li>";
    try {
      const res = await getJSON(`/api/nearest?lat=${lat}&lon=${lon}&facility_set=${state.set}&speed_kmh=${state.speed}&k=5`);
      const feats = [];
      if (res.route) {
        if (res.route.type === "FeatureCollection") feats.push(...res.route.features);
        else feats.push(res.route);
      }
      if (res.results && res.results.features) {
        feats.push(...res.results.features);
      }
      map.getSource("result-src").setData({ type: "FeatureCollection", features: feats });

      list.innerHTML = "";
      (res.results.features || []).forEach((f) => {
        const p = f.properties;
        const [flon, flat] = f.geometry.coordinates;
        const li = document.createElement("li");
        li.innerHTML = `<b>${esc(p.name || "(unnamed)")}</b> (${esc(p.type)}) — ` +
          `${p.walk_min} min · ${Math.round(p.network_m)} m walk vs ${Math.round(p.straight_m)} m straight` +
          (p.detour_ratio ? ` · detour ×${p.detour_ratio}` : "");
        li.onclick = () => map.flyTo({ center: [flon, flat], zoom: 17 });
        list.appendChild(li);
      });
      setStatus(`Nearest (${state.set}, ${state.speed} km/h): pgRouting ${res.elapsed_ms} ms`);
    } catch (err) {
      list.innerHTML = `<li class="error">${esc(err.message)}</li>`;
    }
  }

  async function onIsochrone() {
    if (!state.origin) return;
    const { lat, lon } = state.origin;
    $("isoInfo").textContent = "Computing isochrone…";
    try {
      const iso = await getJSON(`/api/isochrone?lat=${lat}&lon=${lon}&minutes=${state.minutes}&speed_kmh=${state.speed}`);
      map.getSource("result-src").setData({ type: "FeatureCollection", features: [iso] });
      const rings = iso.geometry?.type === "MultiPolygon"
        ? iso.geometry.coordinates.flatMap((poly) => poly[0])
        : iso.geometry?.coordinates?.[0];
      if (Array.isArray(rings) && rings.length) {
        const bounds = rings.reduce(
          (b, c) => b.extend(c),
          new maplibregl.LngLatBounds(rings[0], rings[0])
        );
        map.fitBounds(bounds, { padding: 30 });
      }
      const p = iso.properties;
      $("isoInfo").innerHTML = `<b>${p.minutes} min @ ${p.speed_kmh} km/h</b> (${Math.round(p.distance_budget_m)} m budget): ` +
        `${p.area_km2} km², ${p.buildings} buildings, ${p.facilities} facilities (${p.hospitals} hospital-tagged). ` +
        `<span class="hint">${esc(p.method)}; ${p.elapsed_ms} ms</span>`;
    } catch (err) {
      $("isoInfo").innerHTML = `<span class="error">${esc(err.message)}</span>`;
    }
  }

  async function onWfsHospitals() {
    const params = new URLSearchParams({
      service: "WFS", version: "2.0.0", request: "GetFeature",
      typeNames: `${state.cfg.workspace}:healthcare`, outputFormat: "application/json",
      CQL_FILTER: "type='hospital'"
    });
    $("wfsInfo").textContent = "Requesting WFS…";
    try {
      const fc = await getJSON(`${state.cfg.geoserver_proxy_wfs_url}?${params}`);
      map.getSource("wfs-src").setData(fc);
      $("wfsInfo").textContent = `WFS 2.0 GetFeature, CQL_FILTER type='hospital': ${fc.features.length} features.`;
    } catch (err) {
      $("wfsInfo").innerHTML = `<span class="error">${esc(err.message)}</span>`;
    }
  }

  // -------------------------------------------------------- Q9 benchmark
  const Q9_MATCH = {
    mvt: (u) => u.includes("building_walk_time"),
    wms: (u) => u.includes("GetMap") && (u.includes("building_access") || u.includes("building%5Faccess")),
    wfs: (u) => u.includes("GetFeature") && u.includes("building_access")
  };

  const BUILDING_SOURCES = { mvt: "buildings-mvt-src", wms: "buildings-wms-src", wfs: "buildings-wfs-src" };

  // Time until the buildings source alone has finished loading after mutate()
  // replaced it (basemap and other layers are deliberately not waited for).
  function settleAfter(mode, tag, mutate, timeoutMs) {
    return new Promise((resolve) => {
      const srcId = BUILDING_SOURCES[mode];
      const started = performance.now();
      let sawRequest = mode === "wfs";
      mutate();
      map.triggerRepaint();
      const poll = () => {
        if (!sawRequest) sawRequest = requestLog.some((r) => r.t >= started - 1 && r.url.includes(tag));
        let loaded = false;
        try { loaded = Boolean(map.getSource(srcId)) && map.isSourceLoaded(srcId); } catch (_) { loaded = false; }
        if (sawRequest && loaded) return resolve(false);
        if (performance.now() - started > timeoutMs) return resolve(true);
        setTimeout(poll, 25);
      };
      setTimeout(poll, 25);
    });
  }

  async function onMeasure() {
    if (state.mode !== "live") return;
    const mode = state.bldMode;
    $("chkBuildings").checked = true;
    $("btnMeasure").disabled = true;
    const zoom = map.getZoom();
    setStatus(`Measuring ${MODE_LABELS[mode]} at zoom ${zoom.toFixed(1)}…`);
    performance.clearResourceTimings();
    performance.setResourceTimingBufferSize(2000);
    const stamp = Date.now();
    const tag = `_m=${stamp}`;
    const t0 = performance.now();
    try {
      let wfs = null;
      let timedOut;
      if (mode === "wfs") {
        wfs = await fetchBuildingsWfs(stamp);
        timedOut = await settleAfter(mode, tag, () => { addBuildingsWfs(wfs.fc); applyBuildingMode(); }, 60000);
      } else {
        timedOut = await settleAfter(mode, tag, () => {
          if (mode === "mvt") rebuildBuildingsMvt(stamp); else rebuildBuildingsWms(stamp);
          applyBuildingMode();
        }, 60000);
      }
      const elapsed = performance.now() - t0;
      let requests;
      let bytes = 0;
      if (mode === "wfs") {
        requests = 1;
        bytes = wfs.bytes;
      } else {
        requests = requestLog.filter((r) => r.t >= t0 - 1 && r.url.includes(tag)).length;
        bytes = performance.getEntriesByType("resource")
          .filter((e) => e.startTime >= t0 - 1 && e.name.includes(tag) && Q9_MATCH[mode](e.name))
          .reduce((sum, e) => sum + (e.transferSize || e.encodedBodySize || 0), 0);
      }
      const row = {
        method: MODE_LABELS[mode],
        zoom: Math.round(zoom),
        requests,
        kb: bytes > 0 ? (bytes / 1024).toFixed(1) : "DevTools",
        ms: Math.round(elapsed) + (timedOut ? " (timeout)" : "")
      };
      state.q9Rows.push(row);
      renderQ9();
      setStatus(`Q9: ${row.method} z${row.zoom}: ${row.requests} requests, ${row.kb} KB, ${row.ms} ms until the buildings layer loaded`);
    } catch (err) {
      setStatus(`Q9 measurement failed: ${err.message}`, true);
    } finally {
      $("btnMeasure").disabled = false;
    }
  }

  function renderQ9() {
    const tbody = $("q9Table").querySelector("tbody");
    tbody.innerHTML = "";
    state.q9Rows.forEach((r) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${esc(r.method)}</td><td>${r.zoom}</td><td>${r.requests}</td><td>${esc(r.kb)}</td><td>${r.ms}</td>`;
      tbody.appendChild(tr);
    });
  }

  async function copyQ9Markdown() {
    const lines = ["| Method | Zoom | Requests | KB | ms to load |", "|---|---|---|---|---|"]
      .concat(state.q9Rows.map((r) => `| ${r.method} | ${r.zoom} | ${r.requests} | ${r.kb} | ${r.ms} |`));
    const text = lines.join("\n");
    try {
      await navigator.clipboard.writeText(text);
      setStatus("Q9 table copied to the clipboard as Markdown.");
    } catch (_) {
      window.prompt("Copy the Q9 table:", text);
    }
  }

  // ------------------------------------------------------------ coverage
  function renderCoverage() {
    const tbody = $("coverageTable").querySelector("tbody");
    tbody.innerHTML = "";
    [4.8, 3.6].forEach((sp) => {
      const rows = state.coverageRows.filter((r) => r.facility_set === state.set && Number(r.speed_kmh) === sp);
      const cell = (m) => {
        const r = rows.find((x) => Number(x.minutes) === m);
        return r ? `${Number(r.pct_buildings).toFixed(1)}%` : "–";
      };
      const tr = document.createElement("tr");
      if (sp === state.speed) tr.style.fontWeight = "600";
      tr.innerHTML = `<td>${sp} km/h</td><td>${cell(5)}</td><td>${cell(10)}</td><td>${cell(15)}</td>`;
      tbody.appendChild(tr);
    });
  }

  // ---------------------------------------------------------------- boot
  async function detectMode() {
    const forced = new URLSearchParams(window.location.search).get("mode");
    if (forced !== "static") {
      try {
        const cfg = await getJSON("/api/config", LIVE_PROBE_TIMEOUT_MS);
        if (cfg && cfg.tileserv_url) return { mode: "live", cfg };
      } catch (err) {
        if (forced === "live") throw err;
      }
    }
    if (typeof pmtiles === "undefined") throw new Error("pmtiles.js did not load");
    const meta = await getJSON(STATIC_CONFIG);
    const protocol = new pmtiles.Protocol();
    maplibregl.addProtocol("pmtiles", protocol.tile);
    return { mode: "static", cfg: { center: meta.center, bbox: meta.bbox }, meta };
  }

  function applyModeToUi() {
    const badge = $("modeBadge");
    if (state.mode === "live") {
      badge.textContent = "LIVE · Docker / Kubernetes services";
      badge.className = "badge live";
      return;
    }
    document.body.classList.add("static-mode");
    badge.textContent = "STATIC · PMTiles (no server)";
    badge.className = "badge static";
    $("mvtSourceLabel").textContent = "static PMTiles";
    $("facSourceLabel").textContent = "static PMTiles";
    document.querySelectorAll(".live-only input, .live-only select, .live-only button")
      .forEach((el) => { el.disabled = true; });
  }

  let booted = false;
  async function boot() {
    if (booted) return;
    booted = true;
    try {
      const detected = await detectMode();
      state.mode = detected.mode;
      state.cfg = detected.cfg;
      state.staticMeta = detected.meta || null;
    } catch (err) {
      setStatus(`No live API and no static PMTiles found (${err.message})`, true);
      return;
    }
    applyModeToUi();
    await initMap(state.cfg);

    $("facilitySet").onchange = (e) => { state.set = e.target.value; refreshScenario(); };
    $("speed").onchange = (e) => { state.speed = Number(e.target.value); refreshScenario(); };
    $("minutes").onchange = (e) => { state.minutes = Number(e.target.value); };
    $("searchForm").onsubmit = onSearch;
    $("btnNearest").onclick = onNearest;
    $("btnIsochrone").onclick = onIsochrone;
    $("btnWfsHospitals").onclick = onWfsHospitals;
    $("btnClear").onclick = () => {
      ["origin-src", "result-src", "wfs-src"].forEach((id) => {
        if (map && map.getSource(id)) {
          map.getSource(id).setData({ type: "FeatureCollection", features: [] });
        }
      });
      state.origin = null;
      $("btnNearest").disabled = true; $("btnIsochrone").disabled = true;
      $("nearestList").innerHTML = ""; $("isoInfo").textContent = ""; $("wfsInfo").textContent = "";
      $("originInfo").textContent = "No origin selected.";
    };
    document.querySelectorAll(".zoomBtn").forEach((btn) => {
      btn.onclick = () => map.jumpTo({ center: [state.cfg.center.lon, state.cfg.center.lat],
                                        zoom: Number(btn.dataset.zoom) });
    });
    $("btnMeasure").onclick = onMeasure;
    $("btnQ9Copy").onclick = copyQ9Markdown;
    $("btnQ9Reset").onclick = () => { state.q9Rows = []; renderQ9(); };

    if (state.mode === "live") {
      const messages = [];
      try {
        state.coverageRows = await getJSON("/api/stats/coverage");
        renderCoverage();
      } catch (err) { messages.push(`coverage: ${err.message}`); }
      const src = map.getSource("facilities-src");
      const count = src && src._data && src._data.features ? src._data.features.length : 0;
      messages.push(`${count} facilities from pg_featureserv`);
      setStatus(`Ready (live) — ${messages.join(" · ")}`, messages.some((m) => m.includes(":")));
    } else {
      state.coverageRows = state.staticMeta.coverage || [];
      renderCoverage();
      setStatus(`Ready (static PMTiles, ${state.staticMeta.tiles_written} tiles, generated ` +
        `${state.staticMeta.generated_at_utc}). Routing, geocoding, WMS/WFS and DEM need the live stack.`);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }
})();
