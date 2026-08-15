(() => {
  const KIOSK = location.pathname === "/kiosk";
  if (KIOSK) document.body.classList.add("kiosk");

  const REFRESH_MS = 5000;
  const STATUS_COLOR = {
    up: "#3ddeb0",
    healthy: "#3ddeb0",
    degraded: "#f5c14a",
    down: "#ff5d6c",
    unreachable: "#ff5d6c",
    unknown: "#6b7385",
  };
  const CDN_MAP = {
    tile_url: "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
    tile_subdomains: "abcd",
    tile_attribution: "&copy; <a href=\"https://www.openstreetmap.org/copyright\">OpenStreetMap</a> &copy; <a href=\"https://carto.com/attributions\">CARTO</a>",
    min_zoom: 3,
    max_zoom: 18,
  };
  const SITE_ZOOM = 10;

  let state = null;
  let selected = { type: null, id: null };
  let currentView = KIOSK ? "map" : (location.hash.replace("#", "") || "map");
  let leafletMap = null;
  let tileLayer = null;
  let overlay = null;
  let didFit = false;
  let lastTileUrl = "";
  let editDirty = false;
  let newSiteCityId = null;
  let selectedDeviceId = null;
  let selectedFlowId = null;
  let selectedInvIds = new Set();
  let selectedLinkIds = new Set();
  let lastInvCheck = null;
  let lastLinkCheck = null;
  let bulkInvOpen = false;
  let bulkLinkOpen = false;
  let creatingDevice = false;
  let creatingFlow = false;
  let mapEdit = null;
  let portReturnDeviceId = null;

  const $ = (id) => document.getElementById(id);

  function badge(status) {
    const label = status || "unknown";
    return `<span class="badge ${label}"><span class="dot"></span>${label}</span>`;
  }

  function count(map, key) {
    return (map && map[key]) || 0;
  }

  function fmtTime(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString();
  }

  function confirmLeaveForm() {
    if (!editDirty) return true;
    return window.confirm("Discard unsaved changes?");
  }

  function openSiteEditor(siteId, cityId) {
    if (KIOSK) return;
    if (!confirmLeaveForm()) return;
    newSiteCityId = siteId ? null : cityId;
    mapEdit = { kind: "sites", id: siteId || null };
    editDirty = false;
    fillMapEditor();
  }

  function openCityEditor(cityId) {
    if (KIOSK) return;
    if (!confirmLeaveForm()) return;
    mapEdit = { kind: "cities", id: cityId || null };
    editDirty = false;
    fillMapEditor();
  }

  async function deleteCitySite(siteId) {
    const site = (state.sites || []).find((s) => s.id === siteId);
    const label = site ? site.name : "this site";
    if (!window.confirm(`Delete ${label}? Devices at the site must be moved or deleted first.`)) return;
    try {
      await apiSend("DELETE", `/api/sites/${siteId}`);
      await refresh();
    } catch (err) {
      window.alert(err.message);
    }
  }

  function setView(name) {
    if (name === "edit") name = "inventory";
    if (name !== currentView && !confirmLeaveForm()) return;
    if (name !== currentView) editDirty = false;
    currentView = name;
    document.querySelectorAll(".view").forEach((el) => {
      el.classList.toggle("active", el.id === `view-${name}`);
    });
    document.querySelectorAll(".tabs button").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.view === name);
    });
    if (!KIOSK) history.replaceState(null, "", `#${name}`);
    if (name === "map" && leafletMap) {
      setTimeout(() => leafletMap.invalidateSize(), 0);
    }
  }

  function renderSummary(s) {
    const d = s.devices_by_status || {};
    const flows = s.flows_by_status || s.signals_by_status || {};
    const flowCount = s.flows != null ? s.flows : s.signals;
    $("summary").innerHTML = `
      <span class="pill ok"><b>${count(d, "healthy")}</b> devices up</span>
      <span class="pill warn"><b>${count(d, "degraded")}</b> degraded</span>
      <span class="pill bad"><b>${count(d, "unreachable")}</b> down</span>
      <span class="pill unknown"><b>${flowCount}</b> flows · <b>${count(flows, "down")}</b> down</span>
    `;
  }

  function mapNodes() {
    const cities = state.cities || [];
    if (cities.length) return cities.filter((c) => c.lat != null && c.lng != null);
    return (state.sites || []).filter((s) => s.lat != null && s.lng != null);
  }

  function mapZoom() {
    return leafletMap ? leafletMap.getZoom() : 0;
  }

  function showingSitePins() {
    return mapZoom() >= SITE_ZOOM;
  }

  function addLabeledPin(lat, lng, name, sub, status, kind, id, zIndex) {
    const sel = selected.type === kind && String(selected.id) === String(id) ? " sel" : "";
    const marker = L.marker([lat, lng], {
      zIndexOffset: zIndex || 400,
      icon: L.divIcon({
        className: `city-marker ${status || "unknown"}${sel}`,
        html: `<div class="pin-dot"></div><div class="city-pill"><span class="pill-bar"></span><div class="pill-text"><div class="site-label">${escapeHtml(name)}</div><div class="site-sub">${escapeHtml(sub)}</div></div></div>`,
        iconSize: [240, 48],
        iconAnchor: [6, 24],
      }),
    });
    marker.on("click", (ev) => stopSelect(ev, kind, id));
    overlay.addLayer(marker);
  }

  function sitePinLatLng(site, city, index, count) {
    if (site.lat == null || site.lng == null) return null;
    if (showingSitePins() || !city || city.lat == null) return [site.lat, site.lng];
    const sameSpot = Math.hypot(site.lat - city.lat, site.lng - city.lng) < 0.05;
    if (!sameSpot) return [site.lat, site.lng];
    const n = Math.max(count, 1);
    return [
      city.lat + Math.cos((index / n) * Math.PI * 2 - Math.PI / 2) * 0.18,
      city.lng + Math.sin((index / n) * Math.PI * 2 - Math.PI / 2) * 0.18,
    ];
  }

  function drawHopLine(lat1, lng1, lat2, lng2, flowCount, status, id, index) {
    const bulge = (Math.hypot(lat2 - lat1, lng2 - lng1) < 0.08 ? 0.04 : 1.15)
      + (index % 3) * 0.25;
    const pts = hopLatLngs(lat1, lng1, lat2, lng2, bulge);
    const width = 3 + Math.min(flowCount || 1, 10) * 1.1;
    const dim = selected.type === "hop" && selected.id !== id;
    const sel = selected.type === "hop" && selected.id === id;
    const color = STATUS_COLOR[status] || STATUS_COLOR.unknown;
    const hit = L.polyline(pts, { color: "#000", weight: 20, opacity: 0, interactive: true });
    const line = L.polyline(pts, {
      color,
      weight: width,
      opacity: dim ? 0.22 : 0.95,
      lineCap: "round",
      interactive: true,
    });
    if (sel) line.setStyle({ weight: width + 1.6 });
    if (id) {
      hit.on("click", (ev) => stopSelect(ev, "hop", id));
      line.on("click", (ev) => stopSelect(ev, "hop", id));
    }
    overlay.addLayer(hit);
    overlay.addLayer(line);
  }

  function intraCityHops() {
    const sites = new Map((state.sites || []).map((s) => [s.id, s]));
    const buckets = new Map();
    (state.flows || []).forEach((f) => {
      const a = f.source_site_id;
      const b = f.dest_site_id;
      if (!a || !b || a === b) return;
      const sa = sites.get(a);
      const sb = sites.get(b);
      if (!sa || !sb || sa.lat == null || sb.lat == null) return;
      if ((sa.city_id || sa.city_name) !== (sb.city_id || sb.city_name)) return;
      const key = a < b ? `${a}:${b}` : `${b}:${a}`;
      const bucket = buckets.get(key) || {
        id: `site:${key}`,
        source_lat: sa.lat,
        source_lng: sa.lng,
        dest_lat: sb.lat,
        dest_lng: sb.lng,
        flow_count: 0,
        statuses: [],
      };
      bucket.flow_count += 1;
      bucket.statuses.push(f.effective_status || f.status);
      buckets.set(key, bucket);
    });
    return [...buckets.values()].map((h) => {
      const rank = { down: 3, unreachable: 3, degraded: 2, up: 1, healthy: 1, unknown: 0 };
      h.status = h.statuses.reduce((worst, s) =>
        (rank[s] || 0) > (rank[worst] || 0) ? s : worst, "unknown");
      return h;
    });
  }

  function mapSettings() {
    const cfg = (state && state.map) || {};
    return {
      tile_url: cfg.tile_url || CDN_MAP.tile_url,
      tile_subdomains: cfg.tile_subdomains || CDN_MAP.tile_subdomains,
      tile_attribution: cfg.tile_attribution || CDN_MAP.tile_attribution,
      min_zoom: cfg.min_zoom != null ? cfg.min_zoom : CDN_MAP.min_zoom,
      max_zoom: cfg.max_zoom != null ? cfg.max_zoom : CDN_MAP.max_zoom,
    };
  }

  function quadPoint(t, p0, c, p1) {
    const u = 1 - t;
    return [
      u * u * p0[0] + 2 * u * t * c[0] + t * t * p1[0],
      u * u * p0[1] + 2 * u * t * c[1] + t * t * p1[1],
    ];
  }

  function hopLatLngs(lat1, lng1, lat2, lng2, bulge) {
    const p0 = [lng1, lat1];
    const p1 = [lng2, lat2];
    const mx = (lng1 + lng2) / 2;
    const my = (lat1 + lat2) / 2;
    const dx = lng2 - lng1;
    const dy = lat2 - lat1;
    const len = Math.hypot(dx, dy) || 1;
    const c = [mx - (dy / len) * bulge, my + (dx / len) * bulge];
    const pts = [];
    for (let t = 0; t <= 1.0001; t += 0.05) {
      const [lng, lat] = quadPoint(Math.min(t, 1), p0, c, p1);
      pts.push([lat, lng]);
    }
    return pts;
  }

  function stopSelect(ev, type, id) {
    L.DomEvent.stopPropagation(ev);
    select(type, id);
  }

  function ensureMap() {
    if (typeof L === "undefined") {
      $("map").textContent = "Leaflet failed to load.";
      return false;
    }
    const cfg = mapSettings();
    if (!leafletMap) {
      leafletMap = L.map("map", {
        zoomControl: true,
        attributionControl: true,
        minZoom: cfg.min_zoom,
        maxZoom: cfg.max_zoom,
        worldCopyJump: true,
        center: [39.8, -98.6],
        zoom: 4,
      });
      leafletMap.on("click", () => select(null, null));
      leafletMap.on("zoomend", () => renderMap({ overlayOnly: true }));
      overlay = L.layerGroup().addTo(leafletMap);
    } else {
      leafletMap.setMinZoom(cfg.min_zoom);
      leafletMap.setMaxZoom(cfg.max_zoom);
    }
    if (!tileLayer || lastTileUrl !== cfg.tile_url) {
      if (tileLayer) leafletMap.removeLayer(tileLayer);
      tileLayer = L.tileLayer(cfg.tile_url, {
        attribution: cfg.tile_attribution,
        subdomains: cfg.tile_subdomains || "abcd",
        minZoom: cfg.min_zoom,
        maxZoom: cfg.max_zoom,
      }).addTo(leafletMap);
      lastTileUrl = cfg.tile_url;
    }
    return true;
  }

  function fitOnce() {
    if (didFit || !leafletMap) return;
    const bounds = L.latLngBounds([[24.5, -125], [49.5, -66]]);
    mapNodes().forEach((n) => bounds.extend([n.lat, n.lng]));
    leafletMap.fitBounds(bounds, { padding: [28, 28], maxZoom: 5 });
    didFit = true;
  }

  function renderMap(opts) {
    const overlayOnly = Boolean(opts && opts.overlayOnly);
    if (!ensureMap()) {
      if (overlayOnly) return;
      if (mapEdit && !KIOSK) {
        if (!editDirty) fillMapEditor();
      } else {
        renderMapPanel();
      }
      return;
    }
    fitOnce();
    overlay.clearLayers();
    const hops = (state.hops || []).filter((h) =>
      h.source_lat != null && h.source_lng != null && h.dest_lat != null && h.dest_lng != null
    );

    hops.forEach((h, i) => {
      drawHopLine(h.source_lat, h.source_lng, h.dest_lat, h.dest_lng,
        h.flow_count, h.status, h.id, i);
    });
    if (showingSitePins()) {
      intraCityHops().forEach((h, i) => {
        drawHopLine(h.source_lat, h.source_lng, h.dest_lat, h.dest_lng,
          h.flow_count, h.status, null, i);
      });
    }

    if (showingSitePins()) {
      (state.sites || []).filter((s) => s.lat != null && s.lng != null).forEach((s) => {
        const sub = `${s.device_count} device${s.device_count === 1 ? "" : "s"}`;
        addLabeledPin(s.lat, s.lng, s.name, sub, s.status, "site", s.id, 500);
      });
    } else {
      mapNodes().forEach((s) => {
        const kind = s.site_ids ? "city" : "site";
        const siteBit = s.site_count != null
          ? `${s.site_count} site${s.site_count === 1 ? "" : "s"} · `
          : "";
        const sub = `${siteBit}${s.device_count} device${s.device_count === 1 ? "" : "s"}`;
        addLabeledPin(s.lat, s.lng, s.name, sub, s.status, kind, s.id, 400);
      });
    }

    if (!showingSitePins() && (selected.type === "city" || selected.type === "site")) {
      const city = selected.type === "city"
        ? (state.cities || []).find((c) => String(c.id) === String(selected.id))
        : (state.cities || []).find((c) => (c.site_ids || []).includes(selected.id));
      const citySites = city
        ? state.sites.filter((s) => (city.site_ids || []).includes(s.id) && s.lat != null)
        : [];
      citySites.forEach((s, i) => {
        const pos = sitePinLatLng(s, city, i, citySites.length);
        if (!pos) return;
        const sub = `${s.device_count} device${s.device_count === 1 ? "" : "s"}`;
        addLabeledPin(pos[0], pos[1], s.name, sub, s.status, "site", s.id, 500);
      });
    }

    if (overlayOnly) return;
    if (mapEdit && !KIOSK) {
      if (!editDirty) fillMapEditor();
      else $("view-map")?.querySelector(".map-wrap")?.classList.add("with-form");
      return;
    }
    $("view-map")?.querySelector(".map-wrap")?.classList.remove("with-form");
    renderMapPanel();
  }

  function select(type, id) {
    if (mapEdit) {
      if (!confirmLeaveForm()) return;
      mapEdit = null;
      editDirty = false;
    }
    if (id == null || type == null) selected = { type: null, id: null };
    else if (type === "site") selected = { type, id: Number(id) };
    else selected = { type, id: String(id) };
    renderMap();
  }

  function isOutputFlow(f) {
    return Boolean(f.origin_device_id) || f.source_port_kind === "sdi_out" || f.source_port_kind === "net";
  }

  function destLine(f) {
    const dest = [f.dest_city_name, f.dest_site_name, f.dest_device_name, f.dest_port_name || f.dest_label]
      .filter(Boolean)
      .filter((v, i, a) => a.indexOf(v) === i)
      .join(" · ");
    return dest || f.dest_display;
  }

  function deviceFlowLines(deviceId) {
    const flows = (state.flows || []).filter((f) =>
      f.source_device_id === deviceId || f.dest_device_id === deviceId
    );
    const inputs = flows.filter((f) => f.source_device_id === deviceId && !isOutputFlow(f));
    const outputs = flows.filter((f) => f.source_device_id === deviceId && isOutputFlow(f));
    const bySignal = new Map();
    inputs.forEach((f) => {
      const key = `${f.source_port_name || ""}|${f.signal_label || f.label}`;
      if (!bySignal.has(key)) bySignal.set(key, []);
      bySignal.get(key).push(f);
    });
    const inputHtml = [...bySignal.entries()].map(([, rows]) => {
      const first = rows[0];
      const dests = rows.map((f) => destLine(f)).join("; ");
      return `${escapeHtml(first.signal_label || first.source_port_name)} → ${escapeHtml(dests)}`;
    });
    const outputHtml = outputs.map((f) => {
      const origin = f.origin_device_name
        ? ` from ${escapeHtml(f.origin_city_name || f.origin_site_name || f.origin_device_name)}${f.origin_port_name ? ` / ${escapeHtml(f.origin_port_name)}` : ""}`
        : "";
      return `${escapeHtml(f.source_port_name || "out")} → ${escapeHtml(destLine(f))}${origin}`;
    });
    return { inputHtml, outputHtml };
  }

  function renderMapPanel() {
    const panel = $("map-panel");
    if (!selected.type) {
      let html = `<p class="muted">Click a city or a trunk. Zoom in to see each building (WDCW, Hamptons, …). One pipe per city pair — thickness is path count, color is worst status.</p>`;
      if (!KIOSK) {
        html += `<p class="form-actions"><button type="button" class="btn" id="map-add-city">Add city</button></p>`;
      }
      if (KIOSK) {
        const bad = (state.flows || []).filter((f) =>
          f.effective_status === "down" || f.effective_status === "degraded"
        );
        html += `<h3>Alarms</h3>` + (bad.length
          ? `<ul class="row-list">${bad.map((f) =>
            `<li>${badge(f.effective_status)}<span>${escapeHtml(f.signal_label || f.source_port_name)} → ${escapeHtml(destLine(f))}</span></li>`
          ).join("")}</ul>`
          : `<p class="muted">No degraded or down flows.</p>`);
      }
      panel.innerHTML = html;
      const addCity = $("map-add-city");
      if (addCity) addCity.addEventListener("click", () => openCityEditor(null));
      return;
    }
    if (selected.type === "city") {
      const city = (state.cities || []).find((c) => String(c.id) === String(selected.id));
      if (!city) return;
      const sites = state.sites.filter((s) => (city.site_ids || []).includes(s.id));
      const hops = (state.hops || []).filter((h) =>
        h.city_a_id === city.id || h.city_b_id === city.id
        || h.source_city_id === city.id || h.dest_city_id === city.id
      );
      const cityDb = cityDbId(city);
      panel.innerHTML = `
        <h2>${escapeHtml(city.name)}</h2>
        <p class="muted">${city.site_count} site${city.site_count === 1 ? "" : "s"} · ${city.device_count} device${city.device_count === 1 ? "" : "s"}</p>
        <p>${badge(city.status)}</p>
        ${KIOSK ? "" : `<p class="form-actions"><button type="button" class="btn" id="city-edit">Edit city</button>
          <button type="button" class="btn" id="city-add-site">Add site</button></p>`}
        <h3>Sites</h3>
        <p class="muted">Each site is a building in this city. Zoom in to see them on the map.</p>
        ${sites.length ? sites.map((site) => {
          const devices = state.devices.filter((d) => d.site_id === site.id);
          const actions = KIOSK ? "" : `<div class="site-actions">
            <button type="button" class="btn" data-edit-site="${site.id}">Edit</button>
            <button type="button" class="btn danger" data-del-site="${site.id}">Delete</button>
          </div>`;
          return `<div class="site-head"><h3><button type="button" class="linkish" data-select-site="${site.id}">${escapeHtml(site.name)}</button></h3>${actions}</div>
            ${devices.length ? `<ul class="row-list">${devices.map((d) => {
              const lines = deviceFlowLines(d.id);
              const extra = [...lines.inputHtml, ...lines.outputHtml].join("<br>");
              return `<li><span>${escapeHtml(d.name)}<br><span class="muted">${escapeHtml(d.mgmt_host || "no management IP")} · ${escapeHtml(d.vendor)} ${escapeHtml(d.model || "")}${extra ? `<br>${extra}` : ""}</span></span>${badge(d.status)}</li>`;
            }).join("")}</ul>` : `<p class="muted">No devices at this site.</p>`}`;
        }).join("") : `<p class="muted">No sites in this city.</p>`}
        <h3>Trunks</h3>
        ${hops.length ? `<ul class="row-list">${hops.map((h) =>
          `<li><button type="button" data-hop="${escapeAttr(h.id)}"><span>${escapeHtml(h.city_a_name || h.source_city_name)} — ${escapeHtml(h.city_b_name || h.dest_city_name)} · ${h.flow_count}</span>${badge(h.status)}</button></li>`
        ).join("")}</ul>` : `<p class="muted">No inter-city trunks from here.</p>`}
      `;
      panel.querySelectorAll("[data-hop]").forEach((btn) => {
        btn.addEventListener("click", () => select("hop", btn.dataset.hop));
      });
      const addSite = $("city-add-site");
      if (addSite) addSite.addEventListener("click", () => openSiteEditor(null, cityDb));
      const editCity = $("city-edit");
      if (editCity) editCity.addEventListener("click", () => openCityEditor(cityDb));
      panel.querySelectorAll("[data-edit-site]").forEach((btn) => {
        btn.addEventListener("click", () => openSiteEditor(Number(btn.dataset.editSite), cityDb));
      });
      panel.querySelectorAll("[data-del-site]").forEach((btn) => {
        btn.addEventListener("click", () => deleteCitySite(Number(btn.dataset.delSite)));
      });
      panel.querySelectorAll("[data-select-site]").forEach((btn) => {
        btn.addEventListener("click", () => select("site", Number(btn.dataset.selectSite)));
      });
      return;
    }
    if (selected.type === "site") {
      const site = state.sites.find((s) => s.id === selected.id);
      if (!site) return;
      const devices = state.devices.filter((d) => d.site_id === site.id);
      const city = (state.cities || []).find((c) => (c.site_ids || []).includes(site.id));
      const cityDb = city ? cityDbId(city) : site.city_id;
      panel.innerHTML = `
        <h2>${escapeHtml(site.name)}</h2>
        <p class="muted">${escapeHtml(site.city_name || site.city || "")} · ${site.device_count} device${site.device_count === 1 ? "" : "s"}</p>
        <p>${badge(site.status)}</p>
        <p class="form-actions">
          ${city ? `<button type="button" class="btn" id="site-back-city">Back to ${escapeHtml(city.name)}</button>` : ""}
          ${KIOSK ? "" : `<button type="button" class="btn" id="site-edit">Edit site</button>`}
        </p>
        <h3>Devices</h3>
        ${devices.length ? `<ul class="row-list">${devices.map((d) => {
          const lines = deviceFlowLines(d.id);
          const extra = [...lines.inputHtml, ...lines.outputHtml].join("<br>");
          return `<li><span>${escapeHtml(d.name)}<br><span class="muted">${escapeHtml(d.mgmt_host || "no management IP")} · ${escapeHtml(d.vendor)} ${escapeHtml(d.model || "")}${extra ? `<br>${extra}` : ""}</span></span>${badge(d.status)}</li>`;
        }).join("")}</ul>` : `<p class="muted">No devices at this site.</p>`}
      `;
      const back = $("site-back-city");
      if (back && city) back.addEventListener("click", () => select("city", city.id));
      const edit = $("site-edit");
      if (edit) edit.addEventListener("click", () => openSiteEditor(site.id, cityDb));
      return;
    }
    const hop = (state.hops || []).find((h) => h.id === selected.id);
    if (!hop) return;
    const ends = new Set([hop.city_a_id, hop.city_b_id, hop.source_city_id, hop.dest_city_id].filter(Boolean));
    const flows = (state.flows || []).filter((f) =>
      ends.has(f.source_city_key) && ends.has(f.dest_city_key) && f.source_city_key !== f.dest_city_key
    );
    const dirs = hop.directions && hop.directions.length
      ? hop.directions
      : [{ source_city_name: hop.source_city_name, dest_city_name: hop.dest_city_name, source_city_id: hop.source_city_id, dest_city_id: hop.dest_city_id }];
    const pathRows = (rows) => rows.length
      ? `<ul class="row-list">${rows.map((f) => {
        const origin = f.origin_device_name
          ? `<br><span class="muted">origin ${escapeHtml(f.origin_city_name || f.origin_device_name)} ${escapeHtml(f.origin_port_name || "")}</span>`
          : "";
        return `<li><span>${escapeHtml(f.signal_label || f.source_port_name)} → ${escapeHtml(destLine(f))}<br><span class="muted">${escapeHtml(f.source_device_name)}${f.dest_device_name ? ` · ${escapeHtml(f.dest_device_name)}` : ""}</span>${origin}</span>${badge(f.effective_status)}</li>`;
      }).join("")}</ul>`
      : `<p class="muted">No paths this direction.</p>`;
    panel.innerHTML = `
      <h2>${escapeHtml(hop.city_a_name || hop.source_city_name)} — ${escapeHtml(hop.city_b_name || hop.dest_city_name)}</h2>
      <p class="muted">${hop.flow_count} path${hop.flow_count === 1 ? "" : "s"} on this trunk</p>
      <p>${badge(hop.status)}</p>
      ${dirs.map((d) => {
        const rows = flows.filter((f) =>
          f.source_city_key === d.source_city_id && f.dest_city_key === d.dest_city_id
        );
        return `<h3>${escapeHtml(d.source_city_name)} → ${escapeHtml(d.dest_city_name)}</h3>${pathRows(rows)}`;
      }).join("")}
    `;
  }

  function uniqueSorted(items, key) {
    const seen = new Set();
    const out = [];
    items.forEach((item) => {
      const value = item[key];
      if (!value || seen.has(value)) return;
      seen.add(value);
      out.push({ name: value });
    });
    out.sort((a, b) => a.name.localeCompare(b.name));
    return out;
  }

  function fillSelect(el, items, allLabel, valueKey, labelKey) {
    const current = el.value;
    el.innerHTML = `<option value="">${allLabel}</option>` + items.map((item) => {
      const value = String(item[valueKey]);
      return `<option value="${escapeAttr(value)}">${escapeHtml(item[labelKey])}</option>`;
    }).join("");
    if ([...el.options].some((o) => o.value === current)) el.value = current;
  }

  function renderLinkFilters() {
    const flows = state.flows || [];
    fillSelect($("link-src-city"), uniqueSorted(flows, "source_city_name").concat(
      (state.cities || []).map((c) => ({ name: c.name }))
    ), "All source cities", "name", "name");
    fillSelect($("link-dest-city"), uniqueSorted(flows, "dest_city_name"), "All dest cities", "name", "name");
    fillSelect($("link-src-site"), state.sites, "All source sites", "name", "name");
    fillSelect($("link-dest-site"), uniqueSorted(flows, "dest_site_name"), "All dest sites", "name", "name");
    fillSelect($("link-src-port"), uniqueSorted(flows, "source_port_name"), "All source ports", "name", "name");
    fillSelect($("link-dest-device"), uniqueSorted(flows, "dest_device_name"), "All dest devices", "name", "name");
  }

  function filteredFlows() {
    const q = ($("link-search")?.value || "").trim().toLowerCase();
    const status = $("link-status")?.value || "";
    const srcCity = $("link-src-city")?.value || "";
    const destCity = $("link-dest-city")?.value || "";
    const srcSite = $("link-src-site")?.value || "";
    const destSite = $("link-dest-site")?.value || "";
    const srcPort = $("link-src-port")?.value || "";
    const destDevice = $("link-dest-device")?.value || "";
    const vendor = $("link-vendor")?.value || "";
    return (state.flows || []).filter((f) => {
      if (status && f.effective_status !== status) return false;
      if (srcCity && f.source_city_name !== srcCity) return false;
      if (destCity && f.dest_city_name !== destCity) return false;
      if (srcSite && f.source_site_name !== srcSite) return false;
      if (destSite && f.dest_site_name !== destSite) return false;
      if (srcPort && f.source_port_name !== srcPort) return false;
      if (destDevice && f.dest_device_name !== destDevice) return false;
      if (vendor && f.source_device_vendor !== vendor) return false;
      if (q) {
        const blob = [
          f.label, f.signal_label, f.source_port_name, f.source_device_name, f.dest_display,
          f.source_city_name, f.dest_city_name, f.dest_site_name, f.dest_device_name,
          f.dest_label, f.origin_device_name, f.direction,
        ].join(" ").toLowerCase();
        if (!blob.includes(q)) return false;
      }
      return true;
    });
  }

  function filteredDevices() {
    const q = ($("inv-search")?.value || "").trim().toLowerCase();
    const site = $("inv-site")?.value || "";
    const vendor = $("inv-vendor")?.value || "";
    return (state.devices || []).filter((d) => {
      if (site && String(d.site_id) !== site) return false;
      if (vendor && d.vendor !== vendor) return false;
      if (!q) return true;
      return `${d.name} ${d.site_name} ${d.vendor} ${d.mgmt_host || ""} ${d.model || ""}`.toLowerCase().includes(q);
    });
  }

  function updateBulkButtons(kind) {
    const ids = kind === "links" ? selectedLinkIds : selectedInvIds;
    const n = ids.size;
    const prefix = kind === "links" ? "link" : "inv";
    const bulk = $(`${prefix}-bulk`);
    const del = $(`${prefix}-bulk-delete`);
    const count = $(`${prefix}-sel-count`);
    if (bulk) bulk.disabled = n === 0;
    if (del) del.disabled = n === 0;
    if (count) count.textContent = n ? `${n} selected` : "";
    const all = $(`${prefix}-check-all`);
    const visible = kind === "links" ? filteredFlows() : filteredDevices();
    if (all) {
      all.checked = visible.length > 0 && visible.every((row) => ids.has(row.id));
      all.indeterminate = n > 0 && !all.checked;
    }
  }

  function toggleRange(kind, id, shiftKey) {
    const ids = kind === "links" ? selectedLinkIds : selectedInvIds;
    const visible = (kind === "links" ? filteredFlows() : filteredDevices()).map((r) => r.id);
    const last = kind === "links" ? lastLinkCheck : lastInvCheck;
    if (shiftKey && last != null && visible.includes(last)) {
      const a = visible.indexOf(last);
      const b = visible.indexOf(id);
      const [lo, hi] = a < b ? [a, b] : [b, a];
      const shouldCheck = !ids.has(id);
      for (let i = lo; i <= hi; i += 1) {
        if (shouldCheck) ids.add(visible[i]);
        else ids.delete(visible[i]);
      }
    } else if (ids.has(id)) {
      ids.delete(id);
    } else {
      ids.add(id);
    }
    if (kind === "links") lastLinkCheck = id;
    else lastInvCheck = id;
  }

  function renderLinks() {
    const rows = filteredFlows();
    $("links-body").innerHTML = rows.map((f) => {
      const sel = Number(selectedFlowId) === Number(f.id) && !creatingFlow && !bulkLinkOpen ? " sel" : "";
      const checked = selectedLinkIds.has(f.id);
      return `
      <tr class="${sel}${checked ? " checked" : ""}" data-id="${f.id}">
        <td class="check"><input type="checkbox" ${checked ? "checked" : ""}></td>
        <td>${badge(f.effective_status)}</td>
        <td>${escapeHtml(f.signal_label || "—")}</td>
        <td>${escapeHtml(f.source_port_name || "—")}</td>
        <td>${escapeHtml(f.source_device_name)}</td>
        <td>${escapeHtml(f.source_city_name || f.source_site_name)}</td>
        <td>${escapeHtml(f.source_site_name)}</td>
        <td>${escapeHtml(f.dest_city_name || "—")}</td>
        <td>${escapeHtml(f.dest_site_name || "—")}</td>
        <td>${escapeHtml(f.dest_device_name || "—")}</td>
        <td>${escapeHtml(f.origin_device_name || "—")}</td>
        <td>${escapeHtml(f.direction || "—")}</td>
      </tr>`;
    }).join("");
    $("links-empty").hidden = rows.length > 0;
    $("links-body").querySelectorAll("tr").forEach((tr) => {
      const id = Number(tr.dataset.id);
      const box = tr.querySelector("input[type=checkbox]");
      box.addEventListener("click", (ev) => {
        ev.stopPropagation();
        toggleRange("links", id, ev.shiftKey);
        renderLinks();
      });
      tr.addEventListener("click", () => {
        if (!confirmLeaveForm()) return;
        creatingFlow = false;
        bulkLinkOpen = false;
        selectedFlowId = id;
        editDirty = false;
        showFlowEditor(selectedFlowId);
        renderLinks();
      });
    });
    updateBulkButtons("links");
  }

  function fillInvFilters() {
    const el = $("inv-site");
    if (!el || !state) return;
    fillSelect(el, state.sites || [], "All sites", "id", "name");
  }

  function renderInventory() {
    const rows = filteredDevices();
    $("inv-body").innerHTML = rows.map((d) => {
      const sel = Number(selectedDeviceId) === Number(d.id) && !creatingDevice && !bulkInvOpen ? " sel" : "";
      const checked = selectedInvIds.has(d.id);
      return `
      <tr class="${sel}${checked ? " checked" : ""}" data-device="${d.id}">
        <td class="check"><input type="checkbox" ${checked ? "checked" : ""}></td>
        <td>${badge(d.status)}</td>
        <td>${escapeHtml(d.name)}</td>
        <td>${escapeHtml(d.site_name)}</td>
        <td>${escapeHtml(d.vendor)}</td>
        <td>${escapeHtml(d.model || "—")}</td>
        <td>${escapeHtml(d.mgmt_host || "—")}</td>
        <td>${escapeHtml(d.resolved_driver || d.driver_override || "—")}</td>
        <td>${escapeHtml(fmtTime(d.last_seen_at))}</td>
      </tr>`;
    }).join("");
    $("inv-empty").hidden = rows.length > 0;
    $("inv-body").querySelectorAll("tr").forEach((tr) => {
      const id = Number(tr.dataset.device);
      const box = tr.querySelector("input[type=checkbox]");
      box.addEventListener("click", (ev) => {
        ev.stopPropagation();
        toggleRange("inventory", id, ev.shiftKey);
        renderInventory();
      });
      tr.addEventListener("click", () => {
        if (!confirmLeaveForm()) return;
        creatingDevice = false;
        bulkInvOpen = false;
        portReturnDeviceId = null;
        selectedDeviceId = id;
        editDirty = false;
        showDevice(selectedDeviceId);
        renderInventory();
      });
    });
    updateBulkButtons("inventory");
  }

  async function showDevice(id) {
    const panel = $("inv-panel");
    if (!panel) return;
    const listed = (state.devices || []).find((x) => x.id === id);
    if (!listed) {
      panel.innerHTML = `<p class="muted">Device not found.</p>`;
      return;
    }
    panel.innerHTML = `<p class="muted">Loading…</p>`;
    let detail = { recent_polls: [], modules: [], ports: [], flows: [], device: listed };
    try {
      const res = await fetch(`/api/devices/${id}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      detail = await res.json();
    } catch (err) {
      panel.innerHTML = `<p class="muted">Could not load device: ${escapeHtml(err.message)}</p>`;
      return;
    }
    if (selectedDeviceId !== id || creatingDevice) return;
    const d = { ...listed, ...(detail.device || {}) };
    panel.innerHTML = deviceForm(d) + deviceHealthHtml(d, detail);
    bindEditorForm(panel, "devices", {
      getId: () => selectedDeviceId,
      setId: (value) => { selectedDeviceId = value; creatingDevice = false; },
      onSaved: async () => {
        await refresh();
        if (selectedDeviceId) showDevice(selectedDeviceId);
      },
      onDeleted: async () => {
        selectedDeviceId = null;
        creatingDevice = false;
        await refresh();
        panel.innerHTML = `<p class="muted">Click a device to edit it. Host and credentials can be filled in later.</p>`;
      },
      onCancel: () => {
        editDirty = false;
        showDevice(id);
      },
    });
    bindDevicePortActions(d.id, detail.ports || []);
  }

  function deviceHealthHtml(d, data) {
    const polls = data.recent_polls || [];
    const modules = data.modules || [];
    const ports = data.ports || [];
    const flows = data.flows || [];
    const pathItem = (f) => {
      const origin = f.origin_device_name
        ? `<br><span class="muted">origin ${escapeHtml(f.origin_city_name || f.origin_device_name)} ${escapeHtml(f.origin_port_name || "")}</span>`
        : "";
      return `<li><span>${escapeHtml(f.signal_label || f.source_port_name || f.label)} → ${escapeHtml(destLine(f))}<br><span class="muted">${escapeHtml(f.source_device_name)}${f.dest_device_name ? ` · ${escapeHtml(f.dest_device_name)}` : ""} · ${escapeHtml(f.direction || "")}</span>${origin}</span>${badge(f.effective_status)}</li>`;
    };
    const originating = flows.filter((f) => f.source_device_id === d.id && !isOutputFlow(f));
    const leaving = flows.filter((f) => f.source_device_id === d.id && isOutputFlow(f));
    const landing = flows.filter((f) => f.dest_device_id === d.id);
    const through = flows.filter((f) =>
      f.origin_device_id === d.id && f.source_device_id !== d.id
    );
    const pathList = (rows, empty) => rows.length
      ? `<ul class="row-list">${rows.map(pathItem).join("")}</ul>`
      : `<p class="muted">${empty}</p>`;
    return `
      <div id="inv-health">
        <p>${badge(d.status)}${d.poll_enabled ? "" : ' <span class="badge unknown"><span class="dot"></span>poll off</span>'}</p>
        <p class="muted">Driver: ${escapeHtml(d.resolved_driver || d.driver_override || "unresolved")}</p>
        ${d.last_error ? `<p class="badge down">${escapeHtml(d.last_error)}</p>` : ""}
        <h3>Ports</h3>
        <p class="form-actions"><button type="button" class="btn" id="inv-add-port">Add port</button></p>
        ${ports.length ? `<ul class="row-list">${ports.map((p) =>
          `<li><span>${escapeHtml(p.name)}<br><span class="muted">${escapeHtml(p.kind)}${p.slot ? ` · slot ${escapeHtml(p.slot)}` : ""}</span></span>
            <button type="button" class="btn" data-edit-port="${p.id}">Edit</button></li>`
        ).join("")}</ul>` : `<p class="muted">None configured yet.</p>`}
        <h3>Paths originating here</h3>
        ${pathList(originating, "No encode/input paths on this device.")}
        <h3>Paths leaving here</h3>
        ${pathList(leaving, "No outbound paths on this device.")}
        <h3>Paths landing here</h3>
        ${pathList(landing, "No paths terminate on this device.")}
        <h3>Paths originated here, leaving elsewhere</h3>
        ${pathList(through, "No foreign-origin handoff from this device.")}
        <h3>Modules</h3>
        ${modules.length ? `<ul class="row-list">${modules.map((m) =>
          `<li><span>${escapeHtml(m.slot)} ${escapeHtml(m.module_type || "")}</span>${badge(m.status)}</li>`
        ).join("")}</ul>` : `<p class="muted">None discovered yet.</p>`}
        <h3>Recent polls</h3>
        ${polls.length ? `<ul class="row-list">${polls.map((p) =>
          `<li><span>${escapeHtml(fmtTime(p.polled_at))}<br><span class="muted">${escapeHtml(p.method)}${p.latency_ms != null ? ` · ${p.latency_ms}ms` : ""}</span></span>${p.success ? badge("up") : badge("down")}</li>`
        ).join("")}</ul>` : `<p class="muted">No poll history. Start the poller.</p>`}
      </div>
    `;
  }

  function bindDevicePortActions(deviceId, _ports) {
    const add = $("inv-add-port");
    if (add) add.addEventListener("click", () => showPortEditor(deviceId, null));
    $("inv-panel")?.querySelectorAll("[data-edit-port]").forEach((btn) => {
      btn.addEventListener("click", () => showPortEditor(deviceId, Number(btn.dataset.editPort)));
    });
  }

  function showPortEditor(deviceId, portId) {
    if (!confirmLeaveForm()) return;
    portReturnDeviceId = deviceId;
    editDirty = false;
    const panel = $("inv-panel");
    const p = portId ? (state.ports || []).find((x) => x.id === portId) : { device_id: deviceId };
    panel.innerHTML = portForm(p || { device_id: deviceId });
    bindEditorForm(panel, "ports", {
      getId: () => portId,
      setId: (value) => { portId = value; },
      onSaved: async () => {
        portReturnDeviceId = null;
        await refresh();
        showDevice(deviceId);
      },
      onDeleted: async () => {
        portReturnDeviceId = null;
        await refresh();
        showDevice(deviceId);
      },
      onCancel: () => {
        portReturnDeviceId = null;
        editDirty = false;
        showDevice(deviceId);
      },
    });
  }

  function showNewDevice() {
    if (!confirmLeaveForm()) return;
    creatingDevice = true;
    selectedDeviceId = null;
    portReturnDeviceId = null;
    editDirty = false;
    $("inv-body")?.querySelectorAll("tr").forEach((r) => r.classList.remove("sel"));
    const panel = $("inv-panel");
    panel.innerHTML = deviceForm(null);
    bindEditorForm(panel, "devices", {
      getId: () => selectedDeviceId,
      setId: (value) => { selectedDeviceId = value; creatingDevice = false; },
      onSaved: async () => {
        await refresh();
        if (selectedDeviceId) showDevice(selectedDeviceId);
      },
      onDeleted: () => {},
      onCancel: () => {
        creatingDevice = false;
        editDirty = false;
        panel.innerHTML = `<p class="muted">Click a device to edit it. Host and credentials can be filled in later.</p>`;
      },
    });
  }

  function showFlowEditor(id) {
    const panel = $("link-panel");
    if (!panel) return;
    const f = id ? (state.flows || []).find((x) => x.id === id) : null;
    panel.innerHTML = flowForm(f || null);
    bindEditorForm(panel, "flows", {
      getId: () => selectedFlowId,
      setId: (value) => { selectedFlowId = value; creatingFlow = false; },
      onSaved: async () => {
        await refresh();
        showFlowEditor(selectedFlowId);
      },
      onDeleted: async () => {
        selectedFlowId = null;
        creatingFlow = false;
        await refresh();
        panel.innerHTML = `<p class="muted">Click a flow to edit, or New flow.</p>`;
      },
      onCancel: () => {
        editDirty = false;
        if (creatingFlow || !selectedFlowId) {
          creatingFlow = false;
          selectedFlowId = null;
          panel.innerHTML = `<p class="muted">Click a flow to edit, or New flow.</p>`;
          renderLinks();
        } else {
          showFlowEditor(selectedFlowId);
        }
      },
    });
    const src = panel.querySelector('[name="source_device_id"]');
    const dst = panel.querySelector('[name="dest_device_id"]');
    const srcPort = panel.querySelector('[name="source_port_id"]');
    const dstPort = panel.querySelector('[name="dest_port_id"]');
    const syncPorts = (selectEl, portEl) => {
      if (!selectEl || !portEl) return;
      const current = portEl.value;
      const opts = portOptions(selectEl.value);
      portEl.innerHTML = opts.map((o) =>
        `<option value="${escapeAttr(o.value)}"${String(o.value) === String(current) ? " selected" : ""}>${escapeHtml(o.label)}</option>`
      ).join("");
    };
    if (src) src.addEventListener("change", () => syncPorts(src, srcPort));
    if (dst) dst.addEventListener("change", () => syncPorts(dst, dstPort));
  }

  function showNewFlow() {
    if (!confirmLeaveForm()) return;
    creatingFlow = true;
    selectedFlowId = null;
    editDirty = false;
    $("links-body")?.querySelectorAll("tr").forEach((r) => r.classList.remove("sel"));
    showFlowEditor(null);
  }

  function credFlag(set) {
    return set
      ? '<span class="cred-flag ok">set</span>'
      : '<span class="cred-flag missing">not set</span>';
  }

  function field(label, name, value, extra = "") {
    return `<label class="${extra}">${label}<input name="${name}" value="${escapeAttr(value ?? "")}"></label>`;
  }

  function selectField(label, name, options, selected, extra = "") {
    const opts = options.map((o) => {
      const val = o.value;
      const sel = String(val) === String(selected ?? "") ? " selected" : "";
      return `<option value="${escapeAttr(val)}"${sel}>${escapeHtml(o.label)}</option>`;
    }).join("");
    return `<label class="${extra}">${label}<select name="${name}">${opts}</select></label>`;
  }

  function blankOption(label) {
    return { value: "", label };
  }

  function cityDbId(c) {
    return c.db_id != null ? c.db_id : c.id;
  }

  function cityOptions() {
    return [blankOption("(none)"), ...(state.cities || []).map((c) => ({ value: cityDbId(c), label: c.name }))];
  }
  function siteOptions() {
    return [blankOption("(none)"), ...(state.sites || []).map((s) => ({ value: s.id, label: s.name }))];
  }
  function deviceOptions() {
    return [blankOption("(none)"), ...(state.devices || []).map((d) => ({ value: d.id, label: d.name }))];
  }
  function portOptions(deviceId) {
    const ports = (state.ports || []).filter((p) => !deviceId || p.device_id === Number(deviceId));
    return [blankOption("(none)"), ...ports.map((p) => ({ value: p.id, label: `${p.device_name ? p.device_name + " · " : ""}${p.name}` }))];
  }

  function formValues(form) {
    const data = {};
    form.querySelectorAll("input, select, textarea").forEach((el) => {
      if (!el.name) return;
      if (el.type === "checkbox") data[el.name] = el.checked;
      else data[el.name] = el.value;
    });
    return data;
  }

  function deviceForm(d) {
    const sites = (state.sites || []).map((s) => ({ value: s.id, label: s.name }));
    const cred = d ? `User ${credFlag(d.api_username_set)} · Pass ${credFlag(d.api_password_set)}` : "Values are stored in nexnoc.env, not config.json.";
    return `
      <form class="form">
        <h2>${d ? escapeHtml(d.name) : "New device"}</h2>
        <p class="muted">${cred}</p>
        <div class="form-grid">
          ${field("Name", "name", d?.name || "", "wide")}
          ${selectField("Site", "site_id", sites, d?.site_id, "wide")}
          ${selectField("Vendor", "vendor", [
            { value: "appear", label: "Appear" },
            { value: "haivision", label: "Haivision" },
            { value: "net_insight", label: "Net Insight" },
            { value: "generic_snmp", label: "Generic SNMP" },
          ], d?.vendor || "haivision")}
          ${field("Model", "model", d?.model || "")}
          ${field("Firmware", "firmware_version", d?.firmware_version || "")}
          ${field("Management IP / host", "mgmt_host", d?.mgmt_host || "", "wide")}
          ${field("SNMP host (if different)", "snmp_host", d?.snmp_host || "")}
          ${selectField("Access", "access_mode", [
            { value: "direct_api", label: "direct_api" },
            { value: "direct_snmp", label: "direct_snmp" },
            { value: "via_nms", label: "via_nms" },
          ], d?.access_mode || "direct_api")}
          ${field("API port", "api_port", d?.api_port ?? 443)}
          ${field("Username env name", "api_username_env", d?.api_username_env || "")}
          ${field("Password env name", "api_password_env", d?.api_password_env || "")}
          <label>Username value (write-only)<input name="api_username" type="text" autocomplete="off" placeholder="${d?.api_username_set ? "saved — type to replace" : "not set yet"}"></label>
          <label>Password value (write-only)<input name="api_password" type="password" autocomplete="new-password" placeholder="${d?.api_password_set ? "saved — type to replace" : "not set yet"}"></label>
          ${selectField("SNMP version", "snmp_version", [
            { value: "1", label: "v1" },
            { value: "2c", label: "v2c" },
            { value: "3", label: "v3" },
          ], d?.snmp_version || "2c")}
          ${field("SNMP port", "snmp_port", d?.snmp_port ?? 161)}
          ${field("Community env name", "snmp_community_env", d?.snmp_community_env || "")}
          <label>Community value (write-only)<input name="snmp_community" type="password" autocomplete="new-password" placeholder="${d?.snmp_community_set ? "saved — type to replace" : "not set yet"}"></label>
          ${field("v3 user env name", "snmp_v3_user_env", d?.snmp_v3_user_env || "")}
          ${selectField("v3 security", "snmp_v3_sec_level", [
            { value: "noAuthNoPriv", label: "noAuthNoPriv" },
            { value: "authNoPriv", label: "authNoPriv" },
            { value: "authPriv", label: "authPriv" },
          ], d?.snmp_v3_sec_level || "authPriv")}
          ${field("v3 auth proto", "snmp_v3_auth_proto", d?.snmp_v3_auth_proto || "SHA")}
          ${field("v3 priv proto", "snmp_v3_priv_proto", d?.snmp_v3_priv_proto || "AES")}
          ${field("v3 auth pass env", "snmp_v3_auth_pass_env", d?.snmp_v3_auth_pass_env || "")}
          ${field("v3 priv pass env", "snmp_v3_priv_pass_env", d?.snmp_v3_priv_pass_env || "")}
          <label>v3 user value (write-only)<input name="snmp_v3_user" type="text" autocomplete="off" placeholder="${d?.snmp_v3_user_set ? "saved — type to replace" : "not set yet"}"></label>
          <label>v3 auth pass (write-only)<input name="snmp_v3_auth_pass" type="password" autocomplete="new-password"></label>
          <label>v3 priv pass (write-only)<input name="snmp_v3_priv_pass" type="password" autocomplete="new-password"></label>
          <label class="wide"><input name="snmp_enabled" type="checkbox"${d?.snmp_enabled ? " checked" : ""}> SNMP GET in addition to API (v1/v2c/v3)</label>
          <label class="wide"><input name="snmp_trap_enabled" type="checkbox"${(!d || d.snmp_trap_enabled) ? " checked" : ""}> Accept SNMP traps from this host</label>
          <label class="wide"><input name="poll_enabled" type="checkbox"${(!d || d.poll_enabled) ? " checked" : ""}> Poll this device</label>
        </div>
        <div class="form-actions">
          <button type="submit" class="btn primary">Save</button>
          ${d ? `<button type="button" class="btn danger edit-delete">Delete</button>` : ""}
          <button type="button" class="btn edit-cancel">Cancel</button>
        </div>
        <p class="form-msg"></p>
      </form>
    `;
  }

  function leaveSelect(label, name, options) {
    return selectField(label, name, [blankOption("Leave unchanged"), ...options], "", "wide");
  }

  function bulkDeviceForm(ids) {
    const n = ids.size;
    const chosen = (state.devices || []).filter((d) => ids.has(d.id));
    const keepDefault = chosen.find((d) => d.mgmt_host) || chosen[0];
    const keepOpts = chosen.map((d) => ({
      value: d.id,
      label: `${d.name}${d.mgmt_host ? ` · ${d.mgmt_host}` : ""}`,
    }));
    const mergeBlock = n > 1 ? `
        <h3>Merge into one device</h3>
        <p class="muted">Ports and paths move onto the kept device. The others are deleted. Same-named ports are combined.</p>
        ${selectField("Keep this device", "merge_into", keepOpts, keepDefault && keepDefault.id, "wide")}
        <p class="form-actions"><button type="button" class="btn" id="bulk-inv-merge">Merge ${n - 1} into kept device</button></p>
    ` : "";
    return `
      <form class="form" id="bulk-inv-form">
        <h2>Bulk edit ${n} device${n === 1 ? "" : "s"}</h2>
        <p class="muted">Empty fields are left alone. Username/password apply to each device's own env names.</p>
        <div class="form-grid">
          ${leaveSelect("Site", "site_id", (state.sites || []).map((s) => ({ value: s.id, label: s.name })))}
          ${leaveSelect("Vendor", "vendor", [
            { value: "appear", label: "Appear" },
            { value: "haivision", label: "Haivision" },
            { value: "net_insight", label: "Net Insight" },
          ])}
          ${field("Model", "model", "")}
          ${leaveSelect("Access", "access_mode", [
            { value: "direct_api", label: "direct_api" },
            { value: "direct_snmp", label: "direct_snmp" },
            { value: "via_nms", label: "via_nms" },
          ])}
          ${leaveSelect("Polling", "poll_enabled", [
            { value: "true", label: "Enable" },
            { value: "false", label: "Disable" },
          ])}
          <label>Username value (write-only)<input name="api_username" type="text" autocomplete="off" placeholder="leave blank to keep"></label>
          <label>Password value (write-only)<input name="api_password" type="password" autocomplete="new-password" placeholder="leave blank to keep"></label>
        </div>
        ${mergeBlock}
        <div class="form-actions">
          <button type="submit" class="btn primary">Apply to ${n}</button>
          <button type="button" class="btn danger" id="bulk-inv-delete">Delete ${n}</button>
          <button type="button" class="btn edit-cancel">Cancel</button>
        </div>
        <p class="form-msg"></p>
      </form>
    `;
  }

  function bulkFlowForm(ids) {
    const n = ids.size;
    return `
      <form class="form" id="bulk-link-form">
        <h2>Bulk edit ${n} flow${n === 1 ? "" : "s"}</h2>
        <p class="muted">Empty fields are left alone.</p>
        <div class="form-grid">
          ${field("Signal", "signal_label", "")}
          ${leaveSelect("Dest city", "dest_city_id", (state.cities || []).map((c) => ({ value: cityDbId(c), label: c.name })))}
          ${leaveSelect("Dest site", "dest_site_id", (state.sites || []).map((s) => ({ value: s.id, label: s.name })))}
          ${leaveSelect("Dest device", "dest_device_id", (state.devices || []).map((d) => ({ value: d.id, label: d.name })))}
          ${leaveSelect("Direction", "direction", [
            { value: "contribution", label: "contribution" },
            { value: "distribution", label: "distribution" },
          ])}
        </div>
        <div class="form-actions">
          <button type="submit" class="btn primary">Apply to ${n}</button>
          <button type="button" class="btn danger" id="bulk-link-delete">Delete ${n}</button>
          <button type="button" class="btn edit-cancel">Cancel</button>
        </div>
        <p class="form-msg"></p>
      </form>
    `;
  }

  function bulkPatchFromForm(form, kind) {
    const data = cleanBody(formValues(form), kind);
    const patch = {};
    Object.keys(data).forEach((key) => {
      const value = data[key];
      if (value === "" || value == null) return;
      patch[key] = value;
    });
    return patch;
  }

  function showBulkEditor(kind) {
    if (!confirmLeaveForm()) return;
    const ids = kind === "links" ? selectedLinkIds : selectedInvIds;
    if (!ids.size) return;
    editDirty = false;
    if (kind === "links") {
      bulkLinkOpen = true;
      creatingFlow = false;
      selectedFlowId = null;
      const panel = $("link-panel");
      panel.innerHTML = bulkFlowForm(ids);
      bindBulkForm(panel, "flows", ids, () => {
        bulkLinkOpen = false;
        panel.innerHTML = `<p class="muted">Click a flow to edit, or New flow. Check rows for bulk edit.</p>`;
        renderLinks();
      });
      renderLinks();
    } else {
      bulkInvOpen = true;
      creatingDevice = false;
      selectedDeviceId = null;
      const panel = $("inv-panel");
      panel.innerHTML = bulkDeviceForm(ids);
      bindBulkForm(panel, "devices", ids, () => {
        bulkInvOpen = false;
        panel.innerHTML = `<p class="muted">Click a device to edit it. Check rows for bulk edit.</p>`;
        renderInventory();
      });
      renderInventory();
    }
  }

  function bindBulkForm(panel, collection, ids, onClose) {
    const form = panel.querySelector("form.form");
    if (!form) return;
    const msg = form.querySelector(".form-msg");
    form.addEventListener("input", () => { editDirty = true; });
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const patch = bulkPatchFromForm(form, collection);
      if (!Object.keys(patch).length) {
        if (msg) {
          msg.className = "form-msg err";
          msg.textContent = "Set at least one field to change.";
        }
        return;
      }
      try {
        const result = await apiSend("POST", `/api/${collection}/bulk`, {
          ids: [...ids],
          patch,
        });
        editDirty = false;
        const errN = (result.errors || []).length;
        if (msg) {
          msg.className = errN ? "form-msg err" : "form-msg ok";
          msg.textContent = errN
            ? `Updated ${result.updated.length}; ${errN} failed.`
            : `Updated ${result.updated.length}.`;
        }
        await refresh();
        if (!errN) onClose();
      } catch (err) {
        if (msg) {
          msg.className = "form-msg err";
          msg.textContent = err.message;
        }
      }
    });
    const del = form.querySelector("#bulk-inv-delete, #bulk-link-delete");
    if (del) {
      del.addEventListener("click", async () => {
        await bulkDelete(collection, ids, onClose, msg);
      });
    }
    const mergeBtn = form.querySelector("#bulk-inv-merge");
    if (mergeBtn) {
      mergeBtn.addEventListener("click", async () => {
        const keep = Number(form.querySelector('[name="merge_into"]')?.value);
        if (!keep) return;
        const sources = [...ids].filter((id) => id !== keep);
        const keepDev = (state.devices || []).find((d) => d.id === keep);
        const label = keepDev ? keepDev.name : `device ${keep}`;
        if (!window.confirm(
          `Merge ${sources.length} device${sources.length === 1 ? "" : "s"} into ${label}? Ports and paths move; the others are deleted.`
        )) return;
        try {
          const result = await apiSend("POST", "/api/devices/bulk", {
            ids: [...ids],
            merge_into: keep,
          });
          editDirty = false;
          const errN = (result.errors || []).length;
          selectedInvIds = new Set(errN ? result.errors.map((e) => e.id).concat([keep]) : [keep]);
          await refresh();
          if (errN && msg) {
            msg.className = "form-msg err";
            msg.textContent = `Merged ${result.merged.length}; ${errN} failed.`;
            return;
          }
          bulkInvOpen = false;
          selectedDeviceId = keep;
          showDevice(keep);
          renderInventory();
        } catch (err) {
          if (msg) {
            msg.className = "form-msg err";
            msg.textContent = err.message;
          }
        }
      });
    }
    const cancel = form.querySelector(".edit-cancel");
    if (cancel) {
      cancel.addEventListener("click", () => {
        if (!confirmLeaveForm()) return;
        editDirty = false;
        onClose();
      });
    }
  }

  async function bulkDelete(collection, ids, onClose, msg) {
    const n = ids.size;
    if (!n || !window.confirm(`Delete ${n} ${collection === "devices" ? "device" : "flow"}${n === 1 ? "" : "s"}?`)) {
      return;
    }
    try {
      const result = await apiSend("POST", `/api/${collection}/bulk`, {
        ids: [...ids],
        delete: true,
      });
      editDirty = false;
      if (collection === "devices") selectedInvIds = new Set();
      else selectedLinkIds = new Set();
      await refresh();
      const errN = (result.errors || []).length;
      if (errN && msg) {
        msg.className = "form-msg err";
        msg.textContent = `Deleted ${result.deleted.length}; ${errN} failed.`;
        return;
      }
      onClose();
    } catch (err) {
      if (msg) {
        msg.className = "form-msg err";
        msg.textContent = err.message;
      } else {
        window.alert(err.message);
      }
    }
  }

  function flowForm(f) {
    return `
      <form class="form">
        <h2>${f ? escapeHtml(f.signal_label || f.label) : "New flow"}</h2>
        <div class="form-grid">
          ${field("Label", "label", f?.label || "")}
          ${field("Signal", "signal_label", f?.signal_label || "")}
          ${selectField("Source device", "source_device_id", deviceOptions().slice(1), f?.source_device_id, "wide")}
          ${selectField("Source port", "source_port_id", portOptions(f?.source_device_id), f?.source_port_id, "wide")}
          ${selectField("Dest city", "dest_city_id", cityOptions(), f?.dest_city_id)}
          ${selectField("Dest site", "dest_site_id", siteOptions(), f?.dest_site_id)}
          ${selectField("Dest device", "dest_device_id", deviceOptions(), f?.dest_device_id, "wide")}
          ${selectField("Dest port", "dest_port_id", portOptions(f?.dest_device_id), f?.dest_port_id, "wide")}
          ${field("Dest label", "dest_label", f?.dest_label || "")}
          ${field("Direction", "direction", f?.direction || "contribution")}
        </div>
        <div class="form-actions">
          <button type="submit" class="btn primary">Save</button>
          ${f ? `<button type="button" class="btn danger edit-delete">Delete</button>` : ""}
          <button type="button" class="btn edit-cancel">Cancel</button>
        </div>
        <p class="form-msg"></p>
      </form>
    `;
  }

  function portForm(p) {
    return `
      <form class="form">
        <h2>${p && p.id ? escapeHtml(p.name) : "New port"}</h2>
        <div class="form-grid">
          ${selectField("Device", "device_id", deviceOptions().slice(1), p?.device_id, "wide")}
          ${field("Name", "name", p?.name || "")}
          ${selectField("Kind", "kind", [
            { value: "sdi_in", label: "sdi_in" },
            { value: "sdi_out", label: "sdi_out" },
            { value: "net", label: "net" },
            { value: "mgmt", label: "mgmt" },
            { value: "other", label: "other" },
          ], p?.kind || "sdi_in")}
          ${field("Slot", "slot", p?.slot || "")}
        </div>
        <div class="form-actions">
          <button type="submit" class="btn primary">Save</button>
          ${p && p.id ? `<button type="button" class="btn danger edit-delete">Delete</button>` : ""}
          <button type="button" class="btn edit-cancel">Cancel</button>
        </div>
        <p class="form-msg"></p>
      </form>
    `;
  }

  function siteForm(s) {
    const cityId = s?.city_id || newSiteCityId || "";
    const city = (state.cities || []).find((c) => String(cityDbId(c)) === String(cityId));
    const title = s
      ? escapeHtml(s.name)
      : (city ? `New site in ${escapeHtml(city.name)}` : "New site");
    return `
      <form class="form">
        <h2>${title}</h2>
        <p class="muted">A city can have many sites (different buildings).</p>
        <div class="form-grid">
          ${field("Name", "name", s?.name || "", "wide")}
          ${selectField("City", "city_id", cityOptions(), cityId, "wide")}
          ${field("Latitude", "lat", s?.lat ?? "")}
          ${field("Longitude", "lng", s?.lng ?? "")}
          <label class="wide">Notes<textarea name="notes">${escapeHtml(s?.notes || "")}</textarea></label>
        </div>
        <div class="form-actions">
          <button type="submit" class="btn primary">Save</button>
          ${s ? `<button type="button" class="btn danger edit-delete">Delete</button>` : ""}
          <button type="button" class="btn edit-cancel">Cancel</button>
        </div>
        <p class="form-msg"></p>
      </form>
    `;
  }

  function cityForm(c) {
    return `
      <form class="form">
        <h2>${c ? escapeHtml(c.name) : "New city"}</h2>
        <div class="form-grid">
          ${field("Name", "name", c?.name || "", "wide")}
          ${field("Latitude", "lat", c?.lat ?? "")}
          ${field("Longitude", "lng", c?.lng ?? "")}
          <label class="wide">Notes<textarea name="notes">${escapeHtml(c?.notes || "")}</textarea></label>
        </div>
        <div class="form-actions">
          <button type="submit" class="btn primary">Save</button>
          ${c ? `<button type="button" class="btn danger edit-delete">Delete</button>` : ""}
          <button type="button" class="btn edit-cancel">Cancel</button>
        </div>
        <p class="form-msg"></p>
      </form>
    `;
  }

  function fillMapEditor() {
    const panel = $("map-panel");
    if (!panel || !mapEdit || !state) return;
    $("view-map")?.querySelector(".map-wrap")?.classList.add("with-form");
    if (mapEdit.kind === "sites") {
      const s = mapEdit.id ? (state.sites || []).find((x) => x.id === mapEdit.id) : null;
      panel.innerHTML = siteForm(s || null);
    } else {
      const c = mapEdit.id
        ? (state.cities || []).find((x) => Number(cityDbId(x)) === Number(mapEdit.id))
        : null;
      panel.innerHTML = cityForm(c || null);
    }
    bindEditorForm(panel, mapEdit.kind, {
      getId: () => mapEdit && mapEdit.id,
      setId: (value) => { if (mapEdit) mapEdit.id = value; },
      onSaved: async () => {
        const kind = mapEdit && mapEdit.kind;
        const id = mapEdit && mapEdit.id;
        mapEdit = null;
        newSiteCityId = null;
        await refresh();
        if (kind === "cities" && id) {
          const city = (state.cities || []).find((c) => Number(cityDbId(c)) === Number(id));
          if (city) select("city", city.id);
          else renderMap();
        } else if (kind === "sites" && id) {
          const site = (state.sites || []).find((s) => s.id === id);
          if (site && site.city_key) select("city", site.city_key);
          else renderMap();
        } else {
          renderMap();
        }
      },
      onDeleted: async () => {
        mapEdit = null;
        newSiteCityId = null;
        await refresh();
        select(null, null);
      },
      onCancel: () => {
        mapEdit = null;
        newSiteCityId = null;
        editDirty = false;
        renderMap();
      },
    });
  }

  async function apiSend(method, path, body) {
    const res = await fetch(path, {
      method,
      headers: { "Content-Type": "application/json" },
      body: method === "DELETE" ? undefined : JSON.stringify(body || {}),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  }

  function cleanBody(data, kind) {
    const body = { ...data };
    ["api_username", "api_password", "snmp_community",
      "snmp_v3_user", "snmp_v3_auth_pass", "snmp_v3_priv_pass"].forEach((k) => {
      if (!body[k]) delete body[k];
    });
    ["lat", "lng", "api_port", "snmp_port", "site_id", "city_id", "device_id", "source_device_id",
      "source_port_id", "dest_city_id", "dest_site_id", "dest_device_id", "dest_port_id",
    ].forEach((k) => {
      if (body[k] === "") body[k] = null;
    });
    if (kind === "sites" && body.city_id) {
      const city = (state.cities || []).find((c) => String(cityDbId(c)) === String(body.city_id));
      if (city) body.city = city.name;
    }
    return body;
  }

  function bindEditorForm(panel, kind, hooks) {
    const form = panel.querySelector("form.form");
    if (!form) return;
    const msg = form.querySelector(".form-msg");
    form.addEventListener("input", () => { editDirty = true; });
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      try {
        const itemId = hooks.getId();
        const body = cleanBody(formValues(form), kind);
        const path = itemId ? `/api/${kind}/${itemId}` : `/api/${kind}`;
        const method = itemId ? "PATCH" : "POST";
        const result = await apiSend(method, path, body);
        if (msg) {
          msg.className = "form-msg ok";
          msg.textContent = "Saved.";
        }
        editDirty = false;
        if (!itemId) {
          const created = result.device || result.flow || result.port || result.site || result.city;
          if (created && created.id) hooks.setId(created.id);
        }
        newSiteCityId = null;
        await hooks.onSaved();
      } catch (err) {
        if (msg) {
          msg.className = "form-msg err";
          msg.textContent = err.message;
        }
      }
    });
    const del = form.querySelector(".edit-delete");
    if (del) {
      del.addEventListener("click", async () => {
        const itemId = hooks.getId();
        if (!itemId || !window.confirm("Delete this item?")) return;
        try {
          await apiSend("DELETE", `/api/${kind}/${itemId}`);
          hooks.setId(null);
          editDirty = false;
          await hooks.onDeleted();
        } catch (err) {
          if (msg) {
            msg.className = "form-msg err";
            msg.textContent = err.message;
          }
        }
      });
    }
    const cancel = form.querySelector(".edit-cancel");
    if (cancel && hooks.onCancel) {
      cancel.addEventListener("click", () => {
        if (!confirmLeaveForm()) return;
        editDirty = false;
        hooks.onCancel();
      });
    }
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[ch]));
  }
  function escapeAttr(value) {
    return escapeHtml(value);
  }

  function tickClock() {
    $("clock").textContent = new Date().toLocaleTimeString();
  }

  async function refresh() {
    try {
      const res = await fetch("/api/state");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      state = await res.json();
      renderSummary(state.summary);
      renderMap();
      if (!KIOSK) {
        renderLinkFilters();
        fillInvFilters();
        renderLinks();
        renderInventory();
      }
      $("refresh-status").textContent = `Live · refreshed ${new Date().toLocaleTimeString()}`;
      $("poll-age").textContent = state.latest_poll_at
        ? `Last device poll ${fmtTime(state.latest_poll_at)}`
        : "Poller has not run yet";
    } catch (err) {
      $("refresh-status").textContent = `Disconnected (${err.message})`;
    }
  }

  document.querySelectorAll(".tabs button").forEach((btn) => {
    btn.addEventListener("click", () => setView(btn.dataset.view));
  });
  ["link-search", "link-status", "link-src-city", "link-dest-city", "link-src-site", "link-dest-site", "link-src-port", "link-dest-device", "link-vendor"].forEach((id) => {
    const el = $(id);
    if (el) el.addEventListener("input", renderLinks);
  });
  const invSearch = $("inv-search");
  if (invSearch) invSearch.addEventListener("input", renderInventory);
  ["inv-site", "inv-vendor"].forEach((id) => {
    const el = $(id);
    if (el) el.addEventListener("change", renderInventory);
  });
  const invNew = $("inv-new");
  if (invNew) invNew.addEventListener("click", showNewDevice);
  const linkNew = $("link-new");
  if (linkNew) linkNew.addEventListener("click", showNewFlow);

  function bindSelectVisible(kind, toggle) {
    const ids = kind === "links" ? selectedLinkIds : selectedInvIds;
    const visible = (kind === "links" ? filteredFlows() : filteredDevices()).map((r) => r.id);
    const allOn = visible.length > 0 && visible.every((id) => ids.has(id));
    visible.forEach((id) => {
      if (toggle && allOn) ids.delete(id);
      else ids.add(id);
    });
    if (kind === "links") renderLinks();
    else renderInventory();
  }

  const linkSelectVis = $("link-select-visible");
  if (linkSelectVis) linkSelectVis.addEventListener("click", () => bindSelectVisible("links", false));
  const invSelectVis = $("inv-select-visible");
  if (invSelectVis) invSelectVis.addEventListener("click", () => bindSelectVisible("inventory", false));
  const linkCheckAll = $("link-check-all");
  if (linkCheckAll) linkCheckAll.addEventListener("change", () => bindSelectVisible("links", true));
  const invCheckAll = $("inv-check-all");
  if (invCheckAll) invCheckAll.addEventListener("change", () => bindSelectVisible("inventory", true));
  const linkBulk = $("link-bulk");
  if (linkBulk) linkBulk.addEventListener("click", () => showBulkEditor("links"));
  const invBulk = $("inv-bulk");
  if (invBulk) invBulk.addEventListener("click", () => showBulkEditor("inventory"));
  const linkBulkDel = $("link-bulk-delete");
  if (linkBulkDel) {
    linkBulkDel.addEventListener("click", () => {
      bulkDelete("flows", selectedLinkIds, () => {
        bulkLinkOpen = false;
        $("link-panel").innerHTML = `<p class="muted">Click a flow to edit, or New flow. Check rows for bulk edit.</p>`;
        renderLinks();
      });
    });
  }
  const invBulkDel = $("inv-bulk-delete");
  if (invBulkDel) {
    invBulkDel.addEventListener("click", () => {
      bulkDelete("devices", selectedInvIds, () => {
        bulkInvOpen = false;
        $("inv-panel").innerHTML = `<p class="muted">Click a device to edit it. Check rows for bulk edit.</p>`;
        renderInventory();
      });
    });
  }

  setView(currentView);
  tickClock();
  setInterval(tickClock, 1000);
  refresh();
  setInterval(refresh, REFRESH_MS);
})();
