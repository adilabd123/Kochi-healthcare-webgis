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

  const $ = (id) => document.getElementById(id);
  const state = {
    cfg: null,
    origin: null,
    set: "all",
    speed: 4.8,
    minutes: 15,
    coverageRows: []
  };

  let map;
  let popup;

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

  async function getJSON(url) {
    const r = await fetch(url);
    if (!r.ok) {
      let detail = r.statusText;
      try { detail = (await r.json()).detail || detail; } catch (_) {}
      throw new Error(`${r.status}: ${detail}`);
    }
    return r.json();
  }

  function wmsRasterUrl(layers, cqlFilter) {
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
    return decodeURIComponent(u.toString());
  }

  function serviceCql() {
    return `facility_set='${state.set}' AND speed_kmh=${state.speed}`;
  }

  function buildingsTileUrl() {
    return `${state.cfg.tileserv_url}/public.building_walk_time/{z}/{x}/{y}.pbf` +
           `?facility_set=${encodeURIComponent(state.set)}&speed_kmh=${state.speed}`;
  }

  function h3TileUrl(res) {
    return `${state.cfg.tileserv_url}/public.h3_res${res}_4326/{z}/{x}/{y}.pbf`;
  }

  function setVisibility(layerId, visible) {
    if (map && map.getLayer(layerId)) {
      map.setLayoutProperty(layerId, "visibility", visible ? "visible" : "none");
    }
  }

  function addWmsLayer(id, wmsLayers, cqlFilter, visible, opacity) {
    const srcId = `${id}-src`;
    map.addSource(srcId, {
      type: "raster",
      tiles: [wmsRasterUrl(wmsLayers, cqlFilter)],
      tileSize: 256
    });
    map.addLayer({
      id,
      type: "raster",
      source: srcId,
      layout: { visibility: visible ? "visible" : "none" },
      paint: { "raster-opacity": opacity ?? 0.8 }
    });
  }

  function rebuildBuildingsMvt() {
    const visible = $("chkBuildingsMvt").checked;
    if (map.getLayer("buildings-mvt-fill")) map.removeLayer("buildings-mvt-fill");
    if (map.getLayer("buildings-mvt-line")) map.removeLayer("buildings-mvt-line");
    if (map.getSource("buildings-mvt-src")) map.removeSource("buildings-mvt-src");

    const beforeLayer = map.getLayer("facilities-circle") ? "facilities-circle" : undefined;

    map.addSource("buildings-mvt-src", {
      type: "vector",
      tiles: [buildingsTileUrl()],
      minzoom: 11,
      maxzoom: 20
    });

    map.addLayer({
      id: "buildings-mvt-fill",
      type: "fill",
      source: "buildings-mvt-src",
      "source-layer": "building_walk_time",
      layout: { visibility: visible ? "visible" : "none" },
      paint: {
        "fill-color": BAND_COLORS,
        "fill-opacity": 0.82
      }
    }, beforeLayer);

    map.addLayer({
      id: "buildings-mvt-line",
      type: "line",
      source: "buildings-mvt-src",
      "source-layer": "building_walk_time",
      layout: { visibility: visible ? "visible" : "none" },
      paint: {
        "line-color": "#374151",
        "line-width": 0.4
      }
    }, beforeLayer);
  }

  function rebuildServiceWms() {
    const visible = $("chkServiceWms").checked;
    if (map.getLayer("service-wms")) map.removeLayer("service-wms");
    if (map.getSource("service-wms-src")) map.removeSource("service-wms-src");
    addWmsLayer("service-wms", `${state.cfg.workspace}:service_areas`, serviceCql(), visible, 0.72);
  }

  function addH3Layers(res, visible) {
    const srcId = `h3-r${res}-src`;
    const fillId = `h3-r${res}-fill`;
    const lineId = `h3-r${res}-line`;
    map.addSource(srcId, {
      type: "vector",
      tiles: [h3TileUrl(res)],
      minzoom: 10,
      maxzoom: 18
    });
    map.addLayer({
      id: fillId,
      type: "fill",
      source: srcId,
      "source-layer": `h3_res${res}_4326`,
      layout: { visibility: visible ? "visible" : "none" },
      paint: {
        "fill-color": [
          "step",
          ["coalesce", ["get", "walk_min_4p8"], 0],
          "#15803d",
          5, "#84cc16",
          10, "#facc15",
          15, "#dc2626"
        ],
        "fill-opacity": 0.35
      }
    });
    map.addLayer({
      id: lineId,
      type: "line",
      source: srcId,
      "source-layer": `h3_res${res}_4326`,
      layout: { visibility: visible ? "visible" : "none" },
      paint: {
        "line-color": "#1e293b",
        "line-width": res === 9 ? 1.0 : 1.6
      }
    });
  }

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
      zoom: 14
    });

    map.addControl(new maplibregl.NavigationControl(), "top-right");
    map.addControl(new maplibregl.ScaleControl({ maxWidth: 120, unit: "metric" }), "bottom-right");

    map.on("load", async () => {
      const ws = cfg.workspace;
      addWmsLayer("study-wms", `${ws}:study_area`, null, $("chkStudyWms").checked, 0.85);
      addWmsLayer("service-wms", `${ws}:service_areas`, serviceCql(), $("chkServiceWms").checked, 0.72);
      addWmsLayer("gaps-wms", `${ws}:hospital_access_gaps`, null, $("chkGapsWms").checked, 0.9);
      addWmsLayer("edges-wms", `${ws}:walk_edges`, null, $("chkEdgesWms").checked, 0.8);

      addH3Layers(8, $("chkH3Res8").checked);
      addH3Layers(9, $("chkH3Res9").checked);

      map.addSource("origin-src", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      map.addSource("result-src", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      map.addSource("wfs-src", { type: "geojson", data: { type: "FeatureCollection", features: [] } });

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

      await loadFacilities();
      rebuildBuildingsMvt();

      if (cfg.bbox) {
        const [minx, miny, maxx, maxy] = cfg.bbox;
        map.fitBounds([[minx, miny], [maxx, maxy]], { padding: 24 });
      }

      map.on("click", "buildings-mvt-fill", (e) => {
        if (!e.features || !e.features.length) return;
        const p = e.features[0].properties;
        popup.setLngLat(e.lngLat).setHTML(
          `<div class="popup"><h3>${esc(p.name || "Building")}</h3><table>` +
          `<tr><td>Type</td><td>${esc(p.building)}</td></tr>` +
          `<tr><td>Facility set</td><td>${esc(p.facility_set)}</td></tr>` +
          `<tr><td>Network distance</td><td>${esc(p.dist_m)} m</td></tr>` +
          `<tr><td>Walk @ ${state.speed} km/h</td><td><b>${esc(p.walk_min)} min</b> (${esc(p.band)})</td></tr>` +
          `</table></div>`
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
          layers: ["facilities-circle", "buildings-mvt-fill"].filter((id) => map.getLayer(id))
        });
        if (hits.length) return;
        setOrigin(e.lngLat.lat, e.lngLat.lng, "Map click");
      });

      wireLayerToggles();
    });
  }

  function wireLayerToggles() {
    $("chkBuildingsMvt").onchange = (e) => {
      setVisibility("buildings-mvt-fill", e.target.checked);
      setVisibility("buildings-mvt-line", e.target.checked);
    };
    $("chkFacilities").onchange = (e) => setVisibility("facilities-circle", e.target.checked);
    $("chkServiceWms").onchange = (e) => setVisibility("service-wms", e.target.checked);
    $("chkStudyWms").onchange = (e) => setVisibility("study-wms", e.target.checked);
    $("chkGapsWms").onchange = (e) => setVisibility("gaps-wms", e.target.checked);
    $("chkEdgesWms").onchange = (e) => setVisibility("edges-wms", e.target.checked);
    $("chkH3Res9").onchange = (e) => {
      setVisibility("h3-r9-fill", e.target.checked);
      setVisibility("h3-r9-line", e.target.checked);
    };
    $("chkH3Res8").onchange = (e) => {
      setVisibility("h3-r8-fill", e.target.checked);
      setVisibility("h3-r8-line", e.target.checked);
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
      paint: {
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
      }
    });
    return fc.features.length;
  }

  function refreshScenario() {
    if (!map || !map.loaded()) return;
    rebuildServiceWms();
    rebuildBuildingsMvt();
    renderCoverage();
  }

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
      const coords = iso.geometry?.coordinates?.[0];
      if (Array.isArray(coords) && coords.length) {
        const bounds = coords.reduce(
          (b, c) => b.extend(c),
          new maplibregl.LngLatBounds(coords[0], coords[0])
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

  let booted = false;
  async function boot() {
    if (booted) return;
    booted = true;
    try {
      state.cfg = await getJSON("/api/config");
    } catch (err) {
      setStatus(`Cannot load /api/config — is the API running? (${err.message})`, true);
      return;
    }
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

    const messages = [];
    try {
      state.coverageRows = await getJSON("/api/stats/coverage");
      renderCoverage();
    } catch (err) { messages.push(`coverage: ${err.message}`); }
    try {
      const count = map.getSource("facilities-src")?._data?.features?.length ?? 0;
      messages.push(`${count} facilities from pg_featureserv`);
    } catch (err) { messages.push(`pg_featureserv: ${err.message}`); }
    setStatus(`Ready — ${messages.join(" · ")}`, messages.some((m) => m.includes(":")));
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }
})();
