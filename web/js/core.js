/* Shared helpers: DOM, fetch, formatting, toast. */
"use strict";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
// a failed request throws an Error carrying the HTTP status (e.status), so callers can tell a 404 / 409 apart
async function _httpErr(r) { const e = new Error((await r.json().catch(() => ({}))).detail || r.statusText || ("HTTP " + r.status)); e.status = r.status; return e; }
// every request carries X-WardogClips: 1 -- the server refuses a non-GET /api/* call (and check-folder) without it,
// and a cross-site page cannot send a custom header (it forces a CORS preflight the server never approves)
const WC_HDR = { "X-WardogClips": "1" };
const _hdrs = (json) => json ? Object.assign({ "Content-Type": "application/json" }, WC_HDR) : Object.assign({}, WC_HDR);
async function api(url) { const r = await fetch(url, { headers: _hdrs(false) }); if (!r.ok) throw await _httpErr(r); return r.json(); }
async function post(url, body) { const r = await fetch(url, { method: "POST", headers: _hdrs(true), body: JSON.stringify(body || {}) }); if (!r.ok) throw await _httpErr(r); return r.json(); }
async function del(url) { const r = await fetch(url, { method: "DELETE", headers: _hdrs(false) }); if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText); return r.json(); }
async function put(url, body) { const r = await fetch(url, { method: "PUT", headers: _hdrs(true), body: JSON.stringify(body || {}) }); if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText); return r.json(); }

const KIND_COLORS = { kill: "var(--k-kill)", assist: "var(--k-assist)", death: "var(--k-death)", spotted: "var(--k-spotted)", revive: "var(--k-revive)", squad: "var(--k-squad)", voice: "var(--k-voice)", manual: "var(--k-manual)" };
const KIND_LABEL = { kill: "kill", assist: "assist", death: "death", spotted: "spotted", revive: "revive", squad: "squad", voice: "voice", manual: "manual" };
// display names (capitalised) for headings / badges; KIND_LABEL stays lower case for running text
const KIND_NAME = { kill: "Kill", assist: "Assist", death: "Death", spotted: "Spotted", revive: "Revive", squad: "Squad", voice: "Voice", manual: "Manual" };

/* Inline SVG icons (24x24, stroke = currentColor). icon("play") -> markup; static markup in index.html uses
   <i data-ic="play"></i> placeholders that hydrateIcons() swaps for the svg (runs once at load, and on any
   subtree a module renders with placeholders). */
const ICONS = {
  rec: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M7 5v14M17 5v14M3 9h4M3 15h4M17 9h4M17 15h4"/>',
  events: '<path d="M5 21V4M5 4h11l-2 4 2 4H5"/>',
  clips: '<circle cx="10.5" cy="10.5" r="6"/><path d="M19.5 19.5l-4.6-4.6"/><path d="M8 10.5h5M10.5 8v5"/>',
  tx: '<path d="M4 6h16M4 10h12M4 14h16M4 18h9"/>',
  jobs: '<rect x="3" y="4" width="18" height="4" rx="1"/><rect x="3" y="10" width="12" height="4" rx="1"/><rect x="3" y="16" width="7" height="4" rx="1"/>',
  vods: '<rect x="3" y="6" width="18" height="13" rx="2"/><path d="M8 3l4 3 4-3"/><path d="M10 10v5l4-2.5z"/>',
  zones: '<rect x="3" y="5" width="18" height="14" rx="2"/><rect x="13" y="7.5" width="5.5" height="3" rx=".5"/><rect x="5.5" y="7.5" width="5" height="4" rx=".5"/><rect x="9" y="12.5" width="6" height="4" rx=".5"/>',
  warn: '<path d="M12 4l9 16H3z"/><path d="M12 10v4M12 17h.01"/>',
  settings: '<path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>',
  help: '<circle cx="12" cy="12" r="9"/><path d="M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .8-1 1.5v.7M12 17h.01"/>',
  play: '<path d="M7 4.5v15l12.5-7.5z"/>', pause: '<rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/>',
  back: '<path d="M17 5L9 12l8 7M7 5v14"/>', fwd: '<path d="M7 5l8 7-8 7M17 5v14"/>',
  replay: '<path d="M4 12a8 8 0 1 0 2.5-5.8"/><path d="M4 4v4h4"/>', loop: '<path d="M17 2l3 3-3 3"/><path d="M4 11V9a4 4 0 0 1 4-4h12"/><path d="M7 22l-3-3 3-3"/><path d="M20 13v2a4 4 0 0 1-4 4H4"/>',
  inp: '<path d="M8 4v16M8 12h10M14 8l4 4-4 4"/>', outp: '<path d="M16 4v16M16 12H6M10 8l-4 4 4 4"/>',
  mark: '<path d="M6 3h12v18l-6-4-6 4z"/>', reset: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/>',
  wider: '<path d="M3 12h18M7 8l-4 4 4 4M17 8l4 4-4 4"/>', send: '<path d="M4 12l16-8-6 16-2.5-6.5z"/>',
  more: '<circle cx="5" cy="12" r="1.3"/><circle cx="12" cy="12" r="1.3"/><circle cx="19" cy="12" r="1.3"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>', x: '<path d="M6 6l12 12M18 6L6 18"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7"/>', star: '<path d="M12 3.5l2.6 5.3 5.9.9-4.3 4.1 1 5.8L12 16.9l-5.2 2.7 1-5.8-4.3-4.1 5.9-.9z"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.2-4.2"/>', chev: '<path d="M9 6l6 6-6 6"/>', chevd: '<path d="M6 9l6 6 6-6"/>',
  grip: '<circle cx="9" cy="6" r="1"/><circle cx="15" cy="6" r="1"/><circle cx="9" cy="12" r="1"/><circle cx="15" cy="12" r="1"/><circle cx="9" cy="18" r="1"/><circle cx="15" cy="18" r="1"/>',
  folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>', copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/>',
  trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>', scan: '<path d="M4 8V5a1 1 0 0 1 1-1h3M16 4h3a1 1 0 0 1 1 1v3M20 16v3a1 1 0 0 1-1 1h-3M8 20H5a1 1 0 0 1-1-1v-3M4 12h16"/>',
  wave: '<path d="M3 12h2M7 8v8M11 5v14M15 9v6M19 7v10M21 12h0"/>', link: '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
  probe: '<circle cx="12" cy="12" r="3"/><path d="M12 3v3M12 18v3M3 12h3M18 12h3"/>', bolt: '<path d="M13 3L5 13h6l-1 8 8-10h-6z"/>',
  resolve: '<circle cx="12" cy="8" r="4"/><circle cx="7" cy="15" r="4"/><circle cx="17" cy="15" r="4"/>', download: '<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>',
  refresh: '<path d="M20 11a8 8 0 0 0-14.3-4.3L4 8.5M4 13a8 8 0 0 0 14.3 4.3L20 15.5"/><path d="M4 4v4.5h4.5M20 20v-4.5h-4.5"/>',
  pausej: '<rect x="7" y="6" width="3.5" height="12" rx="1"/><rect x="13.5" y="6" width="3.5" height="12" rx="1"/>', target: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/>',
  mic: '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/>', ext: '<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
  calib: '<path d="M4 20h16M6 16l4-10 4 10M8 12h4M17 6v10"/>', plus: '<path d="M12 5v14M5 12h14"/>', filter: '<path d="M4 5h16l-6 8v6l-4-2v-4z"/>',
  panel: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/>', cmd: '<path d="M9 6a3 3 0 1 0-3 3h12a3 3 0 1 0-3-3v12a3 3 0 1 0 3-3H6a3 3 0 1 0 3 3z"/>'
};
const FILLED_ICONS = { play: 1, pause: 1 };
function icon(name, cls) {
  const f = FILLED_ICONS[name] ? " f" : "";
  return `<svg class="i${f}${cls ? " " + cls : ""}" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name] || ""}</svg>`;
}
function hydrateIcons(root) {
  (root || document).querySelectorAll("i[data-ic]").forEach(el => { el.outerHTML = icon(el.dataset.ic, el.className || ""); });
}

