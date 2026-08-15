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

  let state = null;
  let selected = { type: null, id: null };
  let currentView = KIOSK ? "map" : (location.hash.replace("#", "") || "map");
  let leafletMap = null;
  let tileLayer = null;
  let overlay = null;
  let didFit = false;
  let lastTileUrl = "";
  let editKind = "devices";
  let editId = null;
  let editDirty = false;

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

  function setView(name) {
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
    if (name === "edit") renderEditor(false);
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

  function renderMap() {
    if (!ensureMap()) {
      renderMapPanel();
      return;
    }
    fitOnce();
    overlay.clearLayers();
    const hops = (state.hops || []).filter((h) =>
      h.source_lat != null && h.source_lng != null && h.dest_lat != null && h.dest_lng != null
    );

    hops.forEach((h, i) => {
      const bulge = 1.15 + (i % 3) * 0.25;
      const pts = hopLatLngs(h.source_lat, h.source_lng, h.dest_lat, h.dest_lng, bulge);
      const width = 3 + Math.min(h.flow_count, 10) * 1.1;
      const dim = selected.type === "hop" && selected.id !== h.id;
      const sel = selected.type === "hop" && selected.id === h.id;
      const color = STATUS_COLOR[h.status] || STATUS_COLOR.unknown;
      const hit = L.polyline(pts, { color: "#000", weight: 20, opacity: 0, interactive: true });
      const line = L.polyline(pts, {
        color,
        weight: width,
        opacity: dim ? 0.22 : 0.95,
        lineCap: "round",
        interactive: true,
      });
      if (sel) line.setStyle({ weight: width + 1.6 });
      hit.on("click", (ev) => stopSelect(ev, "hop", h.id));
      line.on("click", (ev) => stopSelect(ev, "hop", h.id));
      overlay.addLayer(hit);
      overlay.addLayer(line);
    });

    mapNodes().forEach((s) => {
      const kind = s.site_ids ? "city" : "site";
      const sel = selected.type === kind && String(selected.id) === String(s.id) ? " sel" : "";
      const siteBit = s.site_count != null
        ? `${s.site_count} site${s.site_count === 1 ? "" : "s"} · `
        : "";
      const sub = `${siteBit}${s.device_count} device${s.device_count === 1 ? "" : "s"}`;
      const marker = L.marker([s.lat, s.lng], {
        zIndexOffset: 400,
        icon: L.divIcon({
          className: `city-marker ${s.status}${sel}`,
          html: `<div class="pin-dot"></div><div class="city-pill"><span class="pill-bar"></span><div class="pill-text"><div class="site-label">${escapeHtml(s.name)}</div><div class="site-sub">${escapeHtml(sub)}</div></div></div>`,
          iconSize: [240, 48],
          iconAnchor: [6, 24],
        }),
      });
      marker.on("click", (ev) => stopSelect(ev, kind, s.id));
      overlay.addLayer(marker);
    });

    if (selected.type === "city") {
      const city = (state.cities || []).find((c) => String(c.id) === String(selected.id));
      const citySites = city
        ? state.sites.filter((s) => (city.site_ids || []).includes(s.id) && s.lat != null)
        : [];
      citySites.forEach((s, i) => {
        const sameSpot = city && Math.hypot(s.lat - city.lat, s.lng - city.lng) < 0.02;
        const n = citySites.length;
        const lat = sameSpot
          ? city.lat + Math.cos((i / n) * Math.PI * 2 - Math.PI / 2) * 0.18
          : s.lat;
        const lng = sameSpot
          ? city.lng + Math.sin((i / n) * Math.PI * 2 - Math.PI / 2) * 0.18
          : s.lng;
        const marker = L.marker([lat, lng], {
          zIndexOffset: 500,
          icon: L.divIcon({
            className: `site-dot ${s.status}`,
            html: `<div class="pin-dot"></div>`,
            iconSize: [16, 16],
            iconAnchor: [8, 8],
          }),
        });
        marker.on("click", (ev) => stopSelect(ev, "site", s.id));
        overlay.addLayer(marker);
      });
    }

    renderMapPanel();
  }

  function select(type, id) {
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
      let html = `<p class="muted">Click a city or a trunk. One pipe per city pair — thickness is path count, color is worst status. Click a trunk to see every path on it.</p>`;
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
        ${sites.length ? sites.map((site) => {
          const devices = state.devices.filter((d) => d.site_id === site.id);
          return `<h3>${escapeHtml(site.name)}</h3>
            ${devices.length ? `<ul class="row-list">${devices.map((d) => {
              const lines = deviceFlowLines(d.id);
              const extra = [...lines.inputHtml, ...lines.outputHtml].join("<br>");
              return `<li><span>${escapeHtml(d.name)}<br><span class="muted">${escapeHtml(d.vendor)} ${escapeHtml(d.model || "")}${extra ? `<br>${extra}` : ""}</span></span>${badge(d.status)}</li>`;
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
      return;
    }
    if (selected.type === "site") {
      const site = state.sites.find((s) => s.id === selected.id);
      if (!site) return;
      if (site.city_key) {
        select("city", site.city_key);
        return;
      }
      select("city", `s:${site.id}`);
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

  function renderLinks() {
    const q = $("link-search").value.trim().toLowerCase();
    const status = $("link-status").value;
    const srcCity = $("link-src-city") ? $("link-src-city").value : "";
    const destCity = $("link-dest-city") ? $("link-dest-city").value : "";
    const srcSite = $("link-src-site").value;
    const destSite = $("link-dest-site").value;
    const srcPort = $("link-src-port").value;
    const destDevice = $("link-dest-device").value;
    const vendor = $("link-vendor").value;
    const rows = (state.flows || []).filter((f) => {
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
    $("links-body").innerHTML = rows.map((f) => `
      <tr>
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
      </tr>
    `).join("");
    $("links-empty").hidden = rows.length > 0;
  }

  function renderInventory() {
    const rows = state.devices;
    $("inv-body").innerHTML = rows.map((d) => `
      <tr data-device="${d.id}">
        <td>${badge(d.status)}</td>
        <td>${escapeHtml(d.name)}</td>
        <td>${escapeHtml(d.site_name)}</td>
        <td>${escapeHtml(d.vendor)}</td>
        <td>${escapeHtml(d.model || "—")}</td>
        <td>${escapeHtml(d.device_role || "—")}</td>
        <td>${escapeHtml(d.mgmt_host || "—")}</td>
        <td>${escapeHtml(d.resolved_driver || d.driver_override || "—")}</td>
        <td>${escapeHtml(fmtTime(d.last_seen_at))}</td>
      </tr>
    `).join("");
    $("inv-empty").hidden = rows.length > 0;
    $("inv-body").querySelectorAll("tr").forEach((tr) => {
      tr.addEventListener("click", () => {
        $("inv-body").querySelectorAll("tr").forEach((r) => r.classList.remove("sel"));
        tr.classList.add("sel");
        loadDevice(Number(tr.dataset.device));
      });
    });
  }

  async function loadDevice(id) {
    const panel = $("inv-panel");
    panel.innerHTML = `<p class="muted">Loading…</p>`;
    try {
      const res = await fetch(`/api/devices/${id}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const d = data.device;
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
      panel.innerHTML = `
        <h2>${escapeHtml(d.name)}</h2>
        <p class="muted">${escapeHtml(d.city_name || "")}${d.city_name && d.site_name ? " · " : ""}${escapeHtml(d.site_name)} · ${escapeHtml(d.mgmt_host || "no management IP")}</p>
        <p>${badge(d.status)}${d.poll_enabled ? "" : ' <span class="badge unknown"><span class="dot"></span>poll off</span>'}</p>
        <p class="muted">Credentials: ${d.credentials_ready ? '<span class="cred-flag ok">set</span>' : '<span class="cred-flag missing">missing — edit to add</span>'}</p>
        <p class="form-actions"><button type="button" class="btn" id="inv-edit-device">Edit in portal</button></p>
        <h3>Configuration</h3>
        <p class="muted">${escapeHtml(d.vendor)} ${escapeHtml(d.model || "")} · ${escapeHtml(d.device_role || "device")}</p>
        <p class="muted">${escapeHtml(d.access_mode)}${d.firmware_version ? ` · fw ${escapeHtml(d.firmware_version)}` : ""}</p>
        <p class="muted">Driver: ${escapeHtml(d.resolved_driver || d.driver_override || "unresolved")}</p>
        ${d.last_error ? `<p class="badge down">${escapeHtml(d.last_error)}</p>` : ""}
        <h3>Ports</h3>
        ${ports.length ? `<ul class="row-list">${ports.map((p) =>
          `<li><span>${escapeHtml(p.name)}<br><span class="muted">${escapeHtml(p.kind)}${p.slot ? ` · slot ${escapeHtml(p.slot)}` : ""}</span></span>${badge(p.status)}</li>`
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
      `;
      const editBtn = $("inv-edit-device");
      if (editBtn) {
        editBtn.addEventListener("click", () => {
          editKind = "devices";
          editId = d.id;
          editDirty = false;
          document.querySelectorAll("#edit-kinds button").forEach((b) => {
            b.classList.toggle("active", b.dataset.kind === "devices");
          });
          setView("edit");
          renderEditor(true);
        });
      }
    } catch (err) {
      panel.innerHTML = `<p class="muted">Could not load device: ${escapeHtml(err.message)}</p>`;
    }
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

  function editorRows() {
    const q = ($("edit-search")?.value || "").trim().toLowerCase();
    if (editKind === "cities") {
      return (state.cities || []).filter((c) => !q || c.name.toLowerCase().includes(q))
        .map((c) => ({ ...c, id: cityDbId(c) }));
    }
    if (editKind === "sites") {
      return (state.sites || []).filter((s) => !q || `${s.name} ${s.city_name || ""}`.toLowerCase().includes(q));
    }
    if (editKind === "ports") {
      return (state.ports || []).filter((p) => !q || `${p.device_name} ${p.name} ${p.kind}`.toLowerCase().includes(q));
    }
    if (editKind === "flows") {
      return (state.flows || []).filter((f) => {
        if (!q) return true;
        return `${f.signal_label || ""} ${f.label} ${f.source_device_name} ${f.dest_display || ""} ${f.dest_city_name || ""}`.toLowerCase().includes(q);
      });
    }
    return (state.devices || []).filter((d) => {
      if (!q) return true;
      return `${d.name} ${d.site_name} ${d.vendor} ${d.mgmt_host || ""}`.toLowerCase().includes(q);
    });
  }

  function renderEditorTable() {
    if (!$("edit-head")) return;
    const heads = {
      devices: ["Name", "Site", "Host", "Creds", "Poll"],
      flows: ["Signal", "Source", "Dest city", "Dest"],
      ports: ["Device", "Port", "Kind"],
      sites: ["Site", "City"],
      cities: ["City", "Lat", "Lng"],
    };
    $("edit-head").innerHTML = `<tr>${heads[editKind].map((h) => `<th>${h}</th>`).join("")}</tr>`;
    const rows = editorRows();
    const html = rows.map((row) => {
      const sel = Number(editId) === Number(row.id) ? " sel" : "";
      if (editKind === "devices") {
        return `<tr class="${sel}" data-id="${row.id}"><td>${escapeHtml(row.name)}</td><td>${escapeHtml(row.site_name)}</td><td>${escapeHtml(row.mgmt_host || "—")}</td><td>${row.credentials_ready ? "set" : "missing"}</td><td>${row.poll_enabled ? "on" : "off"}</td></tr>`;
      }
      if (editKind === "flows") {
        return `<tr class="${sel}" data-id="${row.id}"><td>${escapeHtml(row.signal_label || row.label)}</td><td>${escapeHtml(row.source_device_name)}</td><td>${escapeHtml(row.dest_city_name || "—")}</td><td>${escapeHtml(row.dest_device_name || row.dest_site_name || "—")}</td></tr>`;
      }
      if (editKind === "ports") {
        return `<tr class="${sel}" data-id="${row.id}"><td>${escapeHtml(row.device_name)}</td><td>${escapeHtml(row.name)}</td><td>${escapeHtml(row.kind)}</td></tr>`;
      }
      if (editKind === "sites") {
        return `<tr class="${sel}" data-id="${row.id}"><td>${escapeHtml(row.name)}</td><td>${escapeHtml(row.city_name || row.city || "—")}</td></tr>`;
      }
      return `<tr class="${sel}" data-id="${row.id}"><td>${escapeHtml(row.name)}</td><td>${row.lat ?? "—"}</td><td>${row.lng ?? "—"}</td></tr>`;
    }).join("");
    $("edit-body").innerHTML = html;
    $("edit-empty").hidden = rows.length > 0;
    $("edit-body").querySelectorAll("tr").forEach((tr) => {
      tr.addEventListener("click", () => {
        editId = Number(tr.dataset.id);
        editDirty = false;
        renderEditor(true);
      });
    });
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
      <form class="form" id="edit-form">
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
          ${field("Role", "device_role", d?.device_role || "")}
          ${field("Firmware", "firmware_version", d?.firmware_version || "")}
          ${field("Management IP / host", "mgmt_host", d?.mgmt_host || "", "wide")}
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
          <label class="wide"><input name="poll_enabled" type="checkbox"${(!d || d.poll_enabled) ? " checked" : ""}> Poll this device</label>
        </div>
        <div class="form-actions">
          <button type="submit" class="btn primary">Save</button>
          ${d ? `<button type="button" class="btn danger" id="edit-delete">Delete</button>` : ""}
        </div>
        <p class="form-msg" id="edit-msg"></p>
      </form>
    `;
  }

  function flowForm(f) {
    return `
      <form class="form" id="edit-form">
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
          ${f ? `<button type="button" class="btn danger" id="edit-delete">Delete</button>` : ""}
        </div>
        <p class="form-msg" id="edit-msg"></p>
      </form>
    `;
  }

  function portForm(p) {
    return `
      <form class="form" id="edit-form">
        <h2>${p ? escapeHtml(p.name) : "New port"}</h2>
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
          ${p ? `<button type="button" class="btn danger" id="edit-delete">Delete</button>` : ""}
        </div>
        <p class="form-msg" id="edit-msg"></p>
      </form>
    `;
  }

  function siteForm(s) {
    return `
      <form class="form" id="edit-form">
        <h2>${s ? escapeHtml(s.name) : "New site"}</h2>
        <div class="form-grid">
          ${field("Name", "name", s?.name || "", "wide")}
          ${selectField("City", "city_id", cityOptions(), s?.city_id, "wide")}
          ${field("Latitude", "lat", s?.lat ?? "")}
          ${field("Longitude", "lng", s?.lng ?? "")}
          <label class="wide">Notes<textarea name="notes">${escapeHtml(s?.notes || "")}</textarea></label>
        </div>
        <div class="form-actions">
          <button type="submit" class="btn primary">Save</button>
          ${s ? `<button type="button" class="btn danger" id="edit-delete">Delete</button>` : ""}
        </div>
        <p class="form-msg" id="edit-msg"></p>
      </form>
    `;
  }

  function cityForm(c) {
    return `
      <form class="form" id="edit-form">
        <h2>${c ? escapeHtml(c.name) : "New city"}</h2>
        <div class="form-grid">
          ${field("Name", "name", c?.name || "", "wide")}
          ${field("Latitude", "lat", c?.lat ?? "")}
          ${field("Longitude", "lng", c?.lng ?? "")}
          <label class="wide">Notes<textarea name="notes">${escapeHtml(c?.notes || "")}</textarea></label>
        </div>
        <div class="form-actions">
          <button type="submit" class="btn primary">Save</button>
          ${c ? `<button type="button" class="btn danger" id="edit-delete">Delete</button>` : ""}
        </div>
        <p class="form-msg" id="edit-msg"></p>
      </form>
    `;
  }

  function fillEditorForm() {
    const panel = $("edit-panel");
    if (!panel || !state) return;
    if (editKind === "devices") {
      const d = (state.devices || []).find((x) => x.id === editId);
      panel.innerHTML = deviceForm(d || null);
    } else if (editKind === "flows") {
      const f = (state.flows || []).find((x) => x.id === editId);
      panel.innerHTML = flowForm(f || null);
    } else if (editKind === "ports") {
      const p = (state.ports || []).find((x) => x.id === editId);
      panel.innerHTML = portForm(p || null);
    } else if (editKind === "sites") {
      const s = (state.sites || []).find((x) => x.id === editId);
      panel.innerHTML = siteForm(s || null);
    } else {
      const c = (state.cities || []).find((x) => Number(cityDbId(x)) === Number(editId));
      panel.innerHTML = cityForm(c || null);
    }
    bindEditorForm();
  }

  function renderEditor(force) {
    if (KIOSK || !$("edit-panel") || !state) return;
    renderEditorTable();
    if (force || !$("edit-form")) fillEditorForm();
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

  function collectionPath() {
    return `/api/${editKind}`;
  }

  function cleanBody(data) {
    const body = { ...data };
    ["api_username", "api_password"].forEach((k) => {
      if (!body[k]) delete body[k];
    });
    ["lat", "lng", "api_port", "site_id", "city_id", "device_id", "source_device_id",
      "source_port_id", "dest_city_id", "dest_site_id", "dest_device_id", "dest_port_id",
    ].forEach((k) => {
      if (body[k] === "") body[k] = null;
    });
    if (editKind === "sites" && body.city_id) {
      const city = (state.cities || []).find((c) => String(c.id) === String(body.city_id));
      if (city) body.city = city.name;
    }
    return body;
  }

  function bindEditorForm() {
    const form = $("edit-form");
    if (!form) return;
    form.addEventListener("input", () => { editDirty = true; });
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const msg = $("edit-msg");
      try {
        const body = cleanBody(formValues(form));
        const path = editId ? `${collectionPath()}/${editId}` : collectionPath();
        const method = editId ? "PATCH" : "POST";
        const result = await apiSend(method, path, body);
        msg.className = "form-msg ok";
        msg.textContent = "Saved.";
        editDirty = false;
        if (!editId) {
          const created = result.device || result.flow || result.port || result.site || result.city;
          if (created && created.id) editId = created.id;
        }
        await refresh();
        renderEditor(true);
      } catch (err) {
        msg.className = "form-msg err";
        msg.textContent = err.message;
      }
    });
    const del = $("edit-delete");
    if (del) {
      del.addEventListener("click", async () => {
        if (!editId || !window.confirm("Delete this item?")) return;
        const msg = $("edit-msg");
        try {
          await apiSend("DELETE", `${collectionPath()}/${editId}`);
          editId = null;
          editDirty = false;
          await refresh();
          renderEditor(true);
        } catch (err) {
          msg.className = "form-msg err";
          msg.textContent = err.message;
        }
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
        renderLinks();
        renderInventory();
        renderEditor(false);
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

  document.querySelectorAll("#edit-kinds button").forEach((btn) => {
    btn.addEventListener("click", () => {
      editKind = btn.dataset.kind;
      editId = null;
      editDirty = false;
      document.querySelectorAll("#edit-kinds button").forEach((b) => {
        b.classList.toggle("active", b === btn);
      });
      renderEditor(true);
    });
  });
  const editSearch = $("edit-search");
  if (editSearch) editSearch.addEventListener("input", () => renderEditorTable());
  const editNew = $("edit-new");
  if (editNew) {
    editNew.addEventListener("click", () => {
      editId = null;
      editDirty = false;
      renderEditor(true);
    });
  }

  setView(currentView);
  tickClock();
  setInterval(tickClock, 1000);
  refresh();
  setInterval(refresh, REFRESH_MS);
})();
