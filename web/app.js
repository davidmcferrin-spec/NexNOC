(() => {
  const KIOSK = location.pathname === "/kiosk";
  if (KIOSK) document.body.classList.add("kiosk");

  let currentUser = null;
  let adminData = null;

  function canEdit() {
    return !!(currentUser && currentUser.permissions && currentUser.permissions.manage_inventory);
  }
  function canAdmin() {
    return !!(currentUser && currentUser.permissions && currentUser.permissions.manage_users);
  }

  const REFRESH_MS = 5000;
  const REFRESH_FAIL_THRESHOLD = 3;
  const THEME_KEY = "nexnoc.theme";
  const TZ_OVERLAY_KEY = "nexnoc.tzOverlay";
  const MAP_DRAWER_KEY = "nexnoc.mapDrawer";
  const MAP_DRAWER_MIN = 220;
  const MAP_DRAWER_HEIGHT_MIN = 120;
  const MAP_DRAWER_DEFAULT = KIOSK ? 360 : 320;
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
  let hopPerspectiveById = {};
  let currentView = KIOSK ? "map" : "map";
  let ioDeviceId = null;
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
  let portReturnDeviceId = null;
  let setupCityId = null;
  let setupSiteId = null;
  let setupDeviceId = null;
  let setupCreating = null;
  let refreshFails = 0;
  let frozenClockAt = null;
  let jamInFlight = false;

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

  function parseHash() {
    const raw = (location.hash || "").replace("#", "");
    if (raw.startsWith("io:")) {
      return { view: "io", ioId: Number(raw.slice(3)) || null };
    }
    return { view: raw || (KIOSK ? "map" : "map"), ioId: null };
  }
  (() => {
    const parsed = parseHash();
    currentView = parsed.view;
    ioDeviceId = parsed.ioId;
  })();

  function setView(name) {
    if (name === "setup" && !canEdit()) name = "map";
    if (name === "admin" && !canAdmin()) name = "map";
    if (name === "edit") name = "inventory";
    if (typeof name === "string" && name.startsWith("io:")) {
      ioDeviceId = Number(name.slice(3)) || null;
      name = "io";
    }
    if (name !== currentView && !confirmLeaveForm()) return;
    if (name !== currentView) editDirty = false;
    currentView = name;
    document.querySelectorAll(".view").forEach((el) => {
      el.classList.toggle("active", el.id === `view-${name}`);
    });
    document.querySelectorAll(".tabs button").forEach((btn) => {
      const tab = btn.dataset.view;
      btn.classList.toggle("active", tab === name || (name === "io" && tab === "inventory"));
    });
    if (!KIOSK) {
      const hash = name === "io" && ioDeviceId ? `io:${ioDeviceId}` : name;
      history.replaceState(null, "", `#${hash}`);
    }
    if (name === "map" && leafletMap) {
      setTimeout(() => leafletMap.invalidateSize(), 0);
    }
    if (name === "setup" && state) renderSetup(false);
    if (name === "io" && state) renderIoPage();
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

  function addLabeledPin(lat, lng, name, sub, status, kind, id, zIndex, site) {
    const sel = selected.type === kind && String(selected.id) === String(id) ? " sel" : "";
    const pinHtml = (kind === "site" && site && window.NexNOCPins)
      ? `${window.NexNOCPins.markerHtml(site, status, sel.trim() === "sel")}<div class="city-pill"><span class="pill-bar"></span><div class="pill-text"><div class="site-label">${escapeHtml(name)}</div><div class="site-sub">${escapeHtml(sub)}</div></div></div>`
      : `<div class="pin-dot"></div><div class="city-pill"><span class="pill-bar"></span><div class="pill-text"><div class="site-label">${escapeHtml(name)}</div><div class="site-sub">${escapeHtml(sub)}</div></div></div>`;
    const marker = L.marker([lat, lng], {
      zIndexOffset: zIndex || 400,
      icon: L.divIcon({
        className: `city-marker ${kind === "site" ? "site-marker" : ""} ${status || "unknown"}${sel}`,
        html: pinHtml,
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

  function hopDistance(lat1, lng1, lat2, lng2) {
    return Math.hypot((lat2 || 0) - (lat1 || 0), (lng2 || 0) - (lng1 || 0));
  }

  function hopBulge(lat1, lng1, lat2, lng2, siblingIndex) {
    const dist = hopDistance(lat1, lng1, lat2, lng2) || 0.001;
    return Math.min(dist * 0.12, 0.55) + (siblingIndex || 0) * Math.min(dist * 0.05, 0.12);
  }

  function drawHopLine(lat1, lng1, lat2, lng2, flowCount, status, id, index) {
    const bulge = hopBulge(lat1, lng1, lat2, lng2, index);
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

  function hopStatus(statuses) {
    const rank = { down: 3, unreachable: 3, degraded: 2, up: 1, healthy: 1, unknown: 0 };
    return statuses.reduce((worst, s) =>
      (rank[s] || 0) > (rank[worst] || 0) ? s : worst, "unknown");
  }

  function cityHopId(sourceKey, destKey) {
    return (state.hops || []).find((h) =>
      (h.city_a_id === sourceKey && h.city_b_id === destKey)
      || (h.city_a_id === destKey && h.city_b_id === sourceKey)
    )?.id || null;
  }

  function sitePairHops() {
    const sites = new Map((state.sites || []).map((s) => [s.id, s]));
    const buckets = new Map();
    (state.flows || []).forEach((f) => {
      const sa = sites.get(f.source_site_id);
      const sb = f.dest_site_id ? sites.get(f.dest_site_id) : null;
      const aLat = sa?.lat ?? f.source_site_lat;
      const aLng = sa?.lng ?? f.source_site_lng;
      const bLat = sb?.lat ?? f.dest_site_lat;
      const bLng = sb?.lng ?? f.dest_site_lng;
      if (aLat == null || aLng == null || bLat == null || bLng == null) return;
      if (hopDistance(aLat, aLng, bLat, bLng) < 0.0001) return;
      const aKey = f.source_site_id || `src:${f.source_device_id}`;
      const bKey = f.dest_site_id || `dst:${f.dest_city_key || f.dest_label || "x"}`;
      const key = String(aKey) < String(bKey) ? `${aKey}:${bKey}` : `${bKey}:${aKey}`;
      const bucket = buckets.get(key) || {
        id: cityHopId(f.source_city_key, f.dest_city_key),
        source_lat: aLat,
        source_lng: aLng,
        dest_lat: bLat,
        dest_lng: bLng,
        flow_count: 0,
        statuses: [],
      };
      bucket.flow_count += 1;
      bucket.statuses.push(f.effective_status || f.status);
      buckets.set(key, bucket);
    });
    return [...buckets.values()].map((h) => {
      h.status = hopStatus(h.statuses);
      return h;
    });
  }

  function currentTheme() {
    return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
  }

  function themedTileUrl(url) {
    if (currentTheme() === "light") {
      return url
        .replace("/dark_all/", "/light_all/")
        .replace("/dark_nolabels/", "/light_nolabels/");
    }
    return url
      .replace("/light_all/", "/dark_all/")
      .replace("/light_nolabels/", "/dark_nolabels/");
  }

  function syncThemeToggle() {
    const btn = $("theme-toggle");
    if (!btn) return;
    const light = currentTheme() === "light";
    const next = light ? "dark" : "light";
    btn.title = `Switch to ${next} mode`;
    btn.setAttribute("aria-label", `Switch to ${next} mode`);
    btn.setAttribute("aria-pressed", light ? "true" : "false");
  }

  function applyTheme(theme) {
    const next = theme === "light" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem(THEME_KEY, next); } catch (_err) { /* private mode */ }
    syncThemeToggle();
    if (leafletMap) {
      lastTileUrl = "";
      ensureMap();
    }
  }

  function initTheme() {
    let saved = "dark";
    try {
      const raw = localStorage.getItem(THEME_KEY);
      if (raw === "light" || raw === "dark") saved = raw;
    } catch (_err) { /* private mode */ }
    applyTheme(saved);
    $("theme-toggle")?.addEventListener("click", () => {
      applyTheme(currentTheme() === "light" ? "dark" : "light");
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
    const tileUrl = themedTileUrl(cfg.tile_url);
    const mapEl = $("map");
    if (mapEl) mapEl.classList.toggle("local-tiles", cfg.tile_url.startsWith("/tiles/"));
    if (!tileLayer || lastTileUrl !== tileUrl) {
      if (tileLayer) leafletMap.removeLayer(tileLayer);
      tileLayer = L.tileLayer(tileUrl, {
        attribution: cfg.tile_attribution,
        subdomains: cfg.tile_subdomains || "abcd",
        minZoom: cfg.min_zoom,
        maxZoom: cfg.max_zoom,
      }).addTo(leafletMap);
      lastTileUrl = tileUrl;
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
      if (!overlayOnly) renderMapPanel();
      return;
    }
    fitOnce();
    overlay.clearLayers();
    const hops = (state.hops || []).filter((h) =>
      h.source_lat != null && h.source_lng != null && h.dest_lat != null && h.dest_lng != null
    );

    if (showingSitePins()) {
      sitePairHops().forEach((h) => {
        drawHopLine(h.source_lat, h.source_lng, h.dest_lat, h.dest_lng,
          h.flow_count, h.status, h.id, 0);
      });
    } else {
      hops.forEach((h, i) => {
        drawHopLine(h.source_lat, h.source_lng, h.dest_lat, h.dest_lng,
          h.flow_count, h.status, h.id, i);
      });
    }

    if (showingSitePins()) {
      (state.sites || []).filter((s) => s.lat != null && s.lng != null).forEach((s) => {
        const sub = `${s.device_count} device${s.device_count === 1 ? "" : "s"}`;
        addLabeledPin(s.lat, s.lng, s.name, sub, s.status, "site", s.id, 500, s);
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
        addLabeledPin(pos[0], pos[1], s.name, sub, s.status, "site", s.id, 500, s);
      });
    }

    if (overlayOnly) return;
    renderMapPanel();
  }

  function select(type, id) {
    if (id == null || type == null) selected = { type: null, id: null };
    else if (type === "site") selected = { type, id: Number(id) };
    else selected = { type, id: String(id) };
    if (type === "hop") setDrawerOpen(true, true);
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
      panel.innerHTML = `
        <h2>${escapeHtml(city.name)}</h2>
        <p class="muted">${city.site_count} site${city.site_count === 1 ? "" : "s"} · ${city.device_count} device${city.device_count === 1 ? "" : "s"}</p>
        <p>${badge(city.status)}</p>
        <h3>Sites</h3>
        <p class="muted">Each site is a building in this city. Zoom in to see them on the map.</p>
        ${sites.length ? sites.map((site) => {
          const devices = state.devices.filter((d) => d.site_id === site.id);
          return `<div class="site-head"><h3><button type="button" class="linkish" data-select-site="${site.id}">${escapeHtml(site.name)}</button></h3></div>
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
      panel.innerHTML = `
        <h2>${escapeHtml(site.name)}</h2>
        <p class="muted">${escapeHtml(site.city_name || site.city || "")} · ${site.device_count} device${site.device_count === 1 ? "" : "s"}</p>
        <p>${badge(site.status)}</p>
        <p class="form-actions">
          ${city ? `<button type="button" class="btn" id="site-back-city">Back to ${escapeHtml(city.name)}</button>` : ""}
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
      return;
    }
    const hop = (state.hops || []).find((h) => h.id === selected.id);
    if (!hop) return;
    renderHopTrunkPanel(panel, hop);
  }

  function hopEndCities(hop) {
    const seen = new Set();
    const out = [];
    const add = (id, name) => {
      if (!id || seen.has(id)) return;
      seen.add(id);
      out.push({ id, name: name || id });
    };
    add(hop.city_a_id, hop.city_a_name);
    add(hop.city_b_id, hop.city_b_name);
    add(hop.source_city_id, hop.source_city_name);
    add(hop.dest_city_id, hop.dest_city_name);
    return out;
  }

  function trunkFlows(hop) {
    const ends = new Set([hop.city_a_id, hop.city_b_id, hop.source_city_id, hop.dest_city_id].filter(Boolean));
    return (state.flows || []).filter((f) =>
      f.source_city_key && f.dest_city_key
      && ends.has(f.source_city_key) && ends.has(f.dest_city_key)
      && f.source_city_key !== f.dest_city_key
    );
  }

  function trunkDeviceKey(f, cityKey) {
    if (f.source_city_key === cityKey && f.source_device_id) return `d:${f.source_device_id}`;
    if (f.dest_city_key === cityKey && f.dest_device_id) return `d:${f.dest_device_id}`;
    if (f.source_city_key === cityKey) {
      return `s:${f.source_site_id || ""}:${f.source_device_name || "source"}`;
    }
    return `t:${f.dest_site_id || ""}:${f.dest_device_name || f.dest_label || f.dest_display || "dest"}`;
  }

  function cityDeviceCountOnTrunk(flows, cityKey) {
    const keys = new Set();
    flows.forEach((f) => {
      if (f.source_city_key === cityKey || f.dest_city_key === cityKey) {
        keys.add(trunkDeviceKey(f, cityKey));
      }
    });
    return keys.size;
  }

  function defaultHopPerspective(hop, flows) {
    const cities = hopEndCities(hop);
    let best = cities[0] ? cities[0].id : null;
    let bestCount = -1;
    cities.forEach((c) => {
      const n = cityDeviceCountOnTrunk(flows, c.id);
      if (n > bestCount) {
        best = c.id;
        bestCount = n;
      }
    });
    return best;
  }

  function remoteEndpoint(f, outbound) {
    if (outbound) {
      return [f.dest_site_name, f.dest_device_name, f.dest_port_name || f.dest_label]
        .filter(Boolean)
        .filter((v, i, a) => a.indexOf(v) === i)
        .join(" · ") || f.dest_display || f.dest_city_name || "—";
    }
    return [f.source_site_name, f.source_device_name, f.source_port_name]
      .filter(Boolean)
      .filter((v, i, a) => a.indexOf(v) === i)
      .join(" · ") || "—";
  }

  function localPortName(f, outbound) {
    if (outbound) return f.source_port_name || f.signal_label || f.label || "out";
    return f.dest_port_name || f.dest_label || f.signal_label || "in";
  }

  function renderHopTrunkPanel(panel, hop) {
    const scroll = panel.scrollTop;
    const cities = hopEndCities(hop);
    const flows = trunkFlows(hop);
    const valid = new Set(cities.map((c) => c.id));
    let perspective = hopPerspectiveById[hop.id];
    if (!perspective || !valid.has(perspective)) {
      perspective = defaultHopPerspective(hop, flows);
      hopPerspectiveById[hop.id] = perspective;
    }
    const perspectiveCity = cities.find((c) => c.id === perspective) || cities[0];

    const sites = new Map();
    flows.forEach((f) => {
      const outbound = f.source_city_key === perspective;
      const inbound = f.dest_city_key === perspective;
      if (!outbound && !inbound) return;
      const siteId = outbound ? (f.source_site_id || `src:${f.source_device_id}`) : (f.dest_site_id || `dst:${f.dest_device_id || f.dest_label || "x"}`);
      const siteName = outbound ? (f.source_site_name || "Unknown site") : (f.dest_site_name || f.dest_display || "Unknown site");
      const deviceKey = trunkDeviceKey(f, perspective);
      const deviceId = outbound ? f.source_device_id : f.dest_device_id;
      const listed = deviceId ? (state.devices || []).find((d) => d.id === deviceId) : null;
      const deviceName = outbound
        ? (f.source_device_name || listed?.name || "Source")
        : (f.dest_device_name || listed?.name || f.dest_label || f.dest_display || "Destination");
      const deviceStatus = listed?.status || (outbound ? f.source_device_status : "unknown");
      let site = sites.get(String(siteId));
      if (!site) {
        site = { id: siteId, name: siteName, devices: new Map() };
        sites.set(String(siteId), site);
      }
      let device = site.devices.get(deviceKey);
      if (!device) {
        device = { key: deviceKey, id: deviceId, name: deviceName, status: deviceStatus, lines: [] };
        site.devices.set(deviceKey, device);
      }
      const port = localPortName(f, outbound);
      const signal = f.signal_label || f.label || "";
      const arrow = outbound ? "→" : "←";
      const remote = remoteEndpoint(f, outbound);
      const label = signal && signal !== port
        ? `${signal} · ${port} ${arrow} ${remote}`
        : `${port} ${arrow} ${remote}`;
      device.lines.push({
        outbound,
        port,
        label,
        status: f.effective_status || f.status,
      });
    });

    const siteList = [...sites.values()].sort((a, b) => a.name.localeCompare(b.name));
    siteList.forEach((site) => {
      site.deviceList = [...site.devices.values()].sort((a, b) => a.name.localeCompare(b.name));
      site.deviceList.forEach((device) => {
        device.lines.sort((a, b) => {
          if (a.outbound !== b.outbound) return a.outbound ? -1 : 1;
          return a.port.localeCompare(b.port);
        });
      });
    });

    const picker = cities.length
      ? `<div class="hop-perspective" role="tablist" aria-label="Trunk city perspective">
          <span class="muted">From</span>
          ${cities.map((c) =>
            `<button type="button" role="tab" class="hop-perspective-btn${c.id === perspective ? " active" : ""}" data-perspective="${escapeAttr(c.id)}" aria-selected="${c.id === perspective ? "true" : "false"}">${escapeHtml(c.name)}</button>`
          ).join("")}
        </div>`
      : "";

    const body = siteList.length
      ? siteList.map((site) => `
          <div class="site-head"><h3>${escapeHtml(site.name)}</h3></div>
          ${site.deviceList.map((device) => `
            <div class="trunk-device">
              <div class="trunk-device-head">
                <span>${escapeHtml(device.name)}</span>
                ${badge(device.status)}
              </div>
              <ul class="row-list">${device.lines.map((line) =>
                `<li><span>${escapeHtml(line.label)}</span>${badge(line.status)}</li>`
              ).join("")}</ul>
            </div>
          `).join("")}
        `).join("")
      : `<p class="muted">No built paths on this trunk.</p>`;

    panel.innerHTML = `
      <h2>${escapeHtml(hop.city_a_name || hop.source_city_name)} — ${escapeHtml(hop.city_b_name || hop.dest_city_name)}</h2>
      <p class="muted">${flows.length} path${flows.length === 1 ? "" : "s"} on this trunk${perspectiveCity ? ` · ${escapeHtml(perspectiveCity.name)} devices` : ""}</p>
      <p>${badge(hop.status)}</p>
      ${picker}
      ${body}
    `;
    panel.querySelectorAll("[data-perspective]").forEach((btn) => {
      btn.addEventListener("click", () => {
        hopPerspectiveById[hop.id] = btn.dataset.perspective;
        renderHopTrunkPanel(panel, hop);
      });
    });
    panel.scrollTop = scroll;
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
    let detail = { recent_polls: [], recent_traps: [], modules: [], ports: [], flows: [], device: listed };
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
    bindOpenIo(panel, d.id);
  }

  function deviceHealthHtml(d, data) {
    const polls = data.recent_polls || [];
    const modules = data.modules || [];
    const traps = data.recent_traps || [];
    return `
      <div id="inv-health">
        <p>${badge(d.status)}${d.poll_enabled ? "" : ' <span class="badge unknown"><span class="dot"></span>poll off</span>'}</p>
        <p class="muted">Monitor driver: ${escapeHtml(d.resolved_driver || d.driver_override || "unresolved")}${d.control_driver ? ` · control ${escapeHtml(d.control_driver)}` : ""}</p>
        ${driverNotesHtml(d.resolved_driver || d.driver_override, d.vendor)}
        ${d.last_error ? `<p class="badge down">${escapeHtml(d.last_error)}</p>` : ""}
        <h3>Modules</h3>
        ${modules.length ? `<ul class="row-list">${modules.map((m) =>
          `<li><span>${escapeHtml(m.slot)} ${escapeHtml(m.module_type || "")}</span>${badge(m.status)}</li>`
        ).join("")}</ul>` : `<p class="muted">None discovered yet.</p>`}
        <h3>Recent polls</h3>
        ${polls.length ? `<ul class="row-list">${polls.map((p) =>
          `<li><span>${escapeHtml(fmtTime(p.polled_at))}<br><span class="muted">${escapeHtml(p.method)}${p.latency_ms != null ? ` · ${p.latency_ms}ms` : ""}</span></span>${p.success ? badge("up") : badge("down")}</li>`
        ).join("")}</ul>` : `<p class="muted">No poll history. Start the poller.</p>`}
        <h3>SNMP traps</h3>
        ${traps.length ? `<ul class="row-list">${traps.map((t) =>
          `<li><span>${escapeHtml(fmtTime(t.received_at || t.created_at))}<br><span class="muted">${escapeHtml(t.trap_oid || "oid")} · ${escapeHtml(t.source_ip || "")}</span></span>${t.matched ? badge("up") : badge("unknown")}</li>`
        ).join("")}</ul>` : `<p class="muted">None received. nexnoc-trapd listens on UDP 162.</p>`}
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
    const syncPorts = (selectEl, portEl, role) => {
      if (!selectEl || !portEl) return;
      const current = portEl.value;
      const opts = portOptions(selectEl.value, role);
      portEl.innerHTML = opts.map((o) =>
        `<option value="${escapeAttr(o.value)}"${String(o.value) === String(current) ? " selected" : ""}>${escapeHtml(o.label)}</option>`
      ).join("");
    };
    if (src) src.addEventListener("change", () => syncPorts(src, srcPort, "source"));
    if (dst) dst.addEventListener("change", () => syncPorts(dst, dstPort, "dest"));
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
      const title = o.title ? ` title="${escapeAttr(o.title)}"` : "";
      return `<option value="${escapeAttr(val)}"${sel}${title}>${escapeHtml(o.label)}</option>`;
    }).join("");
    return `<label class="${extra}">${label}<select name="${name}">${opts}</select></label>`;
  }

  function firmwareRangeLabel(d) {
    if (!d) return "";
    if (d.firmware_min && d.firmware_max) return `fw ${d.firmware_min}–${d.firmware_max}`;
    if (d.firmware_min) return `fw >= ${d.firmware_min}`;
    if (d.firmware_max) return `fw <= ${d.firmware_max}`;
    return "";
  }

  function driverLookup(id) {
    return (state.drivers || []).find((d) => d.driver_id === id);
  }

  function driverNotesHtml(id, vendor) {
    const row = driverLookup(id)
      || (vendor && (state.drivers || []).find((d) => d.vendor === vendor && d.is_default));
    if (!row || !row.notes) return "";
    return `<p class="muted">${escapeHtml(row.notes)}</p>`;
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
  function driverOptions(vendor) {
    const all = state.drivers || [];
    const rows = vendor ? all.filter((d) => d.vendor === vendor) : all;
    return [
      { value: "", label: "(auto-resolve)", title: "Pick from vendor + model + firmware_version" },
      ...rows.map((d) => ({
        value: d.driver_id,
        label: [
          d.driver_id,
          d.is_default ? "default" : "",
          firmwareRangeLabel(d),
          d.connectors && d.connectors.length ? `${d.connectors.length} BNC` : "",
        ].filter(Boolean).join(" · "),
        title: d.notes || "",
      })),
    ];
  }

  function portOptions(deviceId, role) {
    const ports = (state.ports || []).filter((p) => !deviceId || p.device_id === Number(deviceId));
    const filtered = ports.filter((p) => {
      if (p.kind === "mgmt") return false;
      const cap = p.capability || "";
      const dir = p.direction || "";
      if (role === "source") {
        return true;
      }
      if (role === "dest") {
        if (cap === "input" || dir === "input") return false;
        return true;
      }
      return true;
    });
    return [blankOption("(none)"), ...filtered.map((p) => {
      const dir = p.direction || p.capability || p.kind;
      return { value: p.id, label: `${p.device_name ? p.device_name + " · " : ""}${p.name}${dir ? ` (${dir})` : ""}` };
    })];
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

  function deviceForm(d, defaults = {}) {
    const sites = (state.sites || []).map((s) => ({ value: s.id, label: s.name }));
    const cred = d ? `User ${credFlag(d.api_username_set)} · Pass ${credFlag(d.api_password_set)}` : "Values are stored in nexnoc.env, not config.json.";
    return `
      <form class="form">
        <h2>${d ? escapeHtml(d.name) : "New device"}</h2>
        <p class="muted">${cred}</p>
        <div class="form-grid">
          ${field("Name", "name", d?.name || "", "wide")}
          ${selectField("Site", "site_id", sites, d?.site_id || defaults.site_id, "wide")}
          ${selectField("Vendor", "vendor", [
            { value: "appear", label: "Appear" },
            { value: "haivision", label: "Haivision" },
            { value: "net_insight", label: "Net Insight" },
            { value: "generic_snmp", label: "Generic SNMP" },
          ], d?.vendor || "haivision")}
          ${field("Model", "model", d?.model || "")}
          ${field("Firmware", "firmware_version", d?.firmware_version || "")}
          ${selectField("Monitor driver", "driver_override", driverOptions(d?.vendor), d?.driver_override || "", "wide")}
          ${selectField("Control driver (Phase 4 pin)", "control_driver", driverOptions(d?.vendor), d?.control_driver || "", "wide")}
          <p class="muted wide driver-notes" hidden></p>
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
          <label class="wide"><input name="snmp_enabled" type="checkbox"${(!d || d.snmp_enabled) ? " checked" : ""}> SNMP GET in addition to API (v1/v2c/v3)</label>
          <label class="wide"><input name="snmp_trap_enabled" type="checkbox"${(!d || d.snmp_trap_enabled) ? " checked" : ""}> Accept SNMP traps from this host</label>
          <label class="wide"><input name="poll_enabled" type="checkbox"${(!d || d.poll_enabled) ? " checked" : ""}> Poll this device</label>
        </div>
        <div class="form-actions">
          <button type="submit" class="btn primary">Save</button>
          ${d && d.id ? `<button type="button" class="btn open-io-btn">Open I/O</button>` : ""}
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
    const title = f && f.id ? escapeHtml(f.signal_label || f.label) : "New flow";
    return `
      <form class="form">
        <h2>${title}</h2>
        <div class="form-grid">
          ${field("Label", "label", f?.label || "")}
          ${field("Signal", "signal_label", f?.signal_label || "")}
          ${selectField("Source device", "source_device_id", deviceOptions().slice(1), f?.source_device_id, "wide")}
          ${selectField("Source port", "source_port_id", portOptions(f?.source_device_id, "source"), f?.source_port_id, "wide")}
          ${selectField("Dest city", "dest_city_id", cityOptions(), f?.dest_city_id)}
          ${selectField("Dest site", "dest_site_id", siteOptions(), f?.dest_site_id)}
          ${selectField("Dest device", "dest_device_id", deviceOptions(), f?.dest_device_id, "wide")}
          ${selectField("Dest port", "dest_port_id", portOptions(f?.dest_device_id, "dest"), f?.dest_port_id, "wide")}
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
          ${selectField("Capability", "capability", [
            { value: "", label: "(none)" },
            { value: "input", label: "fixed input" },
            { value: "output", label: "fixed output" },
            { value: "assignable", label: "assignable" },
          ], p?.capability || "")}
          ${selectField("Direction", "direction", [
            { value: "", label: "(unset)" },
            { value: "unused", label: "unused" },
            { value: "input", label: "input" },
            { value: "output", label: "output" },
          ], p?.direction || "")}
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
    const geoNote = s?.geo_source === "manual"
      ? "Coordinates were set by hand; bootstrap will not overwrite them."
      : s?.geo_source === "geocode"
        ? "Coordinates came from the address lookup. Adjust if the pin is off."
        : "Enter a street address (or lat/lng). Address is geocoded when you leave the field.";
    return `
      <form class="form">
        <h2>${title}</h2>
        <p class="muted">A city can have many sites (different buildings). ${geoNote}</p>
        <div class="form-grid">
          ${field("Name", "name", s?.name || "", "wide")}
          ${selectField("City", "city_id", cityOptions(), cityId, "wide")}
          ${field("Street address", "address", s?.address || "", "wide")}
          ${field("Latitude", "lat", s?.lat ?? "")}
          ${field("Longitude", "lng", s?.lng ?? "")}
          <input type="hidden" name="geo_source" value="${escapeAttr(s?.geo_source || "")}">
          <label class="wide">Map pin
            <input type="hidden" name="pin_icon" value="${escapeAttr(s?.pin_icon || "building")}">
            <div class="pin-picker" data-pin-picker></div>
          </label>
          <label>Pin color<input name="pin_color" type="color" value="${escapeAttr(s?.pin_color || "#6aa4ff")}"></label>
          ${s && s.id ? `<label>Custom pin image<input type="file" id="site-pin-file" accept="image/png,image/svg+xml,image/jpeg,image/webp"></label>` : `<p class="muted wide">Save the site first to upload a custom pin.</p>`}
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
    const geoNote = c?.geo_source === "manual"
      ? "Coordinates were set by hand."
      : c?.geo_source === "geocode"
        ? "Coordinates came from the city-name lookup. Adjust if the pin is off."
        : "Name is looked up when you leave the field; you can always type lat/lng instead.";
    return `
      <form class="form">
        <h2>${c ? escapeHtml(c.name) : "New city"}</h2>
        <p class="muted">${geoNote} Delete is refused while sites still belong to this city.</p>
        <div class="form-grid">
          ${field("Name", "name", c?.name || "", "wide")}
          ${field("Latitude", "lat", c?.lat ?? "")}
          ${field("Longitude", "lng", c?.lng ?? "")}
          <input type="hidden" name="geo_source" value="${escapeAttr(c?.geo_source || "")}">
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

  function setupCity() {
    return (state.cities || []).find((c) => Number(cityDbId(c)) === Number(setupCityId));
  }

  function setupSite() {
    return (state.sites || []).find((s) => s.id === setupSiteId);
  }

  function sitesForSetupCity() {
    const city = setupCity();
    if (!city) return [];
    const ids = new Set(city.site_ids || []);
    return (state.sites || []).filter((s) => ids.has(s.id) || Number(s.city_id) === Number(setupCityId));
  }

  function devicesForSetupSite() {
    if (!setupSiteId) return [];
    return (state.devices || []).filter((d) => d.site_id === setupSiteId);
  }

  function renderSetupList(el, rows, emptyText) {
    if (!el) return;
    if (!rows.length) {
      el.innerHTML = `<li class="empty-row">${escapeHtml(emptyText)}</li>`;
      return;
    }
    el.innerHTML = rows.map((row) => `
      <li class="${row.sel ? "sel" : ""}">
        <button type="button" data-id="${escapeAttr(row.id)}">
          ${escapeHtml(row.name)}
          ${row.sub ? `<span class="sub">${escapeHtml(row.sub)}</span>` : ""}
        </button>
      </li>
    `).join("");
  }

  function renderSetupNav() {
    const cities = (state.cities || [])
      .filter((c) => Number.isFinite(Number(cityDbId(c))))
      .sort((a, b) => a.name.localeCompare(b.name));
    renderSetupList($("setup-cities"), cities.map((c) => ({
      id: cityDbId(c),
      name: c.name,
      sub: `${c.site_count || 0} site${c.site_count === 1 ? "" : "s"} · ${c.device_count || 0} device${c.device_count === 1 ? "" : "s"}`,
      sel: Number(cityDbId(c)) === Number(setupCityId) && setupCreating !== "city",
    })), "No cities yet.");

    const addSite = $("setup-add-site");
    if (addSite) addSite.disabled = !setupCityId;
    if (!setupCityId) {
      renderSetupList($("setup-sites"), [], "Select a city.");
    } else {
      renderSetupList($("setup-sites"), sitesForSetupCity().map((s) => ({
        id: s.id,
        name: s.name,
        sub: `${s.device_count || 0} device${s.device_count === 1 ? "" : "s"}`,
        sel: s.id === setupSiteId && setupCreating !== "site",
      })), "No sites in this city.");
    }

    const addDev = $("setup-add-device");
    if (addDev) addDev.disabled = !setupSiteId;
    if (!setupSiteId) {
      renderSetupList($("setup-devices"), [], "Select a site.");
    } else {
      renderSetupList($("setup-devices"), devicesForSetupSite().map((d) => ({
        id: d.id,
        name: d.name,
        sub: `${d.mgmt_host || "no IP"} · ${d.vendor}`,
        sel: d.id === setupDeviceId && setupCreating !== "device",
      })), "No devices at this site.");
    }

    $("setup-cities")?.querySelectorAll("button[data-id]").forEach((btn) => {
      btn.addEventListener("click", () => selectSetupCity(Number(btn.dataset.id)));
    });
    $("setup-sites")?.querySelectorAll("button[data-id]").forEach((btn) => {
      btn.addEventListener("click", () => selectSetupSite(Number(btn.dataset.id)));
    });
    $("setup-devices")?.querySelectorAll("button[data-id]").forEach((btn) => {
      btn.addEventListener("click", () => selectSetupDevice(Number(btn.dataset.id)));
    });
  }

  function selectSetupCity(id) {
    if (!confirmLeaveForm()) return;
    setupCreating = null;
    setupCityId = id;
    setupSiteId = null;
    setupDeviceId = null;
    newSiteCityId = id;
    editDirty = false;
    renderSetup(true);
  }

  function selectSetupSite(id) {
    if (!confirmLeaveForm()) return;
    setupCreating = null;
    setupSiteId = id;
    setupDeviceId = null;
    editDirty = false;
    const site = (state.sites || []).find((s) => s.id === id);
    if (site && site.city_id) setupCityId = site.city_id;
    renderSetup(true);
  }

  function selectSetupDevice(id) {
    if (!confirmLeaveForm()) return;
    setupCreating = null;
    setupDeviceId = id;
    editDirty = false;
    const device = (state.devices || []).find((d) => d.id === id);
    if (device) {
      setupSiteId = device.site_id;
      const site = (state.sites || []).find((s) => s.id === device.site_id);
      if (site && site.city_id) setupCityId = site.city_id;
    }
    renderSetup(true);
  }

  function startSetupCreate(kind) {
    if (kind === "site" && !setupCityId) return;
    if (kind === "device" && !setupSiteId) return;
    if (!confirmLeaveForm()) return;
    setupCreating = kind;
    if (kind === "city") {
      setupSiteId = null;
      setupDeviceId = null;
    } else if (kind === "site") {
      setupDeviceId = null;
      newSiteCityId = setupCityId;
    }
    editDirty = false;
    renderSetup(true);
  }

  function fillSetupPanel() {
    const panel = $("setup-panel");
    if (!panel || !state) return;
    if (setupCreating === "city") {
      panel.innerHTML = cityForm(null);
      bindSetupForm("cities", () => setupCityId, (id) => { setupCityId = id; setupCreating = null; });
      bindGeoAndPins(panel, "cities");
      return;
    }
    if (setupCreating === "site") {
      newSiteCityId = setupCityId;
      panel.innerHTML = siteForm(null);
      bindSetupForm("sites", () => setupSiteId, (id) => { setupSiteId = id; setupCreating = null; });
      bindGeoAndPins(panel, "sites");
      return;
    }
    if (setupCreating === "device") {
      panel.innerHTML = deviceForm(null, { site_id: setupSiteId });
      bindSetupForm("devices", () => setupDeviceId, (id) => { setupDeviceId = id; setupCreating = null; });
      return;
    }
    if (setupDeviceId) {
      const d = (state.devices || []).find((x) => x.id === setupDeviceId);
      panel.innerHTML = d
        ? deviceForm(d)
        : `<p class="muted">Device not found.</p>`;
      if (d) {
        bindSetupForm("devices", () => setupDeviceId, (id) => { setupDeviceId = id; });
        bindOpenIo(panel, d.id);
      }
      return;
    }
    if (setupSiteId) {
      const s = setupSite();
      panel.innerHTML = s
        ? siteForm(s)
        : `<p class="muted">Site not found.</p>`;
      if (s) {
        bindSetupForm("sites", () => setupSiteId, (id) => { setupSiteId = id; });
        bindGeoAndPins(panel, "sites", s.id);
      }
      return;
    }
    if (setupCityId) {
      const c = setupCity();
      panel.innerHTML = c
        ? cityForm(c)
        : `<p class="muted">City not found.</p>`;
      if (c) {
        bindSetupForm("cities", () => setupCityId, (id) => { setupCityId = id; });
        bindGeoAndPins(panel, "cities");
      }
      return;
    }
    panel.innerHTML = `<p class="muted">City, then building, then box. Add a city to start. The map stays for watching paths.</p>`;
  }

  function bindSetupForm(kind, getId, setId) {
    const panel = $("setup-panel");
    bindEditorForm(panel, kind, {
      getId,
      setId,
      onSaved: async () => {
        setupCreating = null;
        await refresh();
        renderSetup(true);
      },
      onDeleted: async () => {
        if (kind === "cities") {
          setupCityId = null;
          setupSiteId = null;
          setupDeviceId = null;
        } else if (kind === "sites") {
          setupSiteId = null;
          setupDeviceId = null;
        } else {
          setupDeviceId = null;
        }
        setupCreating = null;
        await refresh();
        renderSetup(true);
      },
      onCancel: () => {
        setupCreating = null;
        editDirty = false;
        renderSetup(true);
      },
    });
  }

  function renderSetup(forcePanel) {
    if (KIOSK || !$("setup-cities") || !state) return;
    renderSetupNav();
    if (forcePanel || !editDirty) fillSetupPanel();
  }

  async function apiSend(method, path, body) {
    const res = await fetch(path, {
      method,
      headers: { "Content-Type": "application/json" },
      body: (method === "GET" || method === "DELETE") ? undefined : JSON.stringify(body || {}),
    });
    if (res.status === 401 && !KIOSK) {
      location.href = "/?next=" + encodeURIComponent(location.pathname + location.hash);
      throw new Error("login required");
    }
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
    ["driver_override", "control_driver"].forEach((k) => {
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
    if (kind === "devices") bindDeviceDriverFields(form);
  }

  function fillSelectOptions(select, options, selected) {
    if (!select) return;
    const known = new Set(options.map((o) => String(o.value)));
    const keep = known.has(String(selected ?? "")) ? selected : "";
    select.innerHTML = options.map((o) => {
      const sel = String(o.value) === String(keep) ? " selected" : "";
      const title = o.title ? ` title="${escapeAttr(o.title)}"` : "";
      return `<option value="${escapeAttr(o.value)}"${sel}${title}>${escapeHtml(o.label)}</option>`;
    }).join("");
  }

  function bindDeviceDriverFields(form) {
    const vendorEl = form.querySelector('[name="vendor"]');
    const overrideEl = form.querySelector('[name="driver_override"]');
    const controlEl = form.querySelector('[name="control_driver"]');
    const notesEl = form.querySelector(".driver-notes");
    function refresh() {
      const vendor = vendorEl && vendorEl.value;
      const opts = driverOptions(vendor);
      fillSelectOptions(overrideEl, opts, overrideEl && overrideEl.value);
      fillSelectOptions(controlEl, opts, controlEl && controlEl.value);
      const chosen = (overrideEl && overrideEl.value) || "";
      const row = driverLookup(chosen)
        || (vendor && (state.drivers || []).find((d) => d.vendor === vendor && d.is_default));
      if (notesEl) {
        notesEl.textContent = row && row.notes ? row.notes : "";
        notesEl.hidden = !notesEl.textContent;
      }
    }
    if (vendorEl) vendorEl.addEventListener("change", refresh);
    if (overrideEl) overrideEl.addEventListener("change", refresh);
    refresh();
  }

  function bindOpenIo(root, deviceId) {
    if (!deviceId) return;
    (root || document).querySelectorAll(".open-io-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        ioDeviceId = deviceId;
        setView("io");
      });
    });
  }

  async function lookupGeo(query, kind, latEl, lngEl, sourceEl, msg) {
    if (!query) return;
    if (sourceEl && sourceEl.value === "manual") return;
    if (latEl && latEl.value && lngEl && lngEl.value) return;
    try {
      const data = await apiSend("POST", "/api/geocode", { query, kind });
      if (!data.hit) {
        if (msg) {
          msg.className = "form-msg";
          msg.textContent = "No geocode hit — type lat/lng if you know them.";
        }
        return;
      }
      if (latEl) latEl.value = data.hit.lat;
      if (lngEl) lngEl.value = data.hit.lng;
      if (sourceEl) sourceEl.value = "geocode";
      editDirty = true;
      if (msg) {
        msg.className = "form-msg ok";
        msg.textContent = data.hit.display_name || "Located. Adjust if the pin is off.";
      }
    } catch (err) {
      if (msg) {
        msg.className = "form-msg err";
        msg.textContent = err.message;
      }
    }
  }

  function bindGeoAndPins(panel, kind, siteId) {
    const form = panel.querySelector("form.form");
    if (!form) return;
    const msg = form.querySelector(".form-msg");
    const lat = form.querySelector('[name="lat"]');
    const lng = form.querySelector('[name="lng"]');
    const source = form.querySelector('[name="geo_source"]');
    const markManual = () => {
      if (source) source.value = "manual";
    };
    if (lat) lat.addEventListener("input", markManual);
    if (lng) lng.addEventListener("input", markManual);
    if (kind === "cities") {
      const name = form.querySelector('[name="name"]');
      if (name) {
        name.addEventListener("blur", () => lookupGeo(name.value, "city", lat, lng, source, msg));
      }
    }
    if (kind === "sites") {
      const address = form.querySelector('[name="address"]');
      if (address) {
        address.addEventListener("blur", () => lookupGeo(address.value, "address", lat, lng, source, msg));
      }
      const picker = form.querySelector("[data-pin-picker]");
      const iconInput = form.querySelector('[name="pin_icon"]');
      const colorInput = form.querySelector('[name="pin_color"]');
      if (picker && window.NexNOCPins) {
        const pins = state.pins || [];
        const current = iconInput ? iconInput.value : "building";
        const color = colorInput ? colorInput.value : "#6aa4ff";
        picker.innerHTML = pins.map((p) =>
          `<button type="button" data-pin="${escapeAttr(p.id)}" title="${escapeAttr(p.label)}" class="${p.id === current ? "sel" : ""}">${window.NexNOCPins.svg(p.id, color)}</button>`
        ).join("");
        picker.querySelectorAll("button[data-pin]").forEach((btn) => {
          btn.addEventListener("click", () => {
            if (iconInput) iconInput.value = btn.dataset.pin;
            picker.querySelectorAll("button").forEach((b) => b.classList.toggle("sel", b === btn));
            editDirty = true;
          });
        });
        if (colorInput) {
          colorInput.addEventListener("input", () => {
            picker.querySelectorAll("button[data-pin]").forEach((btn) => {
              btn.innerHTML = window.NexNOCPins.svg(btn.dataset.pin, colorInput.value);
            });
          });
        }
      }
      const file = form.querySelector("#site-pin-file");
      if (file && siteId) {
        file.addEventListener("change", async () => {
          const picked = file.files && file.files[0];
          if (!picked) return;
          const reader = new FileReader();
          reader.onload = async () => {
            try {
              await apiSend("POST", `/api/sites/${siteId}/pin`, {
                filename: picked.name,
                data: String(reader.result || ""),
              });
              if (iconInput) iconInput.value = "upload";
              if (msg) {
                msg.className = "form-msg ok";
                msg.textContent = "Custom pin uploaded.";
              }
              await refresh();
            } catch (err) {
              if (msg) {
                msg.className = "form-msg err";
                msg.textContent = err.message;
              }
            }
          };
          reader.readAsDataURL(picked);
        });
      }
    }
  }

  function renderIoPage() {
    const body = $("io-body");
    const title = $("io-title");
    if (!body) return;
    const d = (state.devices || []).find((x) => x.id === ioDeviceId);
    if (!d) {
      if (title) title.textContent = "Device I/O";
      body.innerHTML = `<p class="muted">Device not found. Go back to Inventory and open I/O from a frame.</p>`;
      return;
    }
    if (title) title.textContent = `${d.name} — I/O`;
    const ports = (state.ports || []).filter((p) => p.device_id === d.id);
    const sdi = ports.filter((p) => p.kind !== "net" && p.kind !== "mgmt");
    const net = ports.filter((p) => p.kind === "net");
    const flows = (state.flows || []).filter((f) =>
      f.source_device_id === d.id || f.dest_device_id === d.id
    );
    const flowsFor = (portId) => flows.filter((f) =>
      f.source_port_id === portId || f.dest_port_id === portId
    );
    const dirSelect = (p) => {
      const fixed = p.capability === "input" || p.capability === "output";
      const current = p.direction || (fixed ? p.capability : "unused");
      if (fixed) {
        return `<p class="muted">Fixed ${escapeHtml(p.capability)}</p>`;
      }
      return `<select data-port-dir="${p.id}">
        <option value="unused"${current === "unused" || current === "" ? " selected" : ""}>unused</option>
        <option value="input"${current === "input" ? " selected" : ""}>input</option>
        <option value="output"${current === "output" ? " selected" : ""}>output</option>
      </select>`;
    };
    const card = (p) => {
      const used = flowsFor(p.id);
      const pathHtml = used.length
        ? `<ul class="row-list">${used.map((f) =>
          `<li><span>${escapeHtml(f.signal_label || f.label)} → ${escapeHtml(destLine(f))}</span>${badge(f.effective_status)}</li>`
        ).join("")}</ul>`
        : `<p class="muted">No paths on this connector.</p>`;
      return `<article class="io-card">
        <h4>${escapeHtml(p.name)}</h4>
        <p class="muted">${escapeHtml(p.kind)}${p.capability ? ` · ${escapeHtml(p.capability)}` : ""}${p.slot ? ` · slot ${escapeHtml(p.slot)}` : ""}</p>
        ${dirSelect(p)}
        ${pathHtml}
        <p class="form-actions">
          <button type="button" class="btn" data-io-path="${p.id}">New path</button>
          <button type="button" class="btn" data-edit-port="${p.id}">Edit</button>
        </p>
      </article>`;
    };
    body.innerHTML = `
      <p class="muted">${escapeHtml(d.site_name || "")}${d.model ? ` · ${escapeHtml(d.model)}` : ""} · driver ${escapeHtml(d.resolved_driver || d.driver_override || "unresolved")}. Assignable BNCs stay unused until a path (or the menu) sets input or output.</p>
      <h3>SDI / BNC</h3>
      ${sdi.length ? `<div class="io-grid">${sdi.map(card).join("")}</div>` : `<p class="muted">No chassis connectors. This vendor has no BNC template, or ports were never stamped.</p>`}
      <h3>Stream / network ports</h3>
      ${net.length ? `<div class="io-grid">${net.map(card).join("")}</div>` : `<p class="muted">None yet. Media IPs belong here, not on the management host.</p>`}
      <p class="form-actions">
        <button type="button" class="btn" id="io-add-net">Add stream port</button>
        <button type="button" class="btn" id="io-add-port">Add connector</button>
      </p>
      <p class="form-msg" id="io-msg"></p>
    `;
    body.querySelectorAll("[data-port-dir]").forEach((sel) => {
      sel.addEventListener("change", async () => {
        const msg = $("io-msg");
        try {
          await apiSend("PATCH", `/api/ports/${sel.dataset.portDir}`, { direction: sel.value });
          await refresh();
        } catch (err) {
          if (msg) {
            msg.className = "form-msg err";
            msg.textContent = err.message;
          }
        }
      });
    });
    body.querySelectorAll("[data-io-path]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const portId = Number(btn.dataset.ioPath);
        const port = (state.ports || []).find((p) => p.id === portId);
        creatingFlow = true;
        selectedFlowId = null;
        setView("links");
        showNewFlowFromPort(d.id, port);
      });
    });
    body.querySelectorAll("[data-edit-port]").forEach((btn) => {
      btn.addEventListener("click", () => {
        setView("inventory");
        selectedDeviceId = d.id;
        showPortEditor(d.id, Number(btn.dataset.editPort));
      });
    });
    const addNet = $("io-add-net");
    if (addNet) {
      addNet.addEventListener("click", async () => {
        const name = window.prompt("Stream port name (e.g. NIC 1 or 10.98.40.109)");
        if (!name) return;
        try {
          await apiSend("POST", "/api/ports", {
            device_id: d.id, name, kind: "net", capability: "assignable", direction: "unused",
          });
          await refresh();
        } catch (err) {
          const msg = $("io-msg");
          if (msg) {
            msg.className = "form-msg err";
            msg.textContent = err.message;
          }
        }
      });
    }
    const addPort = $("io-add-port");
    if (addPort) {
      addPort.addEventListener("click", () => {
        setView("inventory");
        selectedDeviceId = d.id;
        showPortEditor(d.id, null);
      });
    }
  }

  function showNewFlowFromPort(deviceId, port) {
    if (!confirmLeaveForm()) return;
    creatingFlow = true;
    selectedFlowId = null;
    editDirty = false;
    $("links-body")?.querySelectorAll("tr").forEach((r) => r.classList.remove("sel"));
    const asSource = !port || (port.capability !== "output" && port.direction !== "output");
    const seed = {
      source_device_id: asSource ? deviceId : "",
      source_port_id: asSource && port ? port.id : "",
      dest_device_id: asSource ? "" : deviceId,
      dest_port_id: asSource ? "" : (port && port.id),
    };
    const panel = $("link-panel");
    panel.innerHTML = flowForm(seed);
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
        creatingFlow = false;
        selectedFlowId = null;
        panel.innerHTML = `<p class="muted">Click a flow to edit, or New flow.</p>`;
        renderLinks();
      },
    });
    const src = panel.querySelector('[name="source_device_id"]');
    const dst = panel.querySelector('[name="dest_device_id"]');
    const srcPort = panel.querySelector('[name="source_port_id"]');
    const dstPort = panel.querySelector('[name="dest_port_id"]');
    const syncPorts = (selectEl, portEl, role) => {
      if (!selectEl || !portEl) return;
      const current = portEl.value;
      const opts = portOptions(selectEl.value, role);
      portEl.innerHTML = opts.map((o) =>
        `<option value="${escapeAttr(o.value)}"${String(o.value) === String(current) ? " selected" : ""}>${escapeHtml(o.label)}</option>`
      ).join("");
    };
    if (src) src.addEventListener("change", () => syncPorts(src, srcPort, "source"));
    if (dst) dst.addEventListener("change", () => syncPorts(dst, dstPort, "dest"));
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[ch]));
  }
  function escapeAttr(value) {
    return escapeHtml(value);
  }

  function dashboardNow() {
    return window.DashboardTime?.now() ?? new Date();
  }

  function formatJamTime(date) {
    if (!date) return "—";
    return date.toLocaleTimeString("en-US", {
      hour: "numeric",
      minute: "2-digit",
      second: "2-digit",
    });
  }

  function updateClockPopovers() {
    const dt = window.DashboardTime;
    if (!dt) return;
    const offsetMs = dt.getOffsetMs();
    const offsetText = dt.formatOffsetMs(offsetMs);
    const jamText = "Last jam: " + formatJamTime(dt.getLastSyncAt());
    const warnOffset = Math.abs(offsetMs) > (dt.warnOffsetMs || 30_000);
    document.querySelectorAll(".clock-popover").forEach((pop) => {
      const offsetEl = pop.querySelector(".clock-popover-offset");
      const jamEl = pop.querySelector(".clock-popover-jam");
      if (offsetEl) {
        offsetEl.textContent = "Sync offset: " + offsetText;
        offsetEl.classList.toggle("clock-popover-warn", warnOffset);
      }
      if (jamEl) jamEl.textContent = jamText;
    });
  }

  function flashJammed() {
    const brand = document.querySelector(".brand");
    const bar = $("timezone-bar");
    if (brand) {
      brand.classList.add("clock-jammed");
      setTimeout(() => brand.classList.remove("clock-jammed"), 600);
    }
    if (bar) {
      bar.classList.add("clock-jammed");
      setTimeout(() => bar.classList.remove("clock-jammed"), 600);
    }
  }

  async function performJam() {
    if (jamInFlight || !window.DashboardTime?.jamSync) return false;
    jamInFlight = true;
    document.querySelectorAll(".clock-jam-btn").forEach((btn) => { btn.disabled = true; });
    const ok = await window.DashboardTime.jamSync();
    jamInFlight = false;
    document.querySelectorAll(".clock-jam-btn").forEach((btn) => { btn.disabled = false; });
    if (ok) {
      flashJammed();
      tickClock();
    }
    return ok;
  }

  function isEditableTarget(el) {
    if (!el || !(el instanceof HTMLElement)) return false;
    const tag = el.tagName;
    return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable;
  }

  function initClockJam() {
    document.querySelectorAll(".clock-jam-btn").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        performJam();
      });
    });
    document.addEventListener("keydown", (e) => {
      if (!e.altKey || e.ctrlKey || e.metaKey || e.key.toLowerCase() !== "j") return;
      if (isEditableTarget(document.activeElement)) return;
      e.preventDefault();
      performJam();
    });
  }

  function mapDrawerNarrow() {
    return window.matchMedia("(max-width: 900px)").matches;
  }

  function loadDrawerState() {
    try {
      const raw = localStorage.getItem(MAP_DRAWER_KEY);
      if (!raw) return { open: false, width: MAP_DRAWER_DEFAULT, height: null };
      const parsed = JSON.parse(raw);
      return {
        open: false,
        width: Number.isFinite(parsed.width) ? parsed.width : MAP_DRAWER_DEFAULT,
        height: Number.isFinite(parsed.height) ? parsed.height : null,
      };
    } catch (_) {
      return { open: false, width: MAP_DRAWER_DEFAULT, height: null };
    }
  }

  function saveDrawerState(partial) {
    const cur = loadDrawerState();
    const next = { ...cur, ...partial };
    try {
      localStorage.setItem(MAP_DRAWER_KEY, JSON.stringify(next));
    } catch (_) { /* ignore quota / private mode */ }
    return next;
  }

  function clampDrawerWidth(width) {
    const wrap = document.querySelector(".map-wrap");
    const max = wrap ? Math.max(MAP_DRAWER_MIN, Math.floor(wrap.clientWidth * 0.7)) : 640;
    return Math.min(Math.max(MAP_DRAWER_MIN, Math.round(width)), max);
  }

  function clampDrawerHeight(height) {
    const wrap = document.querySelector(".map-wrap");
    const max = wrap ? Math.max(MAP_DRAWER_HEIGHT_MIN, Math.floor(wrap.clientHeight * 0.7)) : 400;
    return Math.min(Math.max(MAP_DRAWER_HEIGHT_MIN, Math.round(height)), max);
  }

  function invalidateMapSoon(delay) {
    const ms = delay == null ? 0 : delay;
    setTimeout(() => {
      if (leafletMap) leafletMap.invalidateSize();
    }, ms);
  }

  function setDrawerOpen(open, persist) {
    const wrap = document.querySelector(".map-wrap");
    const tab = $("map-drawer-tab");
    if (!wrap) return;
    wrap.classList.toggle("drawer-closed", !open);
    if (tab) {
      tab.setAttribute("aria-expanded", open ? "true" : "false");
      tab.title = open ? "Hide details" : "Show details";
    }
    if (persist) saveDrawerState({ open });
    invalidateMapSoon(240);
  }

  function initMapDrawer() {
    const wrap = document.querySelector(".map-wrap");
    const tab = $("map-drawer-tab");
    const handle = $("map-drawer-handle");
    if (!wrap || !tab) return;

    const saved = loadDrawerState();
    wrap.style.setProperty("--map-drawer-open-width", `${clampDrawerWidth(saved.width)}px`);
    if (saved.height) {
      wrap.style.setProperty("--map-drawer-open-height", `${clampDrawerHeight(saved.height)}px`);
    }
    setDrawerOpen(false, false);

    tab.addEventListener("click", () => {
      setDrawerOpen(wrap.classList.contains("drawer-closed"), true);
    });

    if (!handle) return;

    let dragging = false;
    let startPos = 0;
    let startSize = 0;

    handle.addEventListener("pointerdown", (e) => {
      if (wrap.classList.contains("drawer-closed")) return;
      e.preventDefault();
      dragging = true;
      wrap.classList.add("drawer-resizing");
      handle.setPointerCapture(e.pointerId);
      const drawer = $("map-drawer");
      if (mapDrawerNarrow()) {
        startPos = e.clientY;
        startSize = drawer ? drawer.offsetHeight : MAP_DRAWER_HEIGHT_MIN;
      } else {
        startPos = e.clientX;
        startSize = drawer ? drawer.offsetWidth : MAP_DRAWER_DEFAULT;
      }
    });

    handle.addEventListener("pointermove", (e) => {
      if (!dragging) return;
      if (mapDrawerNarrow()) {
        const height = clampDrawerHeight(startSize + (e.clientY - startPos));
        wrap.style.setProperty("--map-drawer-open-height", `${height}px`);
      } else {
        const width = clampDrawerWidth(startSize - (e.clientX - startPos));
        wrap.style.setProperty("--map-drawer-open-width", `${width}px`);
      }
      if (leafletMap) leafletMap.invalidateSize();
    });

    const endDrag = () => {
      if (!dragging) return;
      dragging = false;
      wrap.classList.remove("drawer-resizing");
      const drawer = $("map-drawer");
      if (mapDrawerNarrow()) {
        saveDrawerState({ height: drawer ? drawer.offsetHeight : MAP_DRAWER_HEIGHT_MIN });
      } else {
        saveDrawerState({ width: drawer ? drawer.offsetWidth : MAP_DRAWER_DEFAULT });
      }
      if (leafletMap) leafletMap.invalidateSize();
    };
    handle.addEventListener("pointerup", endDrag);
    handle.addEventListener("pointercancel", endDrag);
  }

  function loadOverlayState() {
    try {
      const raw = localStorage.getItem(TZ_OVERLAY_KEY);
      if (!raw) return { open: false, left: 24, top: 72 };
      const parsed = JSON.parse(raw);
      return {
        open: !!parsed.open,
        left: Number.isFinite(parsed.left) ? parsed.left : 24,
        top: Number.isFinite(parsed.top) ? parsed.top : 72,
      };
    } catch (_) {
      return { open: false, left: 24, top: 72 };
    }
  }

  function saveOverlayState(partial) {
    const cur = loadOverlayState();
    const next = { ...cur, ...partial };
    try {
      localStorage.setItem(TZ_OVERLAY_KEY, JSON.stringify(next));
    } catch (_) { /* ignore quota / private mode */ }
    return next;
  }

  function clampOverlay(left, top) {
    const el = $("tz-overlay");
    if (!el) return { left, top };
    const w = el.offsetWidth || 320;
    const h = el.offsetHeight || 120;
    const maxL = Math.max(8, window.innerWidth - w - 8);
    const maxT = Math.max(8, window.innerHeight - h - 8);
    return {
      left: Math.min(Math.max(8, left), maxL),
      top: Math.min(Math.max(8, top), maxT),
    };
  }

  function placeOverlay(left, top, persist) {
    const el = $("tz-overlay");
    if (!el) return;
    const pos = clampOverlay(left, top);
    el.style.left = `${pos.left}px`;
    el.style.top = `${pos.top}px`;
    if (persist) saveOverlayState(pos);
  }

  function setOverlayOpen(open) {
    const el = $("tz-overlay");
    const btn = $("zones-toggle");
    if (!el) return;
    el.hidden = !open;
    if (btn) btn.classList.toggle("active", open);
    saveOverlayState({ open });
    if (open) {
      const saved = loadOverlayState();
      placeOverlay(saved.left, saved.top, false);
      tickClock();
    }
  }

  function initTzOverlay() {
    const el = $("tz-overlay");
    const head = $("tz-overlay-head");
    const toggle = $("zones-toggle");
    const close = $("tz-overlay-close");
    if (!el || !head || !toggle) return;
    const saved = loadOverlayState();
    placeOverlay(saved.left, saved.top, false);
    setOverlayOpen(saved.open);
    toggle.addEventListener("click", () => setOverlayOpen(el.hidden));
    if (close) close.addEventListener("click", () => setOverlayOpen(false));
    let drag = null;
    head.addEventListener("pointerdown", (e) => {
      if (e.target.closest("button")) return;
      const rect = el.getBoundingClientRect();
      drag = { dx: e.clientX - rect.left, dy: e.clientY - rect.top };
      head.setPointerCapture(e.pointerId);
      e.preventDefault();
    });
    head.addEventListener("pointermove", (e) => {
      if (!drag) return;
      placeOverlay(e.clientX - drag.dx, e.clientY - drag.dy, false);
    });
    const endDrag = () => {
      if (!drag) return;
      drag = null;
      const rect = el.getBoundingClientRect();
      saveOverlayState({ left: rect.left, top: rect.top });
    };
    head.addEventListener("pointerup", endDrag);
    head.addEventListener("pointercancel", endDrag);
    window.addEventListener("resize", () => {
      const rect = el.getBoundingClientRect();
      placeOverlay(rect.left, rect.top, true);
    });
  }

  function clockNow() {
    return frozenClockAt || dashboardNow();
  }

  function updateZoneClocks() {
    const now = clockNow();
    document.querySelectorAll(".clock[data-tz]").forEach((el) => {
      const tz = el.dataset.tz;
      const time = now.toLocaleTimeString("en-US", {
        timeZone: tz,
        hour: "numeric",
        minute: "2-digit",
        hour12: true,
      });
      const [t, ap] = time.split(" ");
      el.textContent = t;
      const ampmEl = document.querySelector(`.ampm[data-tz="${CSS.escape(tz)}"]`);
      if (ampmEl) ampmEl.textContent = ap ?? "";
    });
  }

  function tickClock() {
    const el = $("clock");
    if (!el) return;
    if (!frozenClockAt && !window.DashboardTime?.getLastSyncAt()) {
      el.textContent = "--:--:--";
    } else {
      el.textContent = clockNow().toLocaleTimeString();
    }
    updateZoneClocks();
    updateClockPopovers();
  }

  function markRefreshOk() {
    refreshFails = 0;
    frozenClockAt = null;
    document.body.classList.remove("backend-lost");
    const el = $("refresh-status");
    if (el) el.classList.remove("disconnected");
  }

  function markRefreshFail(err) {
    refreshFails += 1;
    const el = $("refresh-status");
    if (el) {
      el.textContent = `Disconnected (${err.message})`;
      el.classList.add("disconnected");
    }
    if (refreshFails >= REFRESH_FAIL_THRESHOLD && !frozenClockAt) {
      frozenClockAt = dashboardNow();
      document.body.classList.add("backend-lost");
      tickClock();
      console.log(
        `NexNOC lost connection at ${frozenClockAt.toLocaleString()} (${frozenClockAt.toISOString()})`
      );
    }
  }

  async function refresh() {
    const t0 = Date.now();
    try {
      const res = await fetch("/api/state");
      const t1 = Date.now();
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      state = await res.json();
      if (typeof state.server_time_ms === "number") {
        window.DashboardTime?.applyOffset(state.server_time_ms, t0 + (t1 - t0) / 2);
      }
      markRefreshOk();
      renderSummary(state.summary);
      renderMap();
      if (!KIOSK) {
        renderLinkFilters();
        fillInvFilters();
        renderLinks();
        renderInventory();
        renderSetup(false);
        if (currentView === "io") renderIoPage();
      }
      $("refresh-status").textContent = `Live · refreshed ${dashboardNow().toLocaleTimeString()}`;
      $("poll-age").textContent = state.latest_poll_at
        ? `Last device poll ${fmtTime(state.latest_poll_at)}`
        : "Poller has not run yet";
    } catch (err) {
      markRefreshFail(err);
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
  const setupAddCity = $("setup-add-city");
  if (setupAddCity) setupAddCity.addEventListener("click", () => startSetupCreate("city"));
  const setupAddSite = $("setup-add-site");
  if (setupAddSite) setupAddSite.addEventListener("click", () => startSetupCreate("site"));
  const setupAddDevice = $("setup-add-device");
  if (setupAddDevice) setupAddDevice.addEventListener("click", () => startSetupCreate("device"));
  const ioBack = $("io-back");
  if (ioBack) {
    ioBack.addEventListener("click", () => {
      ioDeviceId = null;
      setView("inventory");
    });
  }
  window.addEventListener("hashchange", () => {
    if (KIOSK) return;
    const parsed = parseHash();
    if (parsed.view === currentView && parsed.ioId === ioDeviceId) return;
    ioDeviceId = parsed.ioId;
    setView(parsed.view);
  });

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

  function applyAuthChrome() {
    document.body.classList.toggle("can-edit", canEdit());
    document.body.classList.toggle("can-admin", canAdmin());
    const box = $("user-box");
    if (box && currentUser && !KIOSK) {
      box.hidden = false;
      $("user-name").textContent = currentUser.username;
    }
    const adminBtn = $("admin-open");
    const adminTab = $("tab-admin");
    if (adminBtn) adminBtn.hidden = !canAdmin();
    if (adminTab) adminTab.hidden = !canAdmin();
  }

  async function loadSession() {
    if (KIOSK) return;
    const res = await fetch("/api/auth/me");
    if (res.status === 401) {
      location.href = "/?next=" + encodeURIComponent(location.pathname + location.hash);
      throw new Error("login required");
    }
    const data = await res.json();
    currentUser = data.user;
    if (currentUser && currentUser.must_change_password) {
      location.replace("/?reason=password");
      throw new Error("password change required");
    }
    applyAuthChrome();
  }

  async function loadAdmin() {
    if (!canAdmin()) return;
    adminData = await apiSend("GET", "/api/admin");
    const rolesSel = $("admin-group-roles");
    if (rolesSel && adminData.roles) {
      rolesSel.innerHTML = Object.entries(adminData.roles).map(([id, role]) =>
        `<option value="${id}" title="${escapeAttr(role.description || "")}">${escapeHtml(role.label)}</option>`
      ).join("");
    }
    const usersBody = $("admin-users");
    if (usersBody) {
      usersBody.innerHTML = (adminData.users || []).map((u) => `
        <tr>
          <td>${escapeHtml(u.username)}</td>
          <td>${escapeHtml(u.type)}</td>
          <td>${escapeHtml((u.roles || []).join(", "))}</td>
          <td>${u.enabled ? "yes" : "no"}</td>
          <td>
            <button type="button" class="btn" data-edit-user="${escapeAttr(u.id)}">Edit</button>
            <button type="button" class="btn danger" data-del-user="${escapeAttr(u.id)}">Delete</button>
          </td>
        </tr>`).join("");
      usersBody.querySelectorAll("[data-edit-user]").forEach((btn) => {
        btn.addEventListener("click", () => openUserForm(Number(btn.dataset.editUser)));
      });
      usersBody.querySelectorAll("[data-del-user]").forEach((btn) => {
        btn.addEventListener("click", async () => {
          if (!window.confirm("Delete this user?")) return;
          await apiSend("POST", "/api/admin", { action: "delete_user", id: Number(btn.dataset.delUser) });
          await loadAdmin();
        });
      });
    }
    const ldap = adminData.ldap || {};
    const ldapForm = $("admin-ldap");
    if (ldapForm) {
      ldapForm.enabled.checked = !!ldap.enabled;
      ldapForm.host.value = ldap.host || "";
      ldapForm.port.value = ldap.port || 636;
      ldapForm.bind_template.value = ldap.bind_template || "";
      ldapForm.base_dn.value = ldap.base_dn || "";
      ldapForm.ignore_cert.checked = ldap.ignore_cert !== false;
    }
    const groups = $("admin-groups");
    if (groups) {
      groups.innerHTML = (ldap.allowed_groups || []).map((g) => {
        const name = typeof g === "string" ? g : g.name;
        const roles = typeof g === "string" ? ["viewer"] : (g.roles || []);
        return `<li><span><strong>${escapeHtml(name)}</strong> → ${escapeHtml(roles.join(", "))}</span>
          <button type="button" class="btn danger" data-rm-group="${escapeAttr(name)}">Remove</button></li>`;
      }).join("");
      groups.querySelectorAll("[data-rm-group]").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const next = (adminData.ldap.allowed_groups || []).filter((g) => {
            const name = typeof g === "string" ? g : g.name;
            return name !== btn.dataset.rmGroup;
          });
          await apiSend("POST", "/api/admin", { action: "save_ldap", ...adminData.ldap, allowed_groups: next });
          await loadAdmin();
        });
      });
    }
    const sess = $("admin-session");
    if (sess) sess.session_idle_minutes.value = adminData.session_idle_minutes || 120;
    try {
      const audit = await apiSend("GET", "/api/admin/audit");
      const body = $("admin-audit");
      if (body) {
        body.innerHTML = (audit.entries || []).map((row) => `
          <tr>
            <td>${escapeHtml(row.ts || "")}</td>
            <td>${escapeHtml(row.username || "")}</td>
            <td>${escapeHtml(row.action || "")}</td>
            <td>${escapeHtml(row.path || row.target || row.method || "")}</td>
          </tr>`).join("");
      }
    } catch (_err) {
      /* view_audit may be denied by override */
    }
    await loadServices();
  }

  let selectedService = "nexnoc-web";

  function svcStateClass(active) {
    if (active === "active") return "is-active";
    if (active === "failed") return "is-failed";
    if (active === "inactive") return "is-inactive";
    return "";
  }

  async function loadServiceLogs(unit) {
    const pane = $("admin-svc-log");
    const title = $("admin-svc-log-title");
    if (title) title.textContent = `Logs — ${unit}`;
    if (!pane) return;
    pane.textContent = "Loading…";
    try {
      const data = await apiSend("GET", `/api/admin/services/${encodeURIComponent(unit)}/logs?lines=200`);
      pane.textContent = data.log || "(no log lines)";
      pane.scrollTop = pane.scrollHeight;
    } catch (exc) {
      pane.textContent = exc.message || "Could not load logs";
    }
  }

  async function loadServices() {
    const body = $("admin-services");
    const err = $("admin-svc-error");
    if (!body) return;
    try {
      const data = await apiSend("GET", "/api/admin/services");
      if (err) {
        err.hidden = !!data.available;
        err.textContent = data.available
          ? ""
          : (data.error || "Service helper unavailable — re-run setup.sh");
      }
      const rows = data.services || [];
      if (!rows.some((s) => s.id === selectedService) && rows[0]) {
        selectedService = rows[0].id;
      }
      body.innerHTML = rows.map((svc) => `
        <tr class="${svc.id === selectedService ? "admin-svc-selected" : ""}" data-svc="${escapeAttr(svc.id)}">
          <td>
            <strong>${escapeHtml(svc.id)}</strong>
            <div class="hint">${escapeHtml(svc.label || "")}</div>
          </td>
          <td>
            <span class="admin-svc-state ${svcStateClass(svc.active)}">${escapeHtml(svc.active || "unknown")}</span>
            ${svc.sub ? ` <span class="hint">${escapeHtml(svc.sub)}</span>` : ""}
          </td>
          <td>${escapeHtml(svc.since || "—")}</td>
          <td>
            <button type="button" class="btn" data-restart-svc="${escapeAttr(svc.id)}">Restart</button>
          </td>
        </tr>`).join("");
      body.querySelectorAll("tr[data-svc]").forEach((row) => {
        row.addEventListener("click", (ev) => {
          if (ev.target.closest("[data-restart-svc]")) return;
          selectedService = row.dataset.svc;
          loadServices();
          loadServiceLogs(selectedService);
        });
      });
      body.querySelectorAll("[data-restart-svc]").forEach((btn) => {
        btn.addEventListener("click", async (ev) => {
          ev.stopPropagation();
          const unit = btn.dataset.restartSvc;
          if (!window.confirm(`Restart ${unit}?`)) return;
          btn.disabled = true;
          try {
            await apiSend("POST", `/api/admin/services/${encodeURIComponent(unit)}/restart`, {});
          } catch (_exc) {
            /* nexnoc-web restart drops the connection */
          }
          await new Promise((resolve) => setTimeout(resolve, unit === "nexnoc-web" ? 2000 : 400));
          await loadServices();
          await loadServiceLogs(unit);
        });
      });
      await loadServiceLogs(selectedService);
    } catch (exc) {
      if (err) {
        err.hidden = false;
        err.textContent = exc.message || "Could not load services";
      }
    }
  }

  function openUserForm(id) {
    const user = id
      ? (adminData.users || []).find((u) => u.id === id)
      : { username: "", type: "local", roles: ["viewer"], enabled: true };
    const form = $("user-form");
    $("user-form-title").textContent = id ? "Edit user" : "Add user";
    $("user-form-error").hidden = true;
    form.id.value = user.id || "";
    form.username.value = user.username || "";
    form.type.value = user.type || "local";
    form.password.value = "";
    form.enabled.checked = user.enabled !== false;
    $("user-form-roles").innerHTML = Object.entries(adminData.roles || {}).map(([rid, role]) => `
      <label class="checkbox-label" title="${escapeAttr(role.description || "")}">
        <input type="checkbox" name="role" value="${escapeAttr(rid)}" ${(user.roles || []).includes(rid) ? "checked" : ""}>
        ${escapeHtml(role.label)}
      </label>`).join("");
    $("user-modal").hidden = false;
  }

  initTheme();

  $("logout-btn")?.addEventListener("click", async () => {
    await apiSend("POST", "/api/auth/logout", {});
    location.href = "/";
  });
  $("admin-open")?.addEventListener("click", () => setView("admin"));
  $("admin-add-user")?.addEventListener("click", () => openUserForm(null));
  $("user-form-cancel")?.addEventListener("click", () => { $("user-modal").hidden = true; });
  $("user-form")?.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const err = $("user-form-error");
    err.hidden = true;
    try {
      const data = new FormData(ev.target);
      const roles = [...ev.target.querySelectorAll("input[name=role]:checked")].map((c) => c.value);
      await apiSend("POST", "/api/admin", {
        action: "save_user",
        id: data.get("id") ? Number(data.get("id")) : "",
        username: data.get("username"),
        type: data.get("type"),
        password: data.get("password") || "",
        roles,
        enabled: data.get("enabled") === "on",
      });
      $("user-modal").hidden = true;
      await loadAdmin();
    } catch (exc) {
      err.hidden = false;
      err.textContent = exc.message;
    }
  });
  $("admin-ldap")?.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const form = ev.target;
    await apiSend("POST", "/api/admin", {
      action: "save_ldap",
      enabled: form.enabled.checked,
      host: form.host.value,
      port: Number(form.port.value || 636),
      bind_template: form.bind_template.value,
      base_dn: form.base_dn.value,
      ignore_cert: form.ignore_cert.checked,
      allowed_groups: (adminData.ldap && adminData.ldap.allowed_groups) || [],
    });
    await loadAdmin();
  });
  $("admin-add-group")?.addEventListener("click", async () => {
    const name = ($("admin-group-name").value || "").trim();
    if (!name) return;
    const roles = [...$("admin-group-roles").selectedOptions].map((o) => o.value);
    const groups = [...((adminData.ldap && adminData.ldap.allowed_groups) || [])];
    groups.push({ name, roles: roles.length ? roles : ["viewer"] });
    await apiSend("POST", "/api/admin", { action: "save_ldap", ...adminData.ldap, allowed_groups: groups });
    $("admin-group-name").value = "";
    await loadAdmin();
  });
  $("admin-svc-refresh")?.addEventListener("click", () => {
    loadServices();
  });
  $("admin-session")?.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    await apiSend("POST", "/api/admin", {
      action: "save_session",
      session_idle_minutes: Number(ev.target.session_idle_minutes.value || 120),
    });
    await loadAdmin();
  });

  document.querySelectorAll(".tabs button").forEach((btn) => {
    if (btn.dataset.view === "admin") {
      btn.addEventListener("click", () => { loadAdmin(); });
    }
  });

  (async () => {
    try {
      await loadSession();
    } catch (_err) {
      return;
    }
    setView(currentView);
    initClockJam();
    initTzOverlay();
    initMapDrawer();
    tickClock();
    setInterval(tickClock, 1000);
    refresh();
    setInterval(refresh, REFRESH_MS);
    if (canAdmin() && currentView === "admin") loadAdmin();
  })();
})();
