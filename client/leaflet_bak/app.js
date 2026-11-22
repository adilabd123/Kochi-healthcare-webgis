/* Kochi Healthcare WebGIS - Leaflet client
 *
 * Where each layer comes from:
 *   GeoServer WMS   study area, 5/10/15-min service areas (CQL_FILTER), elderly
 *                   hospital-access gaps, walk network          -> images
 *   GeoServer WFS   "hospitals" demo via the API's same-origin proxy -> GeoJSON
 *   pg_tileserv     public.building_walk_time function layer      -> vector tiles
 *   pg_featureserv  public.facility_access_4326 collection        -> GeoJSON
 *   FastAPI         /api/geocode (Nominatim), /api/nearest and /api/isochrone (pgRouting)
 */
(function () {
  "use strict";

  // Leaflet.VectorGrid 1.3.0 calls L.DomEvent.fakeStop, removed in Leaflet 1.8+.
  if (!L.DomEvent.fakeStop) {
    L.DomEvent.fakeStop = function () { return true; };
  }

  const BAND_COLORS = {
    "0-5 min": "#15803d",
    "5-10 min": "#84cc16",
    "10-15 min": "#facc15",
    ">15 min": "#dc2626",
    "unreachable": "#6b7280",
  };
  const TYPE_COLORS = { hospital: "#dc2626", clinic: "#2563eb", doctors: "#2563eb", dentist: "#2563eb",
                        centre: "#2563eb", pharmacy: "#16a34a", laboratory: "#9333ea" };

  const $ = (id) => document.getElementById(id);
  const state = { cfg: null, origin: null, set: "all", speed: 4.8, minutes: 15, lastVectorClick: 0 };
  let map, serviceWms, buildingsMvt, facilitiesLayer, layerControl;
  const originLayer = L.layerGroup();
  const resultLayer = L.layerGroup();
  const wfsLayer = L.layerGroup();

  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
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
      try { detail = (await r.json()).detail || detail; } catch (_) { /* not JSON */ }
      throw new Error(`${r.status}: ${detail}`);
    }
    return r.json();
  }

  // ------------------------------------------------------------------ map
  function initMap(cfg) {
    map = L.map("map", { zoomControl: true }).setView([cfg.center.lat, cfg.center.lon], 14);
    map.createPane("serviceAreas").style.zIndex = 320;
    map.createPane("buildings").style.zIndex = 360;
    map.createPane("gaps").style.zIndex = 370;
    map.createPane("network").style.zIndex = 380;

    const osm = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19, attribution: "&copy; OpenStreetMap contributors" }).addTo(map);
    const light = L.tileLayer("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png", {
      maxZoom: 19, subdomains: "abcd",
      attribution: "&copy; OpenStreetMap contributors &copy; CARTO" });

    const wmsUrl = cfg.geoserver_wms_url;
    const ws = cfg.workspace;
    const wmsCommon = { format: "image/png", transparent: true, version: "1.1.1", maxZoom: 20 };

    const studyWms = L.tileLayer.wms(wmsUrl, { ...wmsCommon, layers: `${ws}:study_area` }).addTo(map);
    serviceWms = L.tileLayer.wms(wmsUrl, {
      ...wmsCommon, layers: `${ws}:service_areas`, pane: "serviceAreas", opacity: 0.75,
      CQL_FILTER: serviceCql() }).addTo(map);
    const gapsWms = L.tileLayer.wms(wmsUrl, {
      ...wmsCommon, layers: `${ws}:hospital_access_gaps`, pane: "gaps", opacity: 0.9 });
    const edgesWms = L.tileLayer.wms(wmsUrl, {
      ...wmsCommon, layers: `${ws}:walk_edges`, pane: "network", minZoom: 14 });
    const buildingsWms = L.tileLayer.wms(wmsUrl, {
      ...wmsCommon, layers: `${ws}:building_access`, pane: "buildings", opacity: 0.85 });

    buildingsMvt = makeBuildingsMvt().addTo(map);

    layerControl = L.control.layers(
      { "OpenStreetMap": osm, "CARTO light": light },
      {
        "Study area (WMS)": studyWms,
        "Service areas 5/10/15 min (WMS + CQL)": serviceWms,
        "Buildings by walking time (pg_tileserv MVT)": buildingsMvt,
        "Buildings by walking time (WMS, all @4.8)": buildingsWms,
        "Elderly hospital-access gaps (WMS view)": gapsWms,
        "Walk network (WMS)": edgesWms,
        "Origin": originLayer.addTo(map),
        "Nearest / isochrone result": resultLayer.addTo(map),
        "WFS hospitals": wfsLayer.addTo(map),
      },
      { collapsed: true }).addTo(map);
    L.control.scale({ metric: true, imperial: false }).addTo(map);

    if (cfg.bbox) {
      const [minx, miny, maxx, maxy] = cfg.bbox;
      map.fitBounds([[miny, minx], [maxy, maxx]], { padding: [20, 20] });
    }

    map.on("click", (e) => {
      if (Date.now() - state.lastVectorClick < 300) return;  // click was on a building tile
      setOrigin(e.latlng.lat, e.latlng.lng, "Map click");
    });
  }

  function serviceCql() {
    return `facility_set='${state.set}' AND speed_kmh=${state.speed}`;
  }

  function mvtUrl() {
    return `${state.cfg.tileserv_url}/public.building_walk_time/{z}/{x}/{y}.pbf` +
           `?facility_set=${encodeURIComponent(state.set)}&speed_kmh=${state.speed}`;
  }

  function makeBuildingsMvt() {
    const layer = L.vectorGrid.protobuf(mvtUrl(), {
      pane: "buildings",
      interactive: true,
      maxNativeZoom: 20,
      maxZoom: 20,
      getFeatureId: (f) => f.properties.id,
      vectorTileLayerStyles: {
        building_walk_time: (props) => ({
          fill: true, weight: 0.4, color: "#374151",
          fillColor: BAND_COLORS[props.band] || "#6b7280", fillOpacity: 0.85,
        }),
      },
    });
    layer.on("click", (e) => {
      state.lastVectorClick = Date.now();
      const p = e.layer.properties;
      L.popup().setLatLng(e.latlng).setContent(
        `<div class="popup"><h3>${esc(p.name || "Building")}</h3><table>` +
        `<tr><td>Type</td><td>${esc(p.building)}</td></tr>` +
        `<tr><td>Facility set</td><td>${esc(p.facility_set)}</td></tr>` +
        `<tr><td>Network distance</td><td>${esc(p.dist_m)} m</td></tr>` +
        `<tr><td>Walk @ ${state.speed} km/h</td><td><b>${esc(p.walk_min)} min</b> (${esc(p.band)})</td></tr>` +
        `</table></div>`).openOn(map);
    });
    return layer;
  }

  function refreshScenario() {
    serviceWms.setParams({ CQL_FILTER: serviceCql() });
    const wasOn = map.hasLayer(buildingsMvt);
    layerControl.removeLayer(buildingsMvt);
    map.removeLayer(buildingsMvt);
    buildingsMvt = makeBuildingsMvt();
    layerControl.addOverlay(buildingsMvt, "Buildings by walking time (pg_tileserv MVT)");
    if (wasOn) buildingsMvt.addTo(map);
    renderCoverage();
  }

  // ------------------------------------------------------- facilities layer
  async function loadFacilities() {
    const url = `${state.cfg.featureserv_url}/collections/public.facility_access_4326/items.json?limit=1000`;
    const fc = await getJSON(url);
    facilitiesLayer = L.geoJSON(fc, {
      pointToLayer: (f, latlng) => L.circleMarker(latlng, {
        radius: f.properties.type === "hospital" ? 7 : 5,
        color: "#ffffff", weight: 1.5, fillOpacity: 0.95,
        fillColor: TYPE_COLORS[f.properties.type] || "#6b7280" }),
      onEachFeature: (f, lyr) => {
        const p = f.properties;
        lyr.bindPopup(() =>
          `<div class="popup"><h3>${esc(p.name || "(unnamed)")}</h3><table>` +
          `<tr><td>Type</td><td>${esc(p.type)}</td></tr>` +
          `<tr><td>To nearest hospital</td><td>${esc(p.hospital_dist_m)} m · ` +
          `${esc(p.hospital_t4p8_min)} min @4.8 · ${esc(p.hospital_t3p6_min)} min @3.6</td></tr>` +
          `<tr><td>15-min catchment (all)</td><td>${esc(p.all_catch15_4p8 ?? "–")} buildings</td></tr>` +
          `<tr><td>Snap to network</td><td>${esc(p.snap_m)} m</td></tr>` +
          `<tr><td>OSM</td><td>${esc(p.osm_type)} ${esc(p.osm_id)}</td></tr></table></div>`);
      },
    }).addTo(map);
    layerControl.addOverlay(facilitiesLayer, `Healthcare facilities (pg_featureserv, ${fc.features.length})`);
    return fc.features.length;
  }

  // ---------------------------------------------------------------- origin
  function setOrigin(lat, lon, label) {
    state.origin = { lat, lon };
    originLayer.clearLayers();
    L.marker([lat, lon], { title: label }).addTo(originLayer)
      .bindTooltip(esc(label), { permanent: false });
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
        li.onclick = () => { setOrigin(r.lat, r.lon, r.name.split(",")[0]); map.setView([r.lat, r.lon], 16); };
        list.appendChild(li);
      });
    } catch (err) {
      list.innerHTML = `<li class="error">${esc(err.message)}</li>`;
    }
  }

  // ----------------------------------------------------- network analysis
  async function onNearest() {
    if (!state.origin) return;
    const { lat, lon } = state.origin;
    const list = $("nearestList");
    list.innerHTML = "<li>Routing on the walk network…</li>";
    try {
      const res = await getJSON(`/api/nearest?lat=${lat}&lon=${lon}&facility_set=${state.set}` +
                                `&speed_kmh=${state.speed}&k=5`);
      resultLayer.clearLayers();
      list.innerHTML = "";
      const features = res.results.features;
      if (res.route) {
        L.geoJSON(res.route, { style: { color: "#1d4ed8", weight: 5, opacity: 0.85 } }).addTo(resultLayer);
      } else if (features.length) {
        const [flon, flat] = features[0].geometry.coordinates;
        L.polyline([[lat, lon], [flat, flon]], { color: "#1d4ed8", weight: 4, dashArray: "6 6" }).addTo(resultLayer);
      }
      features.forEach((f, i) => {
        const p = f.properties;
        const [flon, flat] = f.geometry.coordinates;
        L.circleMarker([flat, flon], { radius: i === 0 ? 10 : 8, color: "#1d4ed8", weight: 3, fill: false })
          .addTo(resultLayer);
        const li = document.createElement("li");
        li.innerHTML = `<b>${esc(p.name || "(unnamed)")}</b> (${esc(p.type)}) — ` +
          `${p.walk_min} min · ${Math.round(p.network_m)} m walk vs ${Math.round(p.straight_m)} m straight` +
          (p.detour_ratio ? ` · detour ×${p.detour_ratio}` : "");
        li.onclick = () => map.setView([flat, flon], 17);
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
      const iso = await getJSON(`/api/isochrone?lat=${lat}&lon=${lon}&minutes=${state.minutes}` +
                                `&speed_kmh=${state.speed}`);
      resultLayer.clearLayers();
      const lyr = L.geoJSON(iso, { style: { color: "#7c3aed", weight: 2, fillColor: "#a78bfa", fillOpacity: 0.3 } })
        .addTo(resultLayer);
      map.fitBounds(lyr.getBounds(), { padding: [30, 30] });
      const p = iso.properties;
      $("isoInfo").innerHTML = `<b>${p.minutes} min @ ${p.speed_kmh} km/h</b> (${Math.round(p.distance_budget_m)} m budget): ` +
        `${p.area_km2} km², ${p.buildings} buildings, ${p.facilities} facilities (${p.hospitals} hospital-tagged). ` +
        `<span class="hint">${esc(p.method)}; ${p.elapsed_ms} ms</span>`;
    } catch (err) {
      $("isoInfo").innerHTML = `<span class="error">${esc(err.message)}</span>`;
    }
  }

  // --------------------------------------------------------------- WFS demo
  async function onWfsHospitals() {
    const params = new URLSearchParams({
      service: "WFS", version: "2.0.0", request: "GetFeature",
      typeNames: `${state.cfg.workspace}:healthcare`, outputFormat: "application/json",
      CQL_FILTER: "type='hospital'" });
    $("wfsInfo").textContent = "Requesting WFS…";
    try {
      const fc = await getJSON(`${state.cfg.geoserver_proxy_wfs_url}?${params}`);
      wfsLayer.clearLayers();
      L.geoJSON(fc, {
        pointToLayer: (f, latlng) => L.circleMarker(latlng, { radius: 11, color: "#dc2626", weight: 2, fill: false }),
        onEachFeature: (f, lyr) => lyr.bindTooltip(esc(f.properties.name || "(unnamed)")),
      }).addTo(wfsLayer);
      $("wfsInfo").textContent = `WFS 2.0 GetFeature, CQL_FILTER type='hospital': ${fc.features.length} features ` +
        `(GeoServer → API proxy → browser).`;
    } catch (err) {
      $("wfsInfo").innerHTML = `<span class="error">${esc(err.message)}</span>`;
    }
  }

  // --------------------------------------------------------------- coverage
  let coverageRows = [];
  function renderCoverage() {
    const tbody = $("coverageTable").querySelector("tbody");
    tbody.innerHTML = "";
    [4.8, 3.6].forEach((sp) => {
      const rows = coverageRows.filter((r) => r.facility_set === state.set && Number(r.speed_kmh) === sp);
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

  // ------------------------------------------------------------------- boot
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
    initMap(state.cfg);

    $("facilitySet").onchange = (e) => { state.set = e.target.value; refreshScenario(); };
    $("speed").onchange = (e) => { state.speed = Number(e.target.value); refreshScenario(); };
    $("minutes").onchange = (e) => { state.minutes = Number(e.target.value); };
    $("searchForm").onsubmit = onSearch;
    $("btnNearest").onclick = onNearest;
    $("btnIsochrone").onclick = onIsochrone;
    $("btnWfsHospitals").onclick = onWfsHospitals;
    $("btnClear").onclick = () => {
      resultLayer.clearLayers(); originLayer.clearLayers(); wfsLayer.clearLayers();
      state.origin = null;
      $("btnNearest").disabled = true; $("btnIsochrone").disabled = true;
      $("nearestList").innerHTML = ""; $("isoInfo").textContent = ""; $("wfsInfo").textContent = "";
      $("originInfo").textContent = "No origin selected.";
    };

    const messages = [];
    try {
      coverageRows = await getJSON("/api/stats/coverage");
      renderCoverage();
    } catch (err) { messages.push(`coverage: ${err.message}`); }
    try {
      const n = await loadFacilities();
      messages.push(`${n} facilities from pg_featureserv`);
    } catch (err) { messages.push(`pg_featureserv: ${err.message}`); }
    setStatus(`Ready — ${messages.join(" · ")}`, messages.some((m) => m.includes(":")));
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }
})();
