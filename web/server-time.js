/**
 * server-time.js — server-authoritative clock for the NexNOC UI.
 *
 * Offset = server epoch ms minus client epoch ms. All "now" displays should
 * go through DashboardTime so a wrong PC clock cannot skew the board.
 * Re-syncs via GET /api/time (load, jam, 5 min, tab focus) and from
 * /api/state.server_time_ms on each successful dashboard refresh.
 */
"use strict";

const RESYNC_MS = 5 * 60 * 1000;
const WARN_OFFSET_MS = 30_000;

let clockOffsetMs = 0;
let lastSyncAt = null;

function applyOffset(serverMs, clientMsAtSync) {
  if (typeof serverMs !== "number" || Number.isNaN(serverMs)) return false;
  const client = typeof clientMsAtSync === "number" ? clientMsAtSync : Date.now();
  clockOffsetMs = serverMs - client;
  lastSyncAt = new Date();
  return true;
}

function nowMs() {
  return Date.now() + clockOffsetMs;
}

function now() {
  return new Date(nowMs());
}

function formatOffsetMs(ms) {
  const abs = Math.abs(ms);
  if (abs < 500) return "±0.0s (in sync)";
  const secs = (abs / 1000).toFixed(1);
  const sign = ms >= 0 ? "+" : "-";
  const label = ms >= 0 ? "server ahead" : "device ahead";
  return `${sign}${secs}s (${label})`;
}

async function syncFromServer() {
  const t0 = Date.now();
  try {
    const res = await fetch("/api/time");
    if (!res.ok) return false;
    const data = await res.json();
    const t1 = Date.now();
    const serverMs = data.server_time_ms;
    if (typeof serverMs !== "number" || Number.isNaN(serverMs)) return false;
    applyOffset(serverMs, t0 + (t1 - t0) / 2);
    return true;
  } catch (_) {
    return false;
  }
}

function jamSync() {
  return syncFromServer();
}

function init() {
  syncFromServer();
  setInterval(syncFromServer, RESYNC_MS);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") syncFromServer();
  });
}

window.DashboardTime = {
  now,
  nowMs,
  applyOffset,
  syncFromServer,
  jamSync,
  formatOffsetMs,
  getOffsetMs: () => clockOffsetMs,
  getLastSyncAt: () => lastSyncAt,
  warnOffsetMs: WARN_OFFSET_MS,
};

init();