function fmtDur(s) {
  if (s == null || isNaN(s)) return "-";
  s = Math.round(s); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return (h ? h + "h " : "") + String(m).padStart(h ? 2 : 1, "0") + "m " + String(sec).padStart(2, "0") + "s";
}
function fmtHours(s) { return (s / 3600).toFixed(1) + " h"; }
function fmtBytes(b) {
  if (b == null) return "-";
  if (b >= 1e12) return (b / 1e12).toFixed(2) + " TB";
  if (b >= 1e9) return (b / 1e9).toFixed(1) + " GB";
  if (b >= 1e6) return (b / 1e6).toFixed(0) + " MB";
  return Math.round(b / 1e3) + " KB";
}
function fmtDate(utc) {
  if (!utc) return "-";
  const d = new Date(utc * 1000); const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
function fmtTime(utc) { if (!utc) return "-"; const d = new Date(utc * 1000); const p = (n) => String(n).padStart(2, "0"); return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`; }
function fmtAgo(utc) { if (!utc) return "-"; const s = Math.max(0, Date.now() / 1000 - utc); if (s < 60) return Math.round(s) + "s ago"; if (s < 3600) return Math.round(s / 60) + "m ago"; if (s < 86400) return (s / 3600).toFixed(1) + "h ago"; return Math.round(s / 86400) + "d ago"; }
function fmtMbps(b) { return b ? (b / 1e6).toFixed(0) + " Mb/s" : "-"; }
const pct = (x) => Math.round((x || 0) * 100) + "%";

let _toastT = null, _toastEl = null;
// An open modal <dialog> sits in the top layer above everything in the body, so the toast is shown inside
// the topmost one: the open dialog that holds the focus (the one the user works in), else the last open one
// in DOM order. Nested dialogs: the innermost holding the focus.
function _toastHome() {
  const open = Array.from(document.querySelectorAll("dialog[open]"));
  if (!open.length) return document.body;
  const ae = document.activeElement;
  const focused = ae && ae !== document.body ? open.filter(d => d.contains(ae)) : [];
  return focused.length ? focused[focused.length - 1] : open[open.length - 1];
}
function toast(msg, ms = 2200) {
  // the element is kept by reference: a dialog that held it may have been closed, re-rendered or removed since
  const t = _toastEl || (_toastEl = $("toast")); if (!t) return;
  const home = _toastHome(); if (t.parentNode !== home) home.appendChild(t);
  t.textContent = msg; t.hidden = false; clearTimeout(_toastT); _toastT = setTimeout(() => { t.hidden = true; }, ms); }
// a dialog closing with the toast inside: move it to the next home (another open dialog, or the body), so a
// toast still showing stays visible and later toasts are not shown inside a closed dialog. "close" does not
// bubble, so this listens in the capture phase.
document.addEventListener("close", (e) => {
  const t = _toastEl, d = e.target;
  if (!t || !d || d.tagName !== "DIALOG" || !d.contains(t)) return;
  const home = _toastHome(); if (home !== d) home.appendChild(t); else document.body.appendChild(t);
}, true);
function copyText(s) { try { navigator.clipboard.writeText(s); toast("copied: " + s); } catch (e) { toast("clipboard blocked"); } }
hydrateIcons(document);
