/* Builtin site pin glyphs. Monochrome, tinted with site.pin_color. */
window.NexNOCPins = (() => {
  const PATHS = {
    tower: '<rect x="11" y="4" width="2" height="16"/><path d="M6 8h12M7 12h10M8 16h8" fill="none" stroke="currentColor" stroke-width="1.6"/>',
    dish: '<path d="M5 16c4-9 14-9 14 0" fill="none" stroke="currentColor" stroke-width="1.8"/><circle cx="12" cy="16" r="1.6"/><path d="M12 16V7" fill="none" stroke="currentColor" stroke-width="1.5"/>',
    camera: '<rect x="4" y="8" width="12" height="9" rx="1.5"/><polygon points="16,11 21,8 21,17 16,14"/>',
    rack: '<rect x="6" y="3" width="12" height="18" rx="1" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M8 7h8M8 11h8M8 15h8" fill="none" stroke="currentColor" stroke-width="1.5"/>',
    bnc: '<circle cx="12" cy="12" r="6" fill="none" stroke="currentColor" stroke-width="1.8"/><circle cx="12" cy="12" r="2"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3" fill="none" stroke="currentColor" stroke-width="1.5"/>',
    microwave: '<path d="M4 14l8-8 8 8" fill="none" stroke="currentColor" stroke-width="1.8"/><rect x="10" y="14" width="4" height="6"/>',
    capitol: '<rect x="5" y="14" width="14" height="6"/><path d="M4 14h16M7 14V9h10v5M12 4l7 5H5z"/>',
    mic: '<rect x="9" y="3" width="6" height="10" rx="3"/><path d="M7 12a5 5 0 0010 0M12 17v4M8 21h8" fill="none" stroke="currentColor" stroke-width="1.6"/>',
    studio: '<rect x="4" y="8" width="16" height="12"/><polygon points="8,8 12,3 16,8"/>',
    newsroom: '<rect x="4" y="6" width="16" height="13"/><path d="M7 10h10M7 14h7" fill="none" stroke="#071018" stroke-width="1.4"/>',
    stadium: '<ellipse cx="12" cy="14" rx="8" ry="5" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M5 10c2-4 12-4 14 0" fill="none" stroke="currentColor" stroke-width="1.6"/>',
    field: '<rect x="3" y="6" width="18" height="12" rx="2" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M12 6v12M3 12h18" fill="none" stroke="currentColor" stroke-width="1.3"/>',
    arena: '<path d="M4 16h16l-2-8H6z"/><path d="M7 8V5h10v3" fill="none" stroke="currentColor" stroke-width="1.5"/>',
    helmet: '<path d="M5 14c0-5 3-9 7-9s7 4 7 9v2H5z"/><path d="M5 16h14" fill="none" stroke="#071018" stroke-width="1.4"/>',
    building: '<rect x="6" y="4" width="12" height="16"/><path d="M8 8h2M14 8h2M8 12h2M14 12h2M8 16h2M14 16h2" fill="none" stroke="#071018" stroke-width="1.3"/>',
    office: '<rect x="5" y="3" width="14" height="18"/><path d="M8 7h2M14 7h2M8 11h2M14 11h2M8 15h2M14 15h2" fill="none" stroke="#071018" stroke-width="1.2"/>',
    home: '<path d="M3 12l9-8 9 8v9H3z"/><rect x="10" y="14" width="4" height="7" fill="#071018"/>',
    warehouse: '<path d="M3 10l9-6 9 6v11H3z"/><path d="M8 21v-7h8v7" fill="none" stroke="#071018" stroke-width="1.4"/>',
    star: '<polygon points="12,3 14.5,9 21,9.5 16,14 17.5,21 12,17.5 6.5,21 8,14 3,9.5 9.5,9"/>',
    pin: '<path d="M12 22s7-7.2 7-12a7 7 0 10-14 0c0 4.8 7 12 7 12z"/><circle cx="12" cy="10" r="2.2" fill="#071018"/>',
  };

  function svg(id, color) {
    const inner = PATHS[id] || PATHS.building;
    const fill = color || "#6aa4ff";
    return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="22" height="22" fill="${fill}" stroke="none">${inner}</svg>`;
  }

  function markerHtml(site, status, selected) {
    const color = site.pin_color || "#6aa4ff";
    const sel = selected ? " sel" : "";
    if (site.pin_icon === "upload" && site.pin_upload) {
      return `<div class="site-pin ${status || "unknown"}${sel}"><span class="pin-ring"></span><img class="pin-img" src="/uploads/pins/${site.pin_upload}" alt=""></div>`;
    }
    return `<div class="site-pin ${status || "unknown"}${sel}"><span class="pin-ring"></span><span class="pin-glyph">${svg(site.pin_icon || "building", color)}</span></div>`;
  }

  return { PATHS, svg, markerHtml };
})();
