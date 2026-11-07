/* WardogClips page shell.
   - A rail on the far left opens ONE side panel at a time (Library, Events, Find, Said, Jobs, VODs; Zones opens the
     zones view in the main column); clicking the active icon folds the panel away.
   - The main column: the player (player.js), the whole-recording timeline, and ONE context area: the audio tracks
     (mixer.js, always shown) plus tabs Event | Recording | Transcript. Calibration and the zones editor replace it
     (ST.view).
   - Top bar: breadcrumb + the one search field (typing filters Recordings, Enter searches every transcript);
     Ctrl+K = command palette (every action, the rare library-wide ones included). Status bar at the bottom.
   - The settings modal autosaves per field; a server-sent-events feed keeps rows, job bars and transcripts live
     (a refresh is data only: it never restarts playback).
   The other modules rely on the ids / mount points in index.html and on these globals: ST, showTab, openPanel,
   closePanel, showCtab, select, selectEvent, renderStage, renderDetail, renderEvents, loadEvents, loadRecs,
   loadJobs, openCalibration, closeCalibration, showPop, closePop, openMenu, openPalette, openSettings, tc ... */
"use strict";

const ST = { status: null, settings: null, recs: [], sel: null, detail: null, jobs: [], counts: {}, tab: "rec", drive: "", q: "",
  sse: null, transcript: { rec_id: null, segments: [], summary: null, filter: "", cur: null }, search: { q: "", hits: [], fts: true, pending: false },
  jobprog: {}, events: { rec_id: null, list: [] }, event: null, chips: { kill: true, assist: true, death: true, spotted: true, revive: false, squad: false, voice: true, manual: true },
  view: "normal", cal: null, resolve: null, drives: null, handoff: { scope: "rec", status: "kept", min_score: 0, order: "time", kinds: "" },
  found: { events: [], total: 0, pending: false, ran: false, error: null, token: 0, openTok: 0, target: null, lastIdx: null },
  // shell state (localStorage wclips.shell): the side panel's content + whether it is unfolded, the context tab
  panelOpen: true, ctab: "event", sigOpen: false, encoder: null };

const KINDS = ["kill", "assist", "death", "spotted", "revive", "squad", "voice", "manual"];
const KIND_DEFAULT = { kill: true, assist: true, death: true, spotted: true, revive: false, squad: false, voice: true, manual: true };
const PANELS = ["rec", "events", "clips", "said", "jobs", "vods"];
const CTABS = ["event", "rec", "tx"];          // the audio tracks are pinned above the tabs (always shown)
const tc = (s) => { s = Math.max(0, Math.round(s)); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60; return (h ? h + ":" : "") + String(m).padStart(h ? 2 : 1, "0") + ":" + String(sec).padStart(2, "0"); };
const fmtDurShort = (s) => s == null || isNaN(s) ? "-" : tc(s);
const fmtWhen = (utc) => utc ? new Date(utc * 1000).toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }) : "-";
const fmtDay = (utc) => utc ? new Date(utc * 1000).toLocaleDateString([], { weekday: "short", day: "numeric", month: "short", year: "numeric" }) : "-";
const provOf = (name) => (String(name || "").match(/^VOD_([A-Za-z]+)_/) || [])[1] || null;
const provBadge = (p) => p ? `<span class="prov p-${esc(p.toLowerCase())}">${esc(p.toUpperCase())}</span>` : "";

/* ---------------- boot ---------------- */
function restoreShell() {
  try {
    const s = JSON.parse(localStorage.getItem("wclips.shell") || "{}");
    if (PANELS.includes(s.tab) && s.tab !== "vods") ST.tab = s.tab;
    if (typeof s.open === "boolean") ST.panelOpen = s.open;
    if (CTABS.includes(s.ctab)) ST.ctab = s.ctab;
    ST.sigOpen = !!s.sig;
  } catch (e) { }
  applyPanel(); applyCtab();
}
function saveShell() { try { localStorage.setItem("wclips.shell", JSON.stringify({ tab: ST.tab, open: ST.panelOpen, ctab: ST.ctab, sig: ST.sigOpen })); } catch (e) { } }

async function boot() {
  restoreShell();
  renderHelp();
  ST.settings = await api("/api/settings");
  await refreshStatus();
  await loadRecs();
  await loadJobs();
  wire();
  connectSSE();
  // the server caches the probe (3 s while Resolve is up, 45 s while it is down: each down-probe is a ~4 s child process)
  refreshResolve(false); setInterval(() => { if (!document.hidden) refreshResolve(false); }, 15000);
  refreshEncoder(); setInterval(() => { if (!document.hidden) refreshEncoder(); }, 60000);
  renderEvents(); renderSaid(); renderTranscript(); renderDetail(); renderCrumb();
  // the first-run wizard (setup.js) opens while the server says setup is not done; nothing is scanned
  // before the user confirmed folders there (an empty roots list has nothing to scan anyway)
  let setupDone = true;
  try { setupDone = window.SU ? await SU.boot() : true; } catch (e) { console.error(e); }
  const roots = (ST.settings && ST.settings.library && ST.settings.library.roots) || [];
  if (setupDone && roots.length && !ST.recs.length && !ST.drive && !ST.q) { toast("library is empty - scanning the roots now", 4000); scan(); }
}

function wire() {
  // rail: one panel at a time, the active icon folds it; VODs / Zones have their own homes
  document.querySelectorAll("#rail .rb[data-p]").forEach(b => b.onclick = () => {
    const p = b.dataset.p;
    if (p === "zones") toggleZones();
    else if (p === "vods") { if (ST.panelOpen && ST.tab === "vods" && ST.view !== "zones") closePanel(); else openVods(); }
    else togglePanel(p);
  });
  $("btn-settings").onclick = () => openSettings();
  $("btn-help").onclick = openHelp;
  document.querySelectorAll("#ctabs button[data-ct]").forEach(b => b.onclick = () => showCtab(b.dataset.ct));
  $("crumb").onclick = (e) => { if (e.target.closest("b")) { openPanel("rec"); const row = ST.sel && $("rec-" + ST.sel); if (row) row.scrollIntoView({ block: "nearest" }); } };

  $("sel-drive").onchange = () => { ST.drive = $("sel-drive").value; loadRecs(); if (ST.search.q) runSearch(ST.search.q); finderChanged(); };
  wireSearch();
  wireVod();
  $("btn-palette").onclick = openPalette;
  $("btn-send").onclick = (e) => { e.stopPropagation(); if (!$("pop").hidden && $("pop").classList.contains("handoff")) closePop(); else openHandoff($("btn-send")); };
  $("btn-pause").onclick = togglePause;
  $("pill-jobs").onclick = () => openPanel("jobs");
  $("pill-resolve").onclick = resolvePillClick;
  $("btn-dclose").onclick = closeDrawer;
  $("scrim").onclick = closeDrawer;
  $("lightbox").onclick = () => { $("lightbox").hidden = true; };
  // the player's clip tools popover (static markup, player.js wires its buttons)
  $("pl-more").onclick = (e) => { e.stopPropagation(); $("pl-tools").hidden = !$("pl-tools").hidden; };
  $("pl-tools").addEventListener("click", (e) => { if (e.target.closest("button.mi")) setTimeout(() => { $("pl-tools").hidden = true; }, 0); });
  // the player row follows the picture's shape (no letterbox bars): style.css reads --pl-ar
  $("pl-video").addEventListener("loadedmetadata", () => { const v = $("pl-video"); if (v.videoWidth && v.videoHeight) setPlayerAr(v.videoWidth / v.videoHeight); });
  wireFinder();
  wireTips();
  // delegated: "..." menus and the kinds filter buttons, wherever a panel rendered them
  document.addEventListener("click", (e) => {
    const m = e.target.closest("[data-menu]");
    if (m && MENUS[m.dataset.menu]) { e.stopPropagation(); if (popAnchor === m) { closePop(); return; } openMenu(m, MENUS[m.dataset.menu](m.dataset.arg)); return; }
    const k = e.target.closest("[data-kinds]");
    if (k) { e.stopPropagation(); if (popAnchor === k) { closePop(); return; } openKinds(k); return; }
    if (!$("ctx").contains(e.target)) closeCtx();
  });
  document.addEventListener("mousedown", (e) => {
    const p = $("pop");
    if (!p.hidden && !p.contains(e.target) && !(popAnchor && popAnchor.contains && popAnchor.contains(e.target))) closePop();
    const t = $("pl-tools");
    if (!t.hidden && !t.contains(e.target) && !$("pl-more").contains(e.target)) t.hidden = true;
  });
  window.addEventListener("resize", () => { closePop(); closeCtx(); });
  document.addEventListener("keydown", onKey);
  // the zones editor marks #main with .zn-open while it is open: the rail, the crumb and the panel follow it
  new MutationObserver(onMainView).observe($("main"), { attributes: true, attributeFilter: ["class"] });
}

/* ---------------- shell: rail, side panel, context tabs, crumb ---------------- */
function applyPanel() {
  $("panel").classList.toggle("closed", !ST.panelOpen);
  document.querySelectorAll("#panel > .pv").forEach(s => { s.hidden = s.dataset.p !== ST.tab; });
  renderRail(); saveShell();
}
function renderRail() {
  document.querySelectorAll("#rail .rb[data-p]").forEach(b => {
    const p = b.dataset.p;
    const on = p === "zones" ? ST.view === "zones" : (ST.panelOpen && ST.tab === p);
    b.classList.toggle("on", on);
  });
  const c = ST.counts || {}; const dot = $("rail-jobs-dot");
  if (dot) {
    const n = c.running || c.failed || 0;
    dot.hidden = !n; dot.textContent = n; dot.className = "dot" + (!c.running && c.failed ? " bad" : "");
  }
}
// switch the side panel's content; it stays folded if it was folded (the rail / openPanel unfold it)
function showTab(t) {
  if (t === "transcript") { showCtab("tx"); return; }      // the per-recording transcript lives in the context area now
  if (!PANELS.includes(t)) return;
  ST.tab = t; applyPanel();
  if (t === "clips") { if (!ST.found.ran) runFind(); else renderClips(); }
  if (t === "jobs") loadJobs();
  if (t === "said") renderSaid();
  if (t === "events") renderEvents();
  if (t === "vods" && window.VB && typeof VB.mountPanel === "function") VB.mountPanel($("vb-panel"));
}
function openPanel(t) {
  if (ST.view === "zones" && t && t !== "vods") { /* the panel can sit beside the zones view */ }
  ST.panelOpen = true; showTab(t || ST.tab);
}
function closePanel() { ST.panelOpen = false; applyPanel(); }
function togglePanel(t) { if (ST.panelOpen && ST.tab === t) closePanel(); else openPanel(t); }
function openVods(opts) {
  if (window.VB && typeof VB.mountPanel === "function") { if (opts && (opts.provider || opts.query || opts.preselect_url)) VB.open(opts); else openPanel("vods"); return; }
  if (window.VB) VB.open(opts || {}); else toast("the VOD browser did not load");
}
async function toggleZones() {
  if (!window.ZN) { toast("the zones editor did not load"); return; }
  if (ZN.isOpen()) { await ZN.close(); return; }
  ZN.open(ST.detail ? { rec_id: ST.detail.id } : {});
}
let _zonesFolded = null;
function onMainView() {
  const zn = $("main").classList.contains("zn-open");
  if (zn && _zonesFolded === null) { _zonesFolded = ST.panelOpen; if (ST.panelOpen) { ST.panelOpen = false; applyPanel(); } }
  else if (!zn && _zonesFolded !== null) { const was = _zonesFolded; _zonesFolded = null; if (was && !ST.panelOpen) { ST.panelOpen = true; applyPanel(); } }
  renderRail(); renderCrumb();
}

function applyCtab() {
  document.querySelectorAll("#ctabs button[data-ct]").forEach(b => b.classList.toggle("on", b.dataset.ct === ST.ctab));
  document.querySelectorAll("#ctxa .cpane").forEach(p => { p.hidden = p.dataset.ct !== ST.ctab; });
}
function showCtab(ct) {
  if (!CTABS.includes(ct)) return;
  ST.ctab = ct; applyCtab(); saveShell();
  if (ct === "tx") renderTranscript();
  if (ct === "event") renderEventPane();
  if (ct === "rec") renderRecPane();
  renderCtxExtra();
}
// the context area's header actions follow the tab
function renderCtxExtra() {
  const el = $("ctx-extra"); const r = ST.detail;
  if (!r || ST.view !== "normal") { el.innerHTML = ""; return; }
  const st = r.stages || {};
  if (ST.ctab === "rec") {
    el.innerHTML = `<button type="button" class="btn sm" id="d-detect">${icon("target")}${st.detect ? "Re-detect" : "Detect"}</button>
      <button type="button" class="btn sm" id="d-transcribe">${icon("mic")}${(r.transcript && r.transcript.n) || st.transcribe ? "Re-transcribe" : "Transcribe"}</button>
      <button type="button" class="ib sm" data-menu="rec" data-tip="More: reveal, copy path, re-probe, zones">${icon("more")}</button>`;
    $("d-detect").onclick = () => detectRec(r.id);
    $("d-transcribe").onclick = () => transcribeRec(r.id);
  } else if (ST.ctab === "tx") {
    const sum = ST.transcript.rec_id === r.id && ST.transcript.summary ? ST.transcript.summary : (r.transcript || { n: 0 });
    el.innerHTML = `<button type="button" class="btn sm" id="t-run">${icon("mic")}${sum.n || st.transcribe ? "Re-transcribe" : "Transcribe"}</button>
      <button type="button" class="btn sm ghost" id="t-all" title="transcribe the whole library: pick what to include (newest first)">Transcribe all...</button>`;
    $("t-run").onclick = () => transcribeRec(r.id);
    $("t-all").onclick = openTranscribeAll;
  } else el.innerHTML = "";
}

function setPlayerAr(ar) {
  if (!(ar > 0.3 && ar < 5)) return;
  $("review").style.setProperty("--pl-ar", ar.toFixed(4));
}
function recLabel(r) {
  if (!r) return "";
  if (r.kind === "vod" && r.title) return r.title;
  const base = r.kind === "replay" ? "Replay" : r.kind === "vod" ? "VOD" : "Recording";
  return `${base} \u00b7 ${fmtWhen(recStart(r) || r.mtime)}`;
}
// the file name without its extension and date stamp ("Replay_2026-01-02_20-15-00.mp4" -> "Replay")
function nameStem(name) {
  const s = String(name || "").replace(/\.[^.]+$/, "").replace(/[_ -]*\d{4}-\d{2}-\d{2}[_ T]\d{2}-\d{2}-\d{2}/, "").replace(/_/g, " ").trim();
  return s || String(name || "");
}
// a hit / job row only carries the file name: the loaded row's label, else kind + the name's stamp
function recLabelFor(id, name) {
  const r = (ST.recs || []).find(x => x.id === id) || (ST.detail && ST.detail.id === id ? ST.detail : null);
  if (r) return recLabel(r);
  const m = String(name || "").match(/(\d{4})-(\d{2})-(\d{2})[_ T](\d{2})-(\d{2})-(\d{2})/);
  if (!m) return nameStem(name);
  const when = fmtWhen(new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]).getTime() / 1000);
  const stem = nameStem(name);
  return /^replay\b/i.test(stem) ? `${stem} · saved ${when}` : `${stem} · ${when}`;
}
function statusBadge(r) {
  if (r.missing) return `<span class="badge warn" title="the file is not where it was">missing</span>`;
  if (r.status === "error") return `<span class="badge bad" title="${esc(r.error || "")}">${icon("warn")}probe failed</span>`;
  if (r.status === "new") return `<span class="badge" title="not probed yet">new</span>`;
  return "";
}
function renderCrumb() {
  const r = ST.detail; let h = "";
  if (r) h += `<span class="sep">/</span><b title="${esc(r.path || r.name)} - click: show it in the Library">${esc(recLabel(r))}</b><span class="muted num">${fmtDurShort(r.duration_s)}</span>${statusBadge(r)}`;
  if (ST.view === "calibration") h += `<span class="sep">/</span><span class="view">Calibration</span>`;
  else if (ST.view === "zones") h += `<span class="sep">/</span><span class="view">Detection zones</span>`;
  $("crumb").innerHTML = h;
}
// time labels under the timeline strip (player.js draws the strip itself)
function renderTlScale() {
  const el = $("tl-scale"); const r = ST.detail; if (!el) return;
  if (!r || !r.duration_s) { el.innerHTML = ""; return; }
  const D = r.duration_s; const step = [15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400].find(s => D / s <= 12) || 28800;
  let h = ""; for (let x = step; x < D - step * 0.4; x += step) h += `<span style="left:${(x / D * 100).toFixed(3)}%">${tc(x)}</span>`;
  el.innerHTML = h;
}

/* ---------------- tooltips, popovers, menus, lightbox ---------------- */
let _tipT = null;
function wireTips() {
  const tip = $("tip");
  document.addEventListener("mouseover", (e) => {
    const el = e.target.closest && e.target.closest("[data-tip]");
    clearTimeout(_tipT); tip.classList.remove("on"); if (!el) return;
    _tipT = setTimeout(() => {
      if (!document.body.contains(el)) return;
      tip.innerHTML = esc(el.dataset.tip) + (el.dataset.k ? `<span class="kbd">${esc(el.dataset.k)}</span>` : "");
      const rc = el.getBoundingClientRect(); tip.classList.add("on");
      if (el.dataset.side === "r") { tip.style.left = rc.right + 10 + "px"; tip.style.top = rc.top + rc.height / 2 - tip.offsetHeight / 2 + "px"; }
      else {
        tip.style.left = Math.max(8, Math.min(innerWidth - tip.offsetWidth - 8, rc.left + rc.width / 2 - tip.offsetWidth / 2)) + "px";
        tip.style.top = (rc.top - tip.offsetHeight - 8 < 4 ? rc.bottom + 8 : rc.top - tip.offsetHeight - 8) + "px";
      }
    }, 300);
  });
  document.addEventListener("mousedown", () => { clearTimeout(_tipT); tip.classList.remove("on"); }, true);
}
let popAnchor = null;
// one popover at a time (#pop): menus, the kinds filter, the "i" details, Send to Resolve.
// anchor = an element (opens under it, right-aligned), {x, y} (at a point) or null (under the top bar, right)
function showPop(anchor, html, cls) {
  const p = $("pop");
  p.className = "pop" + (cls ? " " + cls : ""); p.innerHTML = html; p.hidden = false; hydrateIcons(p);
  popAnchor = anchor && anchor.getBoundingClientRect ? anchor : null;
  placePop(p, anchor);
  return p;
}
function placePop(p, anchor) {
  const point = anchor && anchor.x != null && !anchor.getBoundingClientRect;
  const rc = anchor && anchor.getBoundingClientRect ? anchor.getBoundingClientRect()
    : point ? { left: anchor.x, right: anchor.x, top: anchor.y, bottom: anchor.y } : { left: innerWidth - 16, right: innerWidth - 16, top: 52, bottom: 52 };
  p.style.left = "0px"; p.style.top = "0px";
  const pw = p.offsetWidth, ph = p.offsetHeight;
  let x = point ? rc.left : rc.right - pw; if (x < 8) x = Math.max(8, rc.left);
  x = Math.max(8, Math.min(x, innerWidth - pw - 8));
  let y = rc.bottom + 6; if (y + ph > innerHeight - 8) y = Math.max(8, rc.top - ph - 6);
  p.style.left = x + "px"; p.style.top = y + "px";
}
function closePop() { const p = $("pop"); if (!p.hidden) { p.hidden = true; p.innerHTML = ""; p.className = "pop"; } popAnchor = null; }
// items: [{ic, label, sub, k, act, danger, on, disabled} | "-" | {h}]
function openMenu(anchor, items) {
  const html = items.map((it, i) => it === "-" ? `<div class="msep"></div>` : it.h ? `<div class="mhdr">${esc(it.h)}</div>` :
    `<button type="button" class="mi${it.danger ? " danger" : ""}${it.on ? " on" : ""}" data-i="${i}" ${it.disabled ? "disabled" : ""}>${icon(it.on ? "check" : (it.ic || "chev"))}<span>${esc(it.label)}${it.sub ? `<small>${esc(it.sub)}</small>` : ""}</span>${it.k ? `<span class="k">${esc(it.k)}</span>` : ""}</button>`).join("");
  const p = showPop(anchor, html, "menu");
  p.querySelectorAll(".mi[data-i]").forEach(b => b.onclick = (e) => {
    e.stopPropagation(); closePop();
    Promise.resolve().then(() => items[+b.dataset.i].act()).catch(err => toast(err.message, 5000));
  });
}
function openLightbox(src) { const lb = $("lightbox"); lb.querySelector("img").src = src; lb.hidden = false; }

// the "..." menus (data-menu="<name>" on a button; data-arg passes a recording id)
const MENUS = {
  library: () => [
    { ic: "scan", label: "Scan new", sub: "walk the library roots for new, moved and missing files", act: scan },
    { ic: "probe", label: "Probe all", sub: "queue a probe for every recording that has none yet", act: probeAll },
    { ic: "tx", label: "Transcribe all...", act: openTranscribeAll },
    { ic: "target", label: "Detect all missing", act: detectAll },
    { ic: "bolt", label: "Proxies for all with events", act: proxiesAll }, "-",
    { ic: "link", label: "Import VOD from a link...", act: openVod },
    { ic: "vods", label: "Browse stream VODs", act: () => openVods() }],
  events: () => [
    { ic: "target", label: "Detect all missing", sub: "every probed recording without a detect, newest first", act: detectAll },
    { ic: "calib", label: "Calibrate...", sub: "HUD boxes, ticker wording, your name", act: () => openCalibration(ST.sel || 0) },
    { ic: "zones", label: "Detection zones...", act: () => window.ZN && ZN.open(ST.detail ? { rec_id: ST.detail.id } : {}) }],
  jobs: () => [
    { ic: "refresh", label: "Retry failed", act: jobsRetryFailed },
    { ic: "check", label: "Clear finished", sub: "done, failed and skipped", act: jobsClearFinished },
    { ic: "trash", label: "Clear jobs (the queue too)", sub: "running jobs keep going", act: jobsClearAll, danger: true }],
  rec: (arg) => recMenuItems(arg ? +arg : ST.sel),
  event: () => {
    const e = ST.event; if (!e) return [{ label: "no event selected", disabled: true }];
    return [{ ic: "copy", label: "Copy timecode", act: () => copyText(tc(e.t_s)) },
      { ic: "send", label: "Send to Resolve...", sub: "markers / selects / export for a selection", act: () => openHandoff($("btn-send")) },
      { ic: "calib", label: "Calibrate at this moment...", act: () => openCalibration(e.rec_id) },
      ...(e.kind === "manual" ? ["-", { ic: "trash", label: "Delete this manual mark", danger: true, act: () => deleteManual(e) }] : [])];
  },
};
function recMenuItems(id) {
  const r = ST.recs.find(x => x.id === id) || (ST.detail && ST.detail.id === id ? ST.detail : null); if (!r) return [{ label: "no recording selected", disabled: true }];
  const st = r.stages || {};
  return [
    { ic: "target", label: st.detect ? "Re-detect" : "Detect kills / deaths", act: () => detectRec(id) },
    { ic: "events", label: "Show events", act: () => { select(id, { events: true }); openPanel("events"); } },
    { ic: "mic", label: r.n_segments || st.transcribe ? "Re-transcribe" : "Transcribe", act: () => transcribeRec(id) },
    { ic: "tx", label: "Show transcript", act: () => { select(id, { transcript: true }); showCtab("tx"); } },
    { ic: "zones", label: "Detection zones...", act: () => window.ZN && ZN.open({ rec_id: id }) },
    ...(r.kind === "vod" && r.channel_key ? [{ ic: "vods", label: `More VODs from ${r.channel || r.channel_key}`,
      act: () => window.VB && VB.open({ provider: provOf(r.name), key: r.channel_key, name: r.channel, url: r.channel_url }) }] : []),
    "-",
    { ic: "folder", label: "Reveal in Explorer", act: () => post(`/api/recordings/${id}/reveal`) },
    { ic: "copy", label: "Copy path", act: () => copyText(r.path) },
    { ic: "probe", label: "Re-probe", act: async () => { await post(`/api/recordings/${id}/probe`); toast("probe queued"); } }];
}

/* the kinds filter: ONE compact button + popover (Events, the timeline, Find clips) */
function kindsBtn() {
  const on = KINDS.filter(k => ST.chips[k]);
  return `<button type="button" class="btn sm kbtn" data-kinds title="which kinds show in Events, the timeline and Find clips">${icon("filter")}<span class="kds">${on.slice(0, 5).map(k => `<i class="k-${k}"></i>`).join("")}</span>${on.length === KINDS.length ? "All kinds" : on.length + " of " + KINDS.length}${icon("chevd")}</button>`;
}
function paintKindsBtns() { document.querySelectorAll("[data-kinds-slot]").forEach(s => { s.innerHTML = kindsBtn(); }); }
function openKinds(anchor) {
  const counts = {}; for (const e of (ST.events.rec_id === ST.sel ? ST.events.list : [])) counts[e.kind] = (counts[e.kind] || 0) + 1;
  const draw = () => {
    const p = $("pop");
    p.innerHTML = `<div class="mhdr">Show kinds</div>` + KINDS.map(k => `<button type="button" class="kopt k-${k} ${ST.chips[k] ? "on" : ""}" data-k="${k}"><span class="cbx ${ST.chips[k] ? "on" : ""}"><svg viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7"/></svg></span><span class="kd"></span><span>${KIND_NAME[k]}${k === "revive" || k === "squad" ? ` <small class="faint">off by default</small>` : ""}</span><span class="num t-xs muted" title="in this recording">${counts[k] || ""}</span></button>`).join("") +
      `<div class="msep"></div><div class="acts" style="padding:4px"><button type="button" class="btn ghost sm" data-all="1">All</button><button type="button" class="btn ghost sm" data-all="0">Highlights only</button></div>`;
    p.querySelectorAll(".kopt").forEach(b => b.onclick = (e) => { e.stopPropagation(); ST.chips[b.dataset.k] = !ST.chips[b.dataset.k]; kindsChanged(); draw(); });
    p.querySelectorAll("[data-all]").forEach(b => b.onclick = (e) => { e.stopPropagation(); for (const k of KINDS) ST.chips[k] = b.dataset.all === "1" || KIND_DEFAULT[k]; kindsChanged(); draw(); });
  };
  showPop(anchor, "", "kfilter"); draw(); placePop($("pop"), anchor);
}
function kindsChanged() {
  const pv = popAnchor && popAnchor.matches && popAnchor.matches("[data-kinds]") ? popAnchor.closest(".pv") : null;
  if (ST.tab === "events") renderEvents();
  if (window.PL) PL.renderTimeline();
  finderChanged();
  paintKindsBtns();
  // both re-renders replace the button: point the open popover at its successor so a second click folds it
  if (pv && popAnchor && !popAnchor.isConnected) { const nb = pv.querySelector("[data-kinds]"); if (nb) popAnchor = nb; }
}

/* ---------------- status bar ---------------- */
function setStatus(text) { $("status").textContent = text && text !== "-" ? text : ""; }
async function refreshStatus() {
  try { ST.status = await api("/api/status"); } catch (err) { setStatus("server unreachable"); return; }
  ST.counts = ST.status.jobs;
  renderLegend(); renderPill();
  const drives = (ST.status.library.drives || []).map(d => d.drive).filter(Boolean);
  const sel = $("sel-drive"); const cur = sel.value;
  sel.innerHTML = `<option value="">All drives</option>` + drives.map(d => `<option value="${esc(d)}" ${d === cur ? "selected" : ""}>${esc(d)}</option>`).join("");
}
function renderLegend() {
  const s = ST.status; if (!s) return;
  const t = s.library.total;
  const per = (s.library.drives || []).map(d => `${d.drive || "?"} ${d.n} files, ${fmtHours(d.secs || 0)}, ${fmtBytes(d.bytes)}${d.missing ? `, ${d.missing} missing` : ""}`).join("\n");
  const lg = $("legend");
  lg.innerHTML = `${t.files} files \u00b7 ${Math.round(t.secs / 3600)} h \u00b7 ${fmtBytes(t.bytes)} \u00b7 ${t.probed} probed${t.missing ? ` \u00b7 <span class="bad">${t.missing} missing</span>` : ""}`;
  lg.title = per;
  const ls = s.library.last_scan; const watch = ST.settings && ST.settings.library && ST.settings.library.auto_scan_new;
  $("sb-watch").textContent = `${watch ? "Library watcher on" : "Library watcher off"} \u00b7 ${ls ? `last scan ${fmtAgo(ls.started_utc)} (${ls.elapsed_s} s)` : "not scanned yet"}${s.ffmpeg && !s.ffmpeg.found ? " \u00b7 ffmpeg MISSING" : ""}`;
}
function renderPill() {
  const c = ST.counts || {}; const p = $("pill-jobs");
  const dot = c.paused ? "warn" : c.running ? "acc pulse" : c.failed ? "bad" : "ok";
  p.querySelector(".sd").className = "sd " + dot;
  $("pill-jobs-t").innerHTML = (c.paused ? "Jobs paused" : c.running ? `${c.running} running` : "Idle") + (c.queued ? ` \u00b7 ${c.queued} queued` : "") + (c.failed ? ` \u00b7 <span class="bad">${c.failed} failed</span>` : "");
  p.classList.toggle("paused", !!c.paused);
  const b = $("btn-pause"); b.innerHTML = icon(c.paused ? "play" : "pausej"); b.dataset.tip = c.paused ? "Resume the job lanes" : "Pause the job lanes (running jobs finish; handy while gaming)";
  b.classList.toggle("paused", !!c.paused);
  const jp = $("j-pause"); if (jp) { jp.innerHTML = icon(c.paused ? "play" : "pausej") + (c.paused ? "Resume" : "Pause"); jp.classList.toggle("pri", !!c.paused); }
  renderRail();
}
async function togglePause() {
  try { const r = await post("/api/jobs/pause", { paused: !ST.counts.paused }); ST.counts.paused = r.paused; renderPill(); toast(r.paused ? "job lanes paused: running jobs finish, nothing new starts" : "job lanes resumed"); }
  catch (err) { toast(err.message, 5000); }
}
const encLabel = (c) => c === "libx264" ? "x264" : c === "h264_nvenc" ? "NVENC" : c === "hevc_nvenc" ? "NVENC HEVC" : (c || "-");
function refreshEncoder() {
  return api("/api/system/encoder").then(e => {
    ST.encoder = e; const el = $("sb-enc");
    el.textContent = encLabel(e.clip_effective) + (e.proxy_effective && e.proxy_effective !== e.clip_effective ? ` / ${encLabel(e.proxy_effective)}` : "");
    el.dataset.tip = `clips ${encLabel(e.clip_effective)}, proxies ${encLabel(e.proxy_effective)} - auto would pick ${encLabel(e.auto_now)} (${e.why})`;
    return e;
  }).catch(() => null);
}

/* ---------------- DaVinci Resolve ---------------- */
async function refreshResolve(fresh) {
  try { ST.resolve = await api(`/api/resolve/status${fresh ? "?fresh=1" : ""}`); } catch (e) { return; }
  renderResolvePill();
}
// the link (resolve/link.py): starting Resolve / waiting for a project / running the hand-offs that waited
// The link's state is only live while its watcher runs (or a hand-off waits); after that it is history
// and the plain status (connected / has_project) is the truth. A 'failed' stays visible until Resolve is
// actually ready, so the reason (and "Start Resolve again") is not lost.
function resolveLink() {
  const L = (ST.resolve && ST.resolve.link) || { state: "idle", waiting: [] }; const r = ST.resolve || {};
  const live = L.watching || (L.waiting || []).length;
  if (live || (L.state === "failed" && !(r.connected && r.has_project))) return L;
  return Object.assign({}, L, { state: "idle" });
}
function resolveStartable() { const r = ST.resolve || {}; return !!(r.enabled && r.available && !(r.connected && r.has_project) && resolveLink().state !== "starting"); }
function renderResolvePill() {
  const r = ST.resolve || {}; const L = resolveLink(); const p = $("pill-resolve");
  const n = (L.waiting || []).length; const w = n ? ` (${n} waiting)` : "";
  let text, dot = "", tip;
  if (!r.enabled) { text = "Resolve off"; tip = "Settings > Resolve"; }
  else if (!r.available) { text = "Resolve not installed"; dot = "bad"; tip = r.error || ""; }
  else if (L.state === "starting") { text = "Resolve starting..." + w; dot = "warn pulse"; tip = "DaVinci Resolve is starting -- open a project when the Project Manager shows"; }
  else if (L.state === "no_project") { text = "Resolve: open a project" + w; dot = "warn"; tip = L.error || "open a project in Resolve's Project Manager"; }
  else if (L.state === "failed") { text = "Resolve failed"; dot = "bad"; tip = L.error || ""; }
  else if (r.connected && r.has_project) { text = `Resolve \u00b7 ${r.project}` + w; dot = "ok"; tip = `${r.product || "Resolve"} ${r.version || ""} - project ${r.project}${r.timeline ? ", timeline " + r.timeline : ""}`; }
  else if (r.connected) { text = "Resolve: no project" + w; dot = "warn"; tip = "Resolve is running without a project (or with the Untitled Project)"; }
  else { text = "Resolve not running" + w; tip = r.error || ""; }
  $("pill-resolve-t").textContent = text; p.querySelector(".sd").className = "sd " + dot;
  const startable = resolveStartable();
  $("pill-resolve-start").hidden = !(startable && !r.connected);
  p.dataset.tip = (tip ? tip + " | " : "") + (n || ["starting", "no_project", "failed"].includes(L.state) ? "click: what waits (cancel, use the open project, start again)" : startable ? "click: start Resolve and wait for a project" : "click: check again");
  const box = $("ho-wait"); if (box) box.innerHTML = renderResolveWait();
  const hb = $("ho-badge"); if (hb) hb.outerHTML = handoffBadge();
  wireResolveWait();
}
// the status-bar pill: while hand-offs wait (or the start failed) it opens what waits, with Cancel /
// "Use <project>" / "Start Resolve again"; otherwise it starts Resolve or checks again
function openResolveWait() {
  const p = $("pill-resolve");
  showPop(p, `<div class="handoff"><h5>${icon("resolve")}DaVinci Resolve<span class="sp"></span>${handoffBadge()}</h5><div id="ho-wait">${renderResolveWait()}</div>
    <div class="acts"><button type="button" class="btn sm" id="ho-check">${resolveStartable() ? "Start Resolve" : "Check again"}</button><button type="button" class="btn ghost sm" id="ho-open">Send to Resolve...</button></div></div>`, "handoff");
  wireResolveWait();
  $("ho-check").onclick = (e) => { e.stopPropagation(); closePop(); resolveStartOrCheck(); };
  $("ho-open").onclick = (e) => { e.stopPropagation(); openHandoff(); };
}
function resolvePillClick(e) {
  if (e && e.stopPropagation) e.stopPropagation();
  if (popAnchor === $("pill-resolve")) { closePop(); return; }
  if (renderResolveWait()) { openResolveWait(); return; }
  resolveStartOrCheck();
}
async function resolveStartOrCheck() {
  if (resolveStartable()) {
    try { const x = await post("/api/resolve/start"); ST.resolve.link = x.link; renderResolvePill(); toast("starting DaVinci Resolve -- open a project when it is up", 4000); }
    catch (err) { toast(err.message, 6000); }
    return;
  }
  $("pill-resolve-t").textContent = "Resolve: checking..."; refreshResolve(true);
}
// what waits for Resolve, with Cancel / "use the open project"
function renderResolveWait() {
  const L = resolveLink(); const ops = L.waiting || [];
  if (!ops.length && !["starting", "no_project", "failed"].includes(L.state)) return "";
  const st = { starting: "Resolve is starting...", no_project: L.error || "open a project in Resolve", failed: L.error || "failed", ready: "running...", down: "waiting for Resolve" }[L.state] || L.state;
  let h = `<div class="howait"><b>${ops.length ? `${ops.length} hand-off${ops.length === 1 ? "" : "s"} waiting` : "Resolve"}</b> <span class="muted">${esc(st)}</span>`;
  if (L.state === "no_project" && ST.resolve && ST.resolve.connected && ST.resolve.project) h += ` <button type="button" class="btn sm" id="ho-useopen" title="send to '${esc(ST.resolve.project)}' as it is">Use "${esc(ST.resolve.project)}"</button>`;
  if (L.state === "failed") h += ` <button type="button" class="btn sm" id="ho-retry">Start Resolve again</button>`;
  if (ops.length) h += ` <button type="button" class="btn ghost sm" id="ho-cancelall">Cancel</button>`;
  h += ops.map(o => `<div class="t-xs">#${o.id} ${esc(o.label)} - ${esc(o.state)}${o.note ? ` <span style="color:var(--warn)">(${esc(o.note)})</span>` : ""}</div>`).join("");
  return h + `</div>`;
}
function wireResolveWait() {
  const u = $("ho-useopen"); if (u) u.onclick = (e) => { e.stopPropagation(); post("/api/resolve/use-open-project").then(x => { ST.resolve.link = x.link; renderResolvePill(); }).catch(err => toast(err.message, 5000)); };
  const c = $("ho-cancelall"); if (c) c.onclick = (e) => { e.stopPropagation(); post("/api/resolve/cancel", {}).then(x => { ST.resolve.link = x.link; renderResolvePill(); toast(`cancelled ${x.cancelled}`); }).catch(err => toast(err.message, 5000)); };
  const t = $("ho-retry"); if (t) t.onclick = (e) => { e.stopPropagation(); resolveStartOrCheck(); };
}
function handoffMsg(kind, r) {
  if (r.note) return `${kind === "markers" ? "markers" : "selects timeline"}: ${r.note}`;
  if (kind === "markers") return `${r.sent} markers sent to ${r.project}` + ((r.imported || []).length ? `, ${r.imported.length} files imported` : "") + ((r.bins || []).length ? ` -- media pool bin ${r.bins.join(", ")}` : "") + ((r.failed || []).length ? `, ${r.failed.length} failed` : "");
  return `timeline "${r.timeline}" in ${r.project}: ${r.clips} clips from ${r.events} events` + ((r.failed || []).length ? `, ${r.failed.length} failed` : "");
}
// the hand-off selection: this recording or the whole library, by status / score / kind
function handoffSelection(extra) {
  const h = ST.handoff; const sel = { status: h.status, order: h.order };
  if (h.scope === "rec" && ST.detail) sel.rec_ids = [ST.detail.id];
  if (+h.min_score > 0) sel.min_score = +h.min_score;
  if (h.kinds) sel.kinds = h.kinds;
  return Object.assign(sel, extra || {});
}
async function handoff(action, extra) {
  const res = $("ho-res"); const sel = handoffSelection(extra);
  const label = { markers: "sending markers", selects: "building the selects timeline", export: "exporting" }[action];
  if (res) res.textContent = label + "...";
  try {
    let r;
    if (action === "markers" || action === "selects") {
      r = await post(`/api/resolve/${action}`, sel);
      if (r.queued) {
        // Resolve is down / has no project: it is being started and the hand-off runs once a project is open
        if (ST.resolve) ST.resolve.link = r.link;
        renderResolvePill();
        const msg = `waiting for Resolve (${r.link.state === "starting" ? "starting it" : r.link.state === "no_project" ? "open a project" : "connecting"}) -- the ${action === "markers" ? "markers follow" : "timeline follows"} automatically (the Resolve pill in the status bar shows what waits)`;
        toast(msg, 6000); if ($("ho-res")) $("ho-res").textContent = msg;
        return r;
      }
      const msg = handoffMsg(action, r);
      toast(msg, 5000); if ($("ho-res")) $("ho-res").textContent = msg + (action === "markers" && r.failed.length ? "\n" + r.failed.map(f => `#${f.event_id}: ${f.error}`).join("\n") : "");
      if (action === "markers" && ST.sel) loadEvents(ST.sel);       // the "in Resolve" badges
    } else {
      r = await post("/api/export", Object.assign(sel, { format: "both" }));
      if ($("ho-res")) $("ho-res").innerHTML = esc(`${r.events} events, ${r.clips} clips -> ${r.out_dir}`) + "<br>" + r.files.map(f => `<a href="${esc(f.url)}">${esc(f.name)}</a>`).join(" | ");
      toast(`exported ${r.files.map(f => f.kind).join(" + ")}`);
    }
    return r;
  } catch (err) { toast(err.message, 8000); if ($("ho-res")) $("ho-res").textContent = err.message; }
}
async function handoffPreview() {
  const el = $("ho-prev"); if (!el) return;
  if (ST.handoff.scope === "rec" && !ST.detail) { el.textContent = "pick a recording first (or send from every recording)"; return; }
  try { const r = await post("/api/resolve/preview", handoffSelection()); if ($("ho-prev")) $("ho-prev").innerHTML = `<b>${r.events}</b> events \u2192 <b>${r.clips}</b> clips <span class="faint">(overlaps merged)</span> \u00b7 ${fmtDur(r.seconds)} from ${r.recordings} recording${r.recordings === 1 ? "" : "s"}`; }
  catch (err) { if ($("ho-prev")) $("ho-prev").textContent = err.message; }
}
function handoffBadge() { const r = ST.resolve || {}; return `<span class="badge ${r.connected ? "ok" : ""}" id="ho-badge">${r.connected ? "Resolve: " + esc(r.project || "no project") : "Resolve not connected"}</span>`; }
function renderHandoff() {
  const h = ST.handoff; const noRec = h.scope === "rec" && !ST.detail;
  const opt = (v, cur, label) => `<option value="${v}" ${String(v) === String(cur) ? "selected" : ""}>${label}</option>`;
  return `<div class="handoff"><h5>${icon("resolve")}Send to DaVinci Resolve<span class="sp"></span>${handoffBadge()}</h5>
    <div class="hrow"><span>Events from</span><div class="seg" id="ho-scope"><button type="button" data-s="rec" class="${h.scope === "rec" ? "on" : ""}">This recording</button><button type="button" data-s="all" class="${h.scope === "all" ? "on" : ""}">Every recording</button></div></div>
    <div class="hrow"><span>Status</span><select class="inp" id="ho-status">${opt("kept", h.status, "Kept only")}${opt("kept,new", h.status, "Kept + new")}${opt("any", h.status, "Everything not false")}</select></div>
    <div class="hrow"><span>Min score</span><input class="inp" type="number" id="ho-min" step="0.1" min="0" value="${h.min_score}"></div>
    <div class="hrow"><span>Kinds</span><input class="inp" type="text" id="ho-kinds" placeholder="all (e.g. kill,assist)" value="${esc(h.kinds)}"></div>
    <div class="hrow"><span>Order</span><select class="inp" id="ho-order">${opt("time", h.order, "Time")}${opt("score", h.order, "Score (best first)")}</select></div>
    <div class="hprev" id="ho-prev">-</div>
    <div class="acts"><button type="button" class="btn pri sm" id="ho-markers" ${noRec ? "disabled" : ""} title="one marker per event on the recording's media-pool clip (imported into the WardogClips bin if missing)">Markers</button>
      <button type="button" class="btn sm" id="ho-selects" ${noRec ? "disabled" : ""} title="a new timeline with one clip per event (in / out), overlapping clips merged">Selects timeline</button>
      <button type="button" class="btn sm" id="ho-export" ${noRec ? "disabled" : ""} title="works with Resolve closed">Export CSV + EDL</button></div>
    <div id="ho-wait">${renderResolveWait()}</div>
    <div class="res" id="ho-res"></div></div>`;
}
function openHandoff(anchor) {
  showPop(anchor || $("btn-send"), renderHandoff(), "handoff");
  wireHandoff();
}
function wireHandoff() {
  const h = ST.handoff;
  const bind = (id, key, num) => { const el = $(id); if (el) el.onchange = () => { h[key] = num ? (parseFloat(el.value) || 0) : el.value.trim(); handoffPreview(); }; };
  bind("ho-status", "status"); bind("ho-min", "min_score", true); bind("ho-kinds", "kinds"); bind("ho-order", "order");
  $("ho-scope").querySelectorAll("button").forEach(b => b.onclick = (e) => { e.stopPropagation(); h.scope = b.dataset.s; const p = $("pop"); p.innerHTML = renderHandoff(); hydrateIcons(p); wireHandoff(); });
  $("ho-markers").onclick = (e) => { e.stopPropagation(); handoff("markers"); };
  $("ho-selects").onclick = (e) => { e.stopPropagation(); handoff("selects"); };
  $("ho-export").onclick = (e) => { e.stopPropagation(); handoff("export"); };
  wireResolveWait();
  handoffPreview();
}

/* ---------------- library actions ---------------- */
async function scan() {
  setStatus("scanning...");
  try {
    const s = await post("/api/library/scan");
    setStatus(`scan: ${s.found} files, ${s.new} new, ${s.moved} moved, ${s.missing} missing${s.growing ? `, ${s.growing} still recording` : ""}`);
    const bad = s.roots.filter(r => !r.ok); if (bad.length) toast("root not reachable: " + bad.map(r => r.root).join(", "), 6000);
    await refreshStatus(); await loadRecs();
  } catch (err) { setStatus("scan failed"); toast(err.message, 6000); }
}
const probeAll = () => post("/api/recordings/probe-all").then(r => toast(`queued ${r.queued} probes (${r.candidates} candidates)`)).catch(err => toast(err.message, 5000));
const detectAll = () => post("/api/detect-all").then(x => toast(`detect: queued ${x.queued} of ${x.candidates} recordings`)).catch(err => toast(err.message, 5000));
const proxiesAll = () => post("/api/proxies/build-all").then(r => toast(`queued ${r.queued} proxies (${r.candidates} recordings with events lack one)`)).catch(err => toast(err.message, 5000));

/* ---------------- recordings (Library panel) ---------------- */
async function loadRecs() {
  const u = new URL("/api/recordings", location.origin);
  if (ST.drive) u.searchParams.set("drive", ST.drive);
  if (ST.q) u.searchParams.set("q", ST.q);
  try { ST.recs = (await api(u)).recordings; } catch (err) { toast(err.message, 5000); return; }
  renderRecs(); renderGsMenu();
}
function recStart(r) {
  // a replay's name stamp is the moment the buffer was SAVED (its end); a recording's is its start
  if (!r.name_utc) return null;
  return r.kind === "replay" && r.duration_s ? r.name_utc - r.duration_s : r.name_utc;
}
// detail badges (the "i" popover, the Recording tab); list rows stay calm
function recBadges(r) {
  const b = [];
  if (r.missing) b.push(`<span class="badge bad">missing</span>`);
  else if (r.status === "probed") b.push(`<span class="badge ok">probed</span>`);
  else if (r.status === "error") b.push(`<span class="badge bad" title="${esc(r.error || "")}">error</span>`);
  else b.push(`<span class="badge">new</span>`);
  if (r.kind === "replay") b.push(`<span class="badge replay">replay</span>`);
  if (r.kind === "vod") b.push(`<span class="badge vod" title="${esc(r.source_url || "stream VOD")}">${esc(provOf(r.name) || "VOD")}</span>`);
  return b.join(" ");
}
function recEventsCell(r) {
  const det = r.stages && r.stages.detect;
  const k = r.n_kills || 0, d = r.n_deaths || 0, n = r.n_events || 0;
  const dots = (k ? `<span class="k-kill" title="${k} kills">${k}</span>` : "") + (d ? `<span class="k-death" title="${d} deaths">${d}</span>` : "");
  if (dots) return `<span class="kdots" title="${n} events">${dots}</span>`;
  if (n) return `<span class="ti" title="${n} events (no kills or deaths)">${icon("events")}${n}</span>`;
  if (det && det.state === "done") return `<span class="zero" title="detect ran and found nothing">0 events</span>`;
  return `<span class="zero">not detected</span>`;
}
function recTxCell(r) {
  const jp = ST.jobprog[r.id];
  if (jp && jp.stage === "transcribe") return `<span class="ti acc" title="transcribing">${icon("tx")}${pct(jp.progress)}</span>`;
  if (r.n_segments) return `<span class="ti" title="${r.n_segments} transcript lines">${icon("tx")}${r.n_segments}</span>`;
  const t = r.stages && r.stages.transcribe;
  if (t && t.state === "done") return `<span class="ti zero" title="${esc(t.note || "transcribed: nobody spoke")}">${icon("tx")}0 lines</span>`;
  if (t && t.state === "failed") return `<span class="ti" style="color:var(--bad-text)" title="${esc(t.note || "")}">${icon("tx")}failed</span>`;
  return "";
}
// the row's thumbnail (same 16:9 box as the VOD browser): a frame at 30 % of the file, made once by the server
// (media/thumbs.py); ?v= changes when the file does, so the browser may cache it. No frame -> the empty box stays.
function recThumb(r) {
  const ok = !r.missing && r.status === "probed";
  const v = `${r.size || 0}_${Math.round(r.mtime || 0)}`;
  return `<span class="th">${ok ? `<img src="/api/recordings/${r.id}/thumb?v=${v}" alt="" loading="lazy" decoding="async" onerror="this.remove()">` : ""}</span>`;
}
function renderRecs() {
  const el = $("tab-rec");
  const sub = $("rec-sub"); const bad = ST.recs.filter(r => r.status === "error" || r.missing).length;
  sub.innerHTML = (ST.q ? `<span class="chip on" style="cursor:default">${icon("search")}name contains \u201c${esc(ST.q)}\u201d<button type="button" class="ib sm" style="width:20px;height:20px" id="rec-clrq" title="clear the search">${icon("x")}</button></span>` : "") +
    (bad ? `<span class="badge bad">${icon("warn")}${bad} need attention</span><span class="t-xs muted">failed probe or missing file</span>` : "");
  sub.hidden = !sub.innerHTML;
  const cq = $("rec-clrq"); if (cq) cq.onclick = clearSearch;
  const secs = ST.recs.reduce((a, r) => a + (r.duration_s || 0), 0);
  $("rec-foot").textContent = `${ST.recs.length} recording${ST.recs.length === 1 ? "" : "s"}${secs ? " \u00b7 " + fmtHours(secs) : ""}${ST.drive ? " \u00b7 " + ST.drive : ""}`;
  if (!ST.recs.length) {
    el.innerHTML = `<div class="empty"><b>No recordings match${ST.q ? ` \u201c${esc(ST.q)}\u201d` : ""}</b>${ST.q ? "Press Enter in the search bar to look inside transcripts instead." : "Scan new walks the library roots."}<div class="acts">${ST.q ? `<button type="button" class="btn sm" id="rec-clr2">Clear search</button>` : `<button type="button" class="btn sm" id="rec-scan">${icon("scan")}Scan new</button>`}</div></div>`;
    const a = $("rec-clr2"); if (a) a.onclick = clearSearch; const b = $("rec-scan"); if (b) b.onclick = scan;
    return;
  }
  const byDay = new Map();
  for (const r of ST.recs) { const s = recStart(r) || r.mtime; const d = fmtDate(s).slice(0, 10); if (!byDay.has(d)) byDay.set(d, { utc: s, rows: [] }); byDay.get(d).rows.push(r); }
  let html = "";
  for (const [, g] of byDay) {
    const secs = g.rows.reduce((a, r) => a + (r.duration_s || 0), 0);
    html += `<div class="day"><b>${esc(fmtDay(g.utc))}</b><span>${g.rows.length} recording${g.rows.length === 1 ? "" : "s"}${secs ? " \u00b7 " + fmtDur(secs) : ""}</span></div>`;
    for (const r of g.rows) {
      const jp = ST.jobprog[r.id];
      const title = r.kind === "vod" && r.title ? r.title : nameStem(r.name);
      html += `<div class="row ${r.id === ST.sel ? "sel" : ""} ${r.missing ? "missing" : ""} ${r.status === "error" ? "err" : ""}" data-id="${r.id}" id="rec-${r.id}" title="${esc(r.name)}${r.error ? "\n" + esc(r.error) : ""}">
        ${recThumb(r)}<div class="main"><div class="l1"><span class="tm">${esc(fmtTime(recStart(r) || r.mtime).slice(0, 5))}</span>${r.kind === "vod" ? provBadge(provOf(r.name)) : ""}<span class="t">${esc(title)}</span></div>
          <div class="l2">${r.status === "error" ? `<span class="err">${esc(r.error || "probe failed")}</span>` : recEventsCell(r) + recTxCell(r)}</div></div>
        <div class="r"><span class="num muted t-xs">${r.status === "error" ? "\u2013" : fmtDurShort(r.duration_s)}</span>
          <span class="rr">${statusBadge(r)}<button type="button" class="ib sm info" data-info="${r.id}" title="details: file, codec, size, status" aria-label="details">${icon("info")}</button></span></div>
        <div class="bar" id="bar-${r.id}" ${jp ? "" : "hidden"}><i style="width:${jp ? pct(jp.progress) : 0}"></i></div></div>`;
    }
  }
  el.innerHTML = html;
  el.querySelectorAll(".row[data-id]").forEach(row => {
    row.onclick = (e) => { if (e.target.closest(".info")) return; select(+row.dataset.id); };
    row.oncontextmenu = (ev) => { ev.preventDefault(); openCtx(ev, +row.dataset.id); };
  });
  el.querySelectorAll("button[data-info]").forEach(b => b.onclick = (e) => { e.stopPropagation(); if (popAnchor === b) { closePop(); return; } recInfo(b, +b.dataset.info); });
}
// the "i" popover: the dense facts a list row no longer shows
function recInfo(anchor, id) {
  const r = ST.recs.find(x => x.id === id); if (!r) return;
  const st = Object.entries(r.stages || {}).map(([k, v]) => `<span class="badge ${v.state === "done" ? "ok" : v.state === "failed" ? "bad" : ""}" title="${esc(fmtDate(v.utc))}${v.note ? " - " + esc(v.note) : ""}">${esc(k)}</span>`).join(" ");
  const p = showPop(anchor, `<h5>${esc(r.kind === "vod" && r.title ? r.title : r.name)}</h5><dl class="kv">
    <dt>File</dt><dd class="mono">${esc(r.path || r.name)}</dd>
    <dt>Drive</dt><dd>${esc(r.drive || "-")} \u00b7 ${fmtBytes(r.size)}</dd>
    <dt>Video</dt><dd>${r.width ? `${r.width}\u00d7${r.height} ${esc(r.vcodec || "")} ${r.fps ? Math.round(r.fps) + " fps" : ""} ${fmtMbps(r.bitrate)}` : "unknown"}</dd>
    <dt>${r.kind === "replay" ? "Replay saved" : "Recorded"}</dt><dd>${fmtDate(r.name_utc)}</dd>
    <dt>Modified</dt><dd>${fmtDate(r.mtime)}</dd>
    <dt>Duration</dt><dd>${fmtDur(r.duration_s)}</dd>
    <dt>Status</dt><dd>${recBadges(r)}${r.error ? `<div class="t-xs" style="margin-top:4px;color:var(--bad-text)">${esc(r.error)}</div>` : ""}</dd>
    <dt>Stages</dt><dd>${st || '<span class="muted">none yet</span>'}</dd>
    <dt>Events</dt><dd>${r.n_events ? `${r.n_events} (${r.n_kills || 0} kills, ${r.n_deaths || 0} deaths)` : r.stages && r.stages.detect ? "nothing detected" : "not detected"}</dd>
    <dt>Transcript</dt><dd>${r.n_segments ? r.n_segments + " lines" : r.stages && r.stages.transcribe ? esc(r.stages.transcribe.note || "no speech") : "not transcribed"}</dd>
    ${r.source_url ? `<dt>Source</dt><dd><a href="${esc(r.source_url)}" target="_blank" rel="noopener">${esc(r.channel || r.source_url)}</a></dd>` : ""}
    </dl><div class="acts"><button type="button" class="btn sm" id="ri-open">Open</button><button type="button" class="btn sm ghost" id="ri-reveal">${icon("folder")}Reveal</button>${r.status === "error" || r.missing ? `<button type="button" class="btn sm ghost" id="ri-probe">${icon("probe")}Re-probe</button>` : ""}</div>`, "info");
  p.querySelector("#ri-open").onclick = () => { closePop(); select(id); };
  p.querySelector("#ri-reveal").onclick = () => post(`/api/recordings/${id}/reveal`).catch(err => toast(err.message, 5000));
  const pr = p.querySelector("#ri-probe"); if (pr) pr.onclick = () => post(`/api/recordings/${id}/probe`).then(() => { toast("probe queued"); closePop(); }).catch(err => toast(err.message, 5000));
}
function stepRec(dir) {
  if (!ST.recs.length) return;
  const i = ST.recs.findIndex(r => r.id === ST.sel);
  const n = Math.max(0, Math.min(ST.recs.length - 1, i + dir));
  select(ST.recs[n].id);
  const el = $("rec-" + ST.recs[n].id); if (el) el.scrollIntoView({ block: "nearest" });
}

async function select(id, opts = {}) {
  const moved = ST.sel !== id;
  ST.sel = id;
  document.querySelectorAll(".row[data-id]").forEach(el => el.classList.toggle("sel", +el.dataset.id === id));
  try { ST.detail = await api(`/api/recordings/${id}`); } catch (err) { toast(err.message, 5000); return; }
  if (ST.detail && ST.detail.width && ST.detail.height) setPlayerAr(ST.detail.width / ST.detail.height);
  if (ST.sel !== id) return;   // the user moved on while this loaded
  if (ST.event && ST.event.rec_id !== id) { ST.event = null; }
  if (window.PL) PL.setRecording(ST.detail);
  renderCrumb(); renderTlScale();
  renderStage();
  // the zones editor follows the selection to that recording's streamer (it asks first about unsaved edits)
  if (ST.view === "zones" && moved && window.ZN) ZN.open({ rec_id: id });
  // the calibration view of this recording is already open: a refresh must not reset its frame
  if (ST.view === "calibration") { if (!ST.cal || ST.cal.rec_id !== id) openCalibration(id); return; }
  renderDetail();
  if (ST.ctab === "tx" || opts.transcript) await loadTranscript(id, opts.seg);
  // the timeline strip needs the events whatever panel is open; a jump to an event (clip search) always loads
  if (ST.tab === "events" || opts.events || opts.event || ST.events.rec_id !== id) await loadEvents(id, opts.event, opts.play, opts.isCurrent);
  else if (ST.tab === "events") renderEvents();
}

/* ---------------- context area: Event + Recording tabs ---------------- */
// a live refresh while the user types in a field of the pane (note, ...) would wipe the text: defer it until
// the field loses focus (its change handler saves first)
function deferIfEditing(box, fn) {
  const ae = document.activeElement;
  if (ae && box.contains(ae) && /^(INPUT|TEXTAREA|SELECT)$/.test(ae.tagName)) {
    if (!ae.dataset.wcDefer) { ae.dataset.wcDefer = "1"; ae.addEventListener("blur", () => setTimeout(fn, 0), { once: true }); }
    return true;
  }
  return false;
}
function renderDetail() {
  renderCrumb(); updateTxCount();
  if (ST.view !== "normal") return;
  renderEventPane(); renderRecPane(); renderCtxExtra();
}
// the stage = the evidence crops of the selected event, now in the Event tab; also repaints the timeline
function renderStage() {
  if (ST.view !== "normal") return;
  if (window.PL) PL.renderTimeline();
  renderEventPane();
}
function updateTxCount() {
  const n = ST.detail ? (ST.transcript.rec_id === ST.detail.id && ST.transcript.summary ? ST.transcript.summary.n : (ST.detail.transcript && ST.detail.transcript.n) || ST.detail.n_segments || 0) : 0;
  $("ctab-txn").textContent = n ? n : "";
}

function renderEventPane() {
  const el = $("cp-event"); if (ST.view !== "normal") return;
  if (deferIfEditing(el, renderEventPane)) return;
  const r = ST.detail; const e = ST.event && r && ST.event.rec_id === r.id ? ST.event : null;
  if (!r) { el.innerHTML = `<div class="empty"><b>No recording selected</b>Open the Library on the left (or press J) and pick a recording.</div>`; return; }
  if (!e) { el.innerHTML = `<div class="empty"><b>No event selected</b>Pick one in Events or on the timeline, or press N.<div class="acts"><button type="button" class="btn sm" id="ev-open-list">${icon("events")}Show events</button></div></div>`; $("ev-open-list").onclick = () => openPanel("events"); return; }
  el.innerHTML = renderEventCard(e, r);
  hydrateIcons(el);
  wireEventCard(e, r);
}
function eventSignals(e) {
  const src = e.source || {}; const lines = [];
  if (src.ticker) lines.push(["Ticker", src.ticker.join(" | "), 1]);
  if (src.feed) lines.push(["Kill feed", src.feed, 1]);
  if (src.money_delta != null) lines.push(["Account delta", (src.money_delta >= 0 ? "+$" : "-$") + Math.abs(Number(src.money_delta)).toLocaleString()]);
  if (src.xp) lines.push(["XP", src.xp]);
  if (src.distance_m) lines.push(["Distance", src.distance_m + " m"]);
  if (src.dark_s != null) lines.push(["Respawn screen", src.dark_s + " s"]);
  if (src.refined != null) lines.push(["Refine shift", "refined by " + src.refined + " s to the exact frame"]);
  if (src.voice) lines.push(["Voice keywords", src.voice.join(", ")]);
  if (e.kind === "voice" && src.text) lines.push(["Said", src.text]);
  if (e.kind === "voice" && src.over_floor_db != null) lines.push(["Voice level", `${Number(src.rms_db).toFixed(0)} dBFS, ${Number(src.over_floor_db).toFixed(1)} dB over the floor for ${src.dur_s} s`]);
  if (src.near_kind) lines.push(["Near", `a ${src.near_kind} at ${tc(src.near_t)}`]);
  if (src.learned && src.learned.factor != null && Math.abs(src.learned.factor - 1) > 0.005) lines.push(["Learned factor", `x${Number(src.learned.factor).toFixed(2)} from your verdicts on look-alikes (${src.learned.sig})`]);
  if (e.clip_in != null || e.clip_out != null) lines.push(["Your clip range", `${tc(e.clip_in ?? e.t_s)} - ${tc(e.clip_out ?? e.t_s)}`]);
  return lines;
}
function renderEventCard(e, r) {
  const src = e.source || {};
  const crops = [];
  if (e.crop_ticker) crops.push(`<figure data-src="/crops/${esc(e.crop_ticker)}?v=${e.created_utc}" title="click: full size"><img src="/crops/${esc(e.crop_ticker)}?v=${e.created_utc}" alt="reward ticker"><figcaption>Reward ticker</figcaption></figure>`);
  if (e.crop_feed) crops.push(`<figure data-src="/crops/${esc(e.crop_feed)}?v=${e.created_utc}" title="click: full size"><img src="/crops/${esc(e.crop_feed)}?v=${e.created_utc}" alt="kill feed"><figcaption>Kill feed</figcaption></figure>`);
  const cropHtml = crops.join("") || `<div class="nocrop">${icon(e.kind === "voice" ? "mic" : "mark")}<div style="margin-top:8px">${e.kind === "voice" ? "Voice events come from the transcript: no HUD crops" : e.kind === "manual" ? "Manual mark: no crops" : "No evidence crops for this event"}</div></div>`;
  const sig = eventSignals(e);
  const t = ST.transcript.rec_id === r.id ? ST.transcript.segments.filter(s => s.t1 >= e.t_s - 15 && s.t0 <= e.t_s + 15) : null;
  const tx = t === null ? `<div class="muted t-s" style="padding:8px">loading transcript...</div>` : t.length ? t.map(s => txLineHtml(s, r, Math.abs(s.t0 - e.t_s) <= 5)).join("")
    : `<div class="muted t-s" style="padding:8px">Nothing said within 15 s${ST.transcript.segments.length ? "" : " (not transcribed)"}.</div>`;
  const stc = { kept: "ok", false: "bad" }[e.status] || "";
  return `<div class="evt">
    <div class="crops">${cropHtml}</div>
    <div class="evmain">
      <div class="evhead"><span class="kb k-${esc(e.kind)}">${kindLine(e)}</span><h3>${evTitle(e, { bare: true, nosub: true })}</h3>
        ${e.status !== "new" ? `<span class="badge ${stc}">${esc(e.status === "false" ? "false positive" : e.status)}</span>` : ""}
        ${e.resolve_marker ? `<span class="badge acc" title="${esc(e.resolve_marker)}">in Resolve</span>` : ""}</div>
      <div class="evmeta"><span>at <b class="tc">${tc(e.t_s)}</b></span><span>${esc(fmtWhen((recStart(r) || 0) + e.t_s))}</span>${e.amount ? `<span><b>+$${Number(e.amount).toLocaleString()}</b></span>` : ""}${src.distance_m ? `<span><b>${src.distance_m} m</b></span>` : ""}${e.kind === "voice" && e.subkind !== "keyword" && src.dur_s != null ? `<span>for <b>${Number(src.dur_s).toFixed(1)} s</b></span>` : ""}</div>
      <div class="verdict">
        <button type="button" class="vbtn keep ${e.status === "kept" ? "on" : ""}" data-st="kept" data-tip="Keep" data-k="Y">${icon("check")}Keep <span class="kbd">Y</span></button>
        <button type="button" class="vbtn false ${e.status === "false" ? "on" : ""}" data-st="false" data-tip="False positive" data-k="X">${icon("x")}False positive <span class="kbd">X</span></button>
        ${e.status !== "new" ? `<button type="button" class="btn ghost sm" data-st="new" data-tip="Back to new" data-k="U">Reset</button>` : ""}
        <span class="sp"></span>
        <button type="button" class="btn sm" id="ev-resolve" title="a marker for this one event on the recording's clip in the open Resolve project">${icon("resolve")}Marker</button>
        <button type="button" class="ib sm" data-menu="event" data-tip="More: copy timecode, delete a manual mark">${icon("more")}</button>
      </div>
      <div class="evmeters"><div class="evmeter"><div class="l"><span>Score</span><b>${e.score.toFixed(2)}</b></div><div class="b"><i style="width:${Math.min(100, e.score / 3 * 100).toFixed(1)}%"></i></div></div>
        <div class="evmeter"><div class="l"><span>Confidence</span><b>${pct(e.confidence)}</b></div><div class="b"><i class="ok" style="width:${pct(e.confidence)}"></i></div></div></div>
      <div class="field-l">Note <span class="faint">\u00b7 goes into the Resolve marker, saves as you type</span></div>
      <input class="inp" type="text" id="ev-note" placeholder="why this one is good..." value="${esc(e.note || "")}">
      <details class="disc-d" id="ev-sig" ${ST.sigOpen ? "open" : ""}><summary>${icon("chev", "chev")}Detection signals <span class="faint">${sig.length}</span></summary>
        ${sig.length ? `<dl class="kv">${sig.map(([k, v, m]) => `<dt>${esc(k)}</dt><dd class="num">${esc(v)}</dd>`).join("")}</dl>` : `<div class="muted t-s" style="padding:0 12px 12px">no signals recorded</div>`}</details>
    </div>
    <div class="evtx"><div class="colh">${icon("tx")}Said around it <span class="faint" style="font-weight:400">\u00b115 s</span></div><div id="ev-tx">${tx}</div></div>
  </div>`;
}
function txLineHtml(s, r, near) {
  const who = s.speaker === "me" ? "you" : s.speaker === "squad" ? "squad" : "";
  return `<div class="tline ${near ? "near" : ""} ${s.speaker === "me" ? "me" : ""}" data-t="${s.t0}" title="${esc(fmtDate((recStart(r) || 0) + s.t0))} - click: play from here | alt+click: copy timecode"><span class="tc ${s.avg_logprob != null && s.avg_logprob < -0.8 ? "lo" : ""}">${tc(s.t0)}</span><span>${who ? `<span class="who">${who}</span>` : ""}${esc(s.text)}</span></div>`;
}
let _noteT = null;
function wireEventCard(e, r) {
  const el = $("cp-event");
  el.querySelectorAll(".verdict [data-st]").forEach(b => b.onclick = () => setEventStatus(e.id, b.dataset.st));
  $("ev-resolve").onclick = () => handoff("markers", { event_ids: [e.id], rec_ids: undefined, status: "any", min_score: undefined, kinds: undefined });
  el.querySelectorAll(".crops figure[data-src]").forEach(f => f.onclick = () => openLightbox(f.dataset.src));
  el.querySelectorAll("#ev-tx .tline[data-t]").forEach(l => l.onclick = (ev) => { if (ev.altKey) copyText(tc(+l.dataset.t)); else if (window.PL) PL.seekAbs(r.id, Math.max(0, +l.dataset.t - 0.5)); });
  $("ev-sig").ontoggle = () => { ST.sigOpen = $("ev-sig").open; saveShell(); };
  const note = $("ev-note");
  const saveNote = async (quiet) => {
    clearTimeout(_noteT);
    if (note.value === (e.note || "")) return;
    try { const u = await post(`/api/events/${e.id}`, { note: note.value }); Object.assign(e, u); if (!quiet) toast("note saved"); } catch (err) { toast(err.message, 5000); }
  };
  note.oninput = () => { clearTimeout(_noteT); _noteT = setTimeout(() => saveNote(true), 800); };
  note.onchange = () => saveNote(false);
  note.onkeydown = (ev) => { if (ev.key === "Enter") note.blur(); };
  // one attempt per recording: a failed load must not loop renderDetail -> loadTranscript -> renderDetail
  if (ST.transcript.rec_id !== r.id && ST.transcript.failed !== r.id) loadTranscript(r.id).then(ok => { if (ok && ST.event && ST.event.id === e.id) renderEventPane(); });
}
async function deleteManual(e) {
  try { await del(`/api/events/${e.id}`); ST.event = null; if (window.PL) PL.clearEvent(); await loadEvents(e.rec_id); renderStage(); renderDetail(); toast("manual mark deleted"); } catch (err) { toast(err.message, 5000); }
}

function renderRecPane() {
  const el = $("cp-rec"); if (ST.view !== "normal") return;
  if (deferIfEditing(el, renderRecPane)) return;
  const r = ST.detail;
  if (!r) { el.innerHTML = `<div class="empty"><b>No recording selected</b>Pick one in the Library.</div>`; return; }
  const start = recStart(r);
  const st = r.stages || {}; const jp = ST.jobprog[r.id];
  const ROLE = { mic: "acc", voip: "acc", voice: "acc", game: "ok", desktop: "", music: "", silent: "", dup: "", other: "warn", unknown: "" };
  const ROLE_LABEL = { mic: "mic (you)", voip: "squad", voice: "voice / mix", dup: "duplicate" };
  // a stream VOD's one track is the streamer's mix: the OBS track names don't apply (audio.obs_layout)
  const names = r.kind === "vod" ? [] : ((ST.settings && ST.settings.audio && ST.settings.audio.track_names) || []);
  const tr = (st.transcribe && st.transcribe.tracks) || {};
  const tracks = r.tracks && r.tracks.length ? `<table class="t trk"><tr><th>Track</th><th>Role</th><th>Speech</th><th>Level</th><th>Lines</th></tr>` +
    r.tracks.map(t => `<tr title="${esc([names[t.idx] ? "OBS name " + names[t.idx] : "", t.codec, t.channels ? t.channels + " ch" : ""].filter(Boolean).join(" \u00b7 "))}"><td>A${t.idx + 1}${names[t.idx] ? ` <span class="muted">${esc(names[t.idx])}</span>` : ""}</td><td><span class="badge ${ROLE[t.role] || ""}">${esc(ROLE_LABEL[t.role] || t.role)}</span></td><td>${t.speech_share == null ? "-" : pct(t.speech_share)}</td><td class="nw">${t.rms_mean_db == null ? "-" : t.rms_mean_db.toFixed(0) + "\u00a0dB"}</td><td>${tr[t.idx] ? tr[t.idx].lines : "-"}</td></tr>`).join("") + `</table>` : `<div class="muted t-s">No audio tracks probed yet.</div>`;
  // pipeline: each stage shows its result, or the live state (queued / running % / failed) from the job lanes
  const queued = (stage) => (r.jobs || []).find(j => j.stage === stage && (j.state === "queued" || j.state === "running"));
  const ts = r.transcript || { n: 0 };
  const ev = r.event_counts || {};
  const stg = (stage, label, val) => {
    const s = st[stage]; const live = jp && jp.stage === stage; const q = queued(stage);
    let dot = s ? (s.state === "done" ? "ok" : s.state === "failed" ? "bad" : "") : "";
    let v = s ? (s.state === "done" ? (val || "done") : esc(s.state)) : "never";
    let cls = s && s.state === "failed" ? "bad" : "";
    if (live) { dot = "acc pulse"; v = `${esc(jp.message || "working")} ${pct(jp.progress)}`; cls = ""; }
    else if (q) { dot = "acc"; v = `${q.state} <span class="faint">#${q.id}</span>`; cls = ""; }
    return `<div class="stg" title="${s ? esc(fmtDate(s.utc)) + (s.note ? " - " + esc(s.note) : "") : ""}"><div class="n"><span class="sd ${dot}"></span>${label}</div><div class="v ${cls}" id="stg-v-${stage}">${v}</div>${live ? `<div class="pg"><i id="stg-pg-${stage}" style="width:${pct(jp.progress)}"></i></div>` : ""}</div>`;
  };
  const evTxt = Object.entries(ev).map(([k, n]) => `${n} ${k}`).join(", ");
  const pipeline = stg("probe", "Probe", r.width ? `${r.width}\u00d7${r.height} ${r.fps ? Math.round(r.fps) + " fps" : ""}` : "") +
    stg("transcribe", "Transcribe", ts.n ? `${ts.n} lines \u00b7 ${esc(ts.model || "")}` : esc((st.transcribe && st.transcribe.note) || "no speech")) +
    stg("detect", "Detect", evTxt ? esc(evTxt) : "nothing found") +
    stg("proxy", "Proxy", r.proxy_path ? "built" : "done");
  const jobs = (r.jobs || []).slice(0, 6).map(j => `<div class="jobline"><span class="badge ${j.state === "done" ? "ok" : j.state === "failed" ? "bad" : j.state === "running" ? "acc" : ""}">${esc(j.state)}</span><span class="stage">${esc(j.stage)}</span><span class="faint">#${j.id}</span><span class="faint t-xs" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(j.error || j.message || "")}">${esc(j.error || j.message || "")}</span></div>`).join("") || `<div class="muted t-s">No jobs for this recording.</div>`;
  const tsum = ts.n ? `${ts.n} segments, ${fmtDur(ts.speech_s)} of speech, ${esc(ts.model || "")} <span class="muted">${fmtAgo(ts.utc)}</span>` :
    (st.transcribe ? `<span class="muted">${esc(st.transcribe.note || "no speech found")}</span>` : `<span class="muted">not transcribed</span>`);
  // the essentials up front; the file / codec facts, the roles explanation and the job history behind disclosures
  el.innerHTML = `<div class="rgrid"><div>
      <div class="pipeline">${pipeline}</div>
      <dl class="kv">
        <dt>${r.kind === "replay" ? "Replay saved" : "Recorded"}</dt><dd>${fmtDate(r.name_utc)}${r.kind === "replay" && start ? ` <span class="muted">\u00b7 buffer starts ${fmtDate(start)}</span>` : ""}</dd>
        <dt>Duration</dt><dd>${fmtDur(r.duration_s)} <span class="muted">\u00b7 ${fmtBytes(r.size)} on ${esc(r.drive)}</span></dd>
        <dt>Status</dt><dd>${recBadges(r)}</dd>
        <dt>Transcript</dt><dd>${tsum}</dd>
        <dt>Detection zones</dt><dd>${r.zone_template_id ? `template #${r.zone_template_id} (pinned)` : esc(r.hud_profile || "built-in")} \u00b7 <button type="button" class="lnk" id="d-zones">edit...</button></dd>
        ${r.kind === "vod" && r.source_url ? `<dt>Source</dt><dd><a href="${esc(r.source_url)}" target="_blank" rel="noopener">${esc(r.channel || r.source_url)}</a></dd>` : ""}
        ${r.error ? `<dt>Error</dt><dd style="color:var(--bad-text)">${esc(r.error)}</dd>` : ""}
      </dl>
      <details class="disc-d" id="rec-det" ${ST.recDetOpen ? "open" : ""}><summary>${icon("chev", "chev")}Details <span class="faint">file, video, keyframes</span></summary>
        <dl class="kv">
          <dt>File</dt><dd class="path">${esc(r.path)}</dd>
          <dt>Video</dt><dd>${r.width ? `${r.width}\u00d7${r.height} \u00b7 ${esc(r.vcodec)} ${esc(r.pix_fmt || "")} \u00b7 ${r.fps ? r.fps.toFixed(2) + " fps" : ""} \u00b7 ${fmtMbps(r.bitrate)}` : "-"}</dd>
          <dt>Keyframe interval</dt><dd>${r.keyint_s ? r.keyint_s + " s" : "-"}</dd>
          <dt>File modified</dt><dd>${fmtDate(r.mtime)}</dd>
        </dl></details></div>
    <div>
      <div class="colh">Audio tracks <span class="infoi" data-tip="Roles follow the OBS track names (Game / Microphone / Discord ...) and are checked when a recording is transcribed (level + voice activity on three 30 s windows; digital silence and duplicate tracks drop out). Your mic and the squad are transcribed separately; clips mix game + mic + squad with the quiet mic lifted. Settings > Audio tracks overrides. Hover a row for its codec.">${icon("info")}</span></div>${tracks}
      <details class="disc-d" id="rec-jobs" ${ST.recJobsOpen ? "open" : ""}><summary>${icon("chev", "chev")}Recent jobs <span class="faint">${(r.jobs || []).length}</span></summary><div class="jobs-in">${jobs}</div></details>
      <div class="colh" style="margin-top:16px">Hand-off</div>
      <div class="muted t-s" style="margin-bottom:8px">Markers, a selects timeline or CSV + EDL for this recording or the whole library.</div>
      <button type="button" class="btn" id="d-send">${icon("send")}Send to Resolve...</button></div></div>`;
  $("d-send").onclick = (e) => { e.stopPropagation(); openHandoff($("d-send")); };
  $("d-zones").onclick = () => window.ZN && ZN.open({ rec_id: r.id });
  $("rec-det").ontoggle = () => { ST.recDetOpen = $("rec-det").open; };
  $("rec-jobs").ontoggle = () => { ST.recJobsOpen = $("rec-jobs").open; };
}

async function detectRec(id) {
  try { const r = await post(`/api/recordings/${id}/detect`); toast(r.already_queued ? "already queued" : "detection queued"); if (ST.tab === "events") renderEvents(); }
  catch (err) { toast(err.message, 5000); }
}
async function transcribeRec(id) {
  try { const r = await post(`/api/recordings/${id}/transcribe`); toast(r.already_queued ? "already queued" : "transcription queued"); if (ST.ctab === "tx") renderTranscript(); }
  catch (err) { toast(err.message, 5000); }
}

/* ---------------- events (Events panel) ---------------- */
// A data refresh: never starts or interrupts playback. Re-detect / re-transcribe replace event
// rows with new ids, so the selected event is re-matched like the server carries verdicts over
// (same kind, closest within 1.5 s); gone -> deselected. `eventId` (an explicit user jump) selects.
async function loadEvents(id, eventId, play, isCurrent) {
  let r;
  try { r = await api(`/api/recordings/${id}/events?include_false=1`); } catch (err) { toast(err.message, 5000); return; }
  if (ST.sel !== id) return;    // a late answer for a recording the user already left
  ST.events = { rec_id: id, list: r.events };
  if (ST.event && ST.event.rec_id === id) {
    const old = ST.event;
    const fresh = ST.events.list.find(x => x.id === old.id) || ST.events.list
      .filter(x => x.kind === old.kind && Math.abs(x.t_s - old.t_s) <= 1.5)
      .sort((a, b) => Math.abs(a.t_s - old.t_s) - Math.abs(b.t_s - old.t_s))[0];
    if (fresh) { ST.event = fresh; if (window.PL) PL.syncEvent(old.id, fresh); }
    else { ST.event = null; if (window.PL) PL.clearEvent(); }
    if (!eventId) { renderStage(); renderDetail(); }
  }
  if (eventId && (!isCurrent || isCurrent())) {     // a newer jump (clip search N / P) already took over
    const e = ST.events.list.find(x => x.id === eventId);
    if (e) selectEvent(e.id, !play, { noPlay: !!(window.PL && PL.isOn(e.id)) });
  }
  renderEvents();
  if (window.PL) PL.renderTimeline();
}

// o.bare: no distance / amount (the caller prints them); o.nosub: no kill subkind (the caller's kind line has it)
const subLabel = (s) => { s = String(s || "").replace(/_/g, " "); return s.charAt(0).toUpperCase() + s.slice(1); };
// voice and the other kinds name their subkind in the title itself: the kind line then leaves it out
const titleHasSub = (e) => !["kill", "death", "squad", "manual"].includes(e.kind);
function kindLine(e) { return esc(KIND_NAME[e.kind] || e.kind) + (e.subkind && !titleHasSub(e) ? " · " + esc(e.subkind) : ""); }
function evTitle(e, o) {
  o = o || {};
  const d = !o.bare && e.source && e.source.distance_m ? ` <small>[${e.source.distance_m} m]</small>` : "";
  const amt = !o.bare && e.amount ? ` <small>+$${Number(e.amount).toLocaleString()}</small>` : "";
  if (e.kind === "kill") return `${esc(e.victim || "kill")}${d}${amt}${e.subkind && !o.nosub ? ` <small>${esc(e.subkind)}</small>` : ""}`;
  if (e.kind === "death") return `killed by ${esc(e.killer || "?")}${d}`;
  if (e.kind === "squad") return `${esc(e.killer || "")} &rarr; ${esc(e.victim || "")}${d}`;
  if (e.kind === "manual") return esc(e.note || "manual mark");
  if (e.kind === "voice") {
    const s = e.source || {};
    if (e.subkind === "keyword") return `"${esc((s.voice || []).join(", "))}" <small>${esc((s.text || "").slice(0, 80))}</small>`;
    return `Excitement +${s.over_floor_db != null ? Number(s.over_floor_db).toFixed(1) : "?"} dB${s.near_kind ? ` <small>near a ${esc(s.near_kind)}</small>` : ""}`;
  }
  return `${esc(e.subkind ? subLabel(e.subkind) : (KIND_NAME[e.kind] || e.kind))}${amt}`;
}
function kindBadge(k) { return `<span class="kb k-${esc(k)}">${esc(KIND_NAME[k] || k)}</span>`; }
const scoreCell = (e) => `<div class="score">${e.status === "kept" ? `<span class="kept" title="kept">${icon("check")}</span>` : ""}${e.score.toFixed(2)}<i><b style="width:${Math.min(100, e.score / 3 * 100).toFixed(1)}%"></b></i></div>`;

function renderEvents() {
  const el = $("tab-events"); const r = ST.detail;
  const head = (n, acts) => `<div class="phd"><h2>Events</h2>${n != null ? `<span class="cnt">${n}</span>` : ""}<span class="sp"></span>${kindsBtn()}${acts || ""}<button type="button" class="ib sm" data-menu="events" data-tip="More: detect all missing, calibrate, zones">${icon("more")}</button></div>`;
  if (!r) { el.innerHTML = head(null) + `<div class="pbody"><div class="empty"><b>No recording selected</b>Pick one in the Library. Detect reads the HUD: reward ticker, kill feed, respawn screen.</div></div>`; return; }
  if (ST.events.rec_id !== r.id) { el.innerHTML = head(null) + `<div class="pbody"><div class="empty">loading...</div></div>`; loadEvents(r.id); return; }
  const jp = ST.jobprog[r.id]; const queued = r.jobs && r.jobs.some(j => j.stage === "detect" && j.state === "queued");
  const all = ST.events.list; const counts = {};
  for (const e of all) if (e.status !== "false") counts[e.kind] = (counts[e.kind] || 0) + 1;
  const list = all.filter(e => ST.chips[e.kind] !== false);
  const detecting = jp && jp.stage === "detect";
  let html = head(all.length, `<button type="button" class="btn sm" id="e-run" ${detecting ? "disabled" : ""} title="${r.stages && r.stages.detect ? "re-detect" : "detect"} this recording">${icon("target")}${r.stages && r.stages.detect ? "Re-detect" : "Detect"}</button>`);
  html += `<div class="psub"><span class="faint t-xs" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:100%" title="${esc(r.name)}">${esc(recLabel(r))}</span></div>`;
  if (detecting) html += `<div class="psub"><span class="badge acc" id="ev-prog">${esc(jp.message || "working")} ${pct(jp.progress)}</span><div class="pg"><i id="ev-progbar" style="width:${pct(jp.progress)}"></i></div></div>`;
  else if (queued) html += `<div class="psub"><span class="badge">detect queued</span><button type="button" class="btn ghost sm" id="e-q">Queue...</button></div>`;
  html += `<div class="pbody" id="evlist">`;
  if (!all.length) html += `<div class="empty"><b>${detecting ? "Detecting..." : r.stages && r.stages.detect ? "Nothing detected" : "Not detected yet"}</b>Detect runs the keyframe pass (about a minute per 10 minutes of footage).</div>`;
  else if (!list.length) html += `<div class="empty"><b>All ${all.length} events are hidden</b>Turn on more kinds in the Kinds filter.<div class="acts"><button type="button" class="btn sm" id="e-showall">Show every kind</button></div></div>`;
  for (const e of list) html += `<div class="ev k-${esc(e.kind)} ${ST.event && e.id === ST.event.id ? "cur" : ""} ${e.status === "false" ? "false" : ""}" data-id="${e.id}" id="ev-${e.id}" title="${esc(e.status)}${e.note ? " - " + esc(e.note) : ""}">
      <span class="kbar"></span><span class="tc">${tc(e.t_s)}</span>
      <div class="ttl"><div>${evTitle(e, { nosub: true })}</div><div><span class="kn">${kindLine(e)}</span>${e.resolve_marker ? " \u00b7 in Resolve" : ""}${e.note ? " \u00b7 " + esc(e.note) : ""}</div></div>
      <div class="rgt">${scoreCell(e)}<span class="conf" title="confidence: share of signals that agreed">${pct(e.confidence)} conf</span></div></div>`;
  html += `</div><div class="pfoot">${KINDS.filter(k => counts[k]).map(k => `${counts[k]} ${KIND_LABEL[k]}`).join(" \u00b7 ") || "no events"}</div>`;
  el.innerHTML = html;
  const run = $("e-run"); if (run) run.onclick = () => detectRec(r.id);
  const q = $("e-q"); if (q) q.onclick = () => openPanel("jobs");
  const sa = $("e-showall"); if (sa) sa.onclick = () => { for (const k of KINDS) ST.chips[k] = true; kindsChanged(); };
  el.querySelectorAll(".ev").forEach(row => row.onclick = () => selectEvent(+row.dataset.id));
}

function selectEvent(id, quiet, opts = {}) {
  const e = ST.events.list.find(x => x.id === id); if (!e) return;
  ST.event = e;
  if (ST.found.target != null && ST.found.target !== id) ST.found.target = null;   // picked elsewhere (Events, timeline)
  document.querySelectorAll(".ev[data-id], .fev[data-id]").forEach(x => x.classList.toggle("cur", +x.dataset.id === id));
  renderStage(); renderDetail();
  if (!opts.noPlay && window.PL) PL.playEvent(e, { autoplay: !quiet });
  if (!quiet) { const row = $("ev-" + id); if (row) row.scrollIntoView({ block: "nearest" }); }
}
// visible events in list order (the kinds filter them): n / p step through these
function visibleEvents() { return (ST.events.rec_id === ST.sel ? ST.events.list : []).filter(e => ST.chips[e.kind] !== false); }
function stepEvent(dir) {
  const list = visibleEvents(); if (!list.length) return;
  const i = ST.event ? list.findIndex(e => e.id === ST.event.id) : -1;
  const n = i < 0 ? (dir > 0 ? 0 : list.length - 1) : Math.max(0, Math.min(list.length - 1, i + dir));
  selectEvent(list[n].id);
}
async function setEventStatus(id, status) {
  try {
    const u = await post(`/api/events/${id}`, { status });
    const label = status === "false" ? "false positive" : status === "new" ? "status reset" : status;
    toast(u.rescored ? `${label} - re-weighted ${u.rescored} look-alike event${u.rescored === 1 ? "" : "s"}` : label, u.rescored ? 3500 : 1600);
    const i = ST.events.list.findIndex(x => x.id === id); if (i >= 0) ST.events.list[i] = Object.assign(ST.events.list[i], u);
    if (ST.event && ST.event.id === id) ST.event = ST.events.list[i];
    const f = ST.found.events.find(x => x.id === id); if (f) { f.status = u.status; f.score = u.score != null ? u.score : f.score; renderClips(); }
    renderEvents(); renderDetail(); if (window.PL) PL.renderTimeline(); loadRecs();
  } catch (err) { toast(err.message, 5000); }
}
function verdictKey(status) {
  if (!ST.event) { toast("no event selected - N picks the next one"); return; }
  setEventStatus(ST.event.id, status);
}

/* ---------------- calibration (replaces the review in the main column) ---------------- */
async function openCalibration(recId) {
  if (window.ZN && ZN.isOpen()) { await ZN.close(); if (ZN.isOpen()) return; }   // kept open = unsaved edits the user kept
  const keepT = ST.view === "calibration" && ST.cal && ST.cal.rec_id === (recId || 0) ? ST.cal.t : null;   // a label / alias refresh keeps the grabbed frame
  let cal;
  try { cal = await api(`/api/calibration?rec_id=${recId || 0}`); } catch (err) { toast(err.message, 5000); return; }
  if (window.ZN && ZN.isOpen()) return;   // the zones view opened while this loaded: it owns the main area now
  ST.cal = cal;
  ST.view = "calibration";
  if (keepT != null) ST.cal.t = keepT;
  if (ST.cal.t == null) ST.cal.t = ST.event && ST.event.rec_id === recId ? Math.max(0, Math.round((ST.event.t_s + 0.3) * 10) / 10) : 60;
  $("main").classList.add("cal-open"); $("calview").hidden = false; $("main").scrollTop = 0;
  if (window.PL) PL.pause();
  closePop(); renderCrumb(); renderCtxExtra();
  renderCalibration();
}
function closeCalibration() {
  ST.view = "normal"; $("main").classList.remove("cal-open"); $("calview").hidden = true; $("calview").innerHTML = "";
  if (window.MX && MX.state && MX.state.man) { $("mixer").hidden = false; MX.redraw(); }
  renderStage(); renderDetail();
}
function renderCalibration() {
  const c = ST.cal; const el = $("calview"); const rid = c.rec_id;
  const pc = (v, base) => (v / base * 100).toFixed(2) + "%";
  const roi = (name, cls) => { const q = c.rois[name]; return `<div class="roi ${cls}" style="left:${pc(q.x, c.width)};top:${pc(q.y, c.height)};width:${pc(q.w, c.width)};height:${pc(q.h, c.height)}"><span>${name}</span></div>`; };
  const tk = c.rois.ticker;
  const frame = rid ? `<img src="/api/recordings/${rid}/frame?t=${c.t}&w=1200" alt="frame">` : `<div class="empty">pick a recording to see a frame</div>`;
  const tickerRows = c.ticker.map((g) => `<tr><td class="mono" title="${esc(g.examples.join(" / "))}">${esc(g.pattern)}</td><td>${g.n}</td><td>${esc([...g.cash.map(x => "$" + x), ...g.xp.map(x => x + "XP")].join(" "))}</td><td><select class="inp sm" data-pat="${esc(g.pattern)}">${c.known_labels.map(l => `<option ${l === g.label ? "selected" : ""}>${l}</option>`).join("")}</select></td></tr>`).join("");
  const nameRows = c.names.map(n => `<tr><td class="mono">${esc(n.name)}</td><td>${n.n}</td><td>${n.as_killer} / ${n.as_victim}</td><td><button type="button" class="btn sm" data-alias="${esc(n.name)}" data-as="player">me</button> <button type="button" class="btn sm" data-alias="${esc(n.name)}" data-as="squad">squad</button></td></tr>`).join("");
  const rules = c.labels.map(l => `<div class="field"><label class="mono">${esc(l.pattern)} &rarr; ${esc(l.label)}</label><button type="button" class="btn ghost sm" data-del="${l.id}">Remove</button></div>`).join("") || `<div class="hint">none yet - the built-in rules cover KILL, KILL ASSIST, SPOTTED TARGET DESTROYED, REVIVED, CONTROL ZONE, SUPPLIES, TIP; label a new line here when it shows up</div>`;
  el.innerHTML = `<div class="vh"><h2>Calibration</h2><span class="muted t-s">${esc(c.profile)} ${c.width}\u00d7${c.height}${rid ? " \u00b7 this recording" : " \u00b7 every recording"}${c.zones_source ? " \u00b7 boxes: " + esc(c.zones_source) : ""}</span><span class="sp"></span>
      <button type="button" class="btn sm" id="cal-back" data-tip="Back to the review" data-k="Esc">${icon("x")}Close</button></div>
    <div class="vbody"><div class="cal"><div>
      <div class="ctl">Frame at <input class="inp" type="number" id="cal-t" step="1" min="0" value="${c.t}"> s <button type="button" class="btn sm" id="cal-grab">Grab</button>${rid ? `<button type="button" class="btn sm" id="cal-detect">${icon("target")}Run detect on this recording</button>` : ""}<span class="muted t-xs">aliases: ${esc(c.aliases.join(", ") || "-")} \u00b7 squad: ${esc(c.squad.join(", ") || "-")}</span></div>
      <div class="calframe" id="cal-frame">${frame}${rid ? roi("ticker", "ticker") + roi("feed", "feed") + `<div class="money" style="left:${pc(tk.x, c.width)};top:${pc(tk.y + c.money_y, c.height)};width:${pc(tk.w, c.width)}"></div>` : ""}</div>
      <div class="hint">Ticker box = reward ticker (money readout above the dashed line, ticker lines below); feed box = kill feed. The boxes come from the HUD profile or this streamer's zone template (Zones edits them).</div>
    </div><div>
      <div class="card"><h4>Ticker lines seen<span class="sp"></span><span class="faint">${c.ticker.reduce((a, g) => a + g.n, 0)} sightings</span></h4>
        ${c.ticker.length ? `<table class="t"><tr><th>Text (# = number)</th><th>n</th><th>Amounts</th><th>Label</th></tr>${tickerRows}</table>` : `<div class="hint">run Detect first</div>`}</div>
      <div class="card"><h4>Names seen with a distance tag <span class="faint">(rows involving you)</span></h4>
        ${c.names.length ? `<table class="t"><tr><th>Name</th><th>n</th><th>Killer / victim</th><th></th></tr>${nameRows}</table>` : `<div class="hint">none yet</div>`}</div>
      <div class="card"><h4>Your ticker rules</h4>${rules}</div>
      <div class="card learn" id="cal-learn"><h4>Learning from your verdicts<span class="sp"></span><button type="button" class="btn sm" id="cal-rescore">Rescore all</button></h4><div class="hint">loading...</div></div>
    </div></div></div>`;
  hydrateIcons(el);
  $("cal-back").onclick = closeCalibration;
  $("cal-grab").onclick = () => { c.t = parseFloat($("cal-t").value) || 0; renderCalibration(); };
  $("cal-t").onkeydown = (e) => { if (e.key === "Enter") $("cal-grab").click(); };
  if ($("cal-detect")) $("cal-detect").onclick = () => detectRec(rid);
  el.querySelectorAll("select[data-pat]").forEach(s => s.onchange = async () => {
    try { const r = await post("/api/calibration/labels", { text: s.dataset.pat, label: s.value }); toast(`rule saved, ${r.relabelled} lines relabelled - re-detect to rebuild events`); openCalibration(rid); }
    catch (err) { toast(err.message, 5000); }
  });
  el.querySelectorAll("button[data-alias]").forEach(b => b.onclick = async () => {
    try { const r = await post("/api/calibration/alias", { name: b.dataset.alias, as: b.dataset.as }); ST.settings.player.aliases = r.aliases; ST.settings.player.squad = r.squad; toast("saved - re-detect to apply"); openCalibration(rid); }
    catch (err) { toast(err.message, 5000); }
  });
  el.querySelectorAll("button[data-del]").forEach(b => b.onclick = async () => {
    try { await del(`/api/calibration/labels/${b.dataset.del}`); openCalibration(rid); } catch (err) { toast(err.message, 5000); }
  });
  $("cal-rescore").onclick = () => post("/api/learning/rescore").then(x => { toast(`rescored ${x.rescored} of ${x.examined} new events`); renderLearning(rid); }).catch(err => toast(err.message, 5000));
  renderLearning(rid);
}
// learning: per-signature kept / false counts and the factor they give look-alike events
async function renderLearning(rid) {
  const box = $("cal-learn"); if (!box) return;
  let L; try { L = await api("/api/learning"); } catch (err) { const h = box.querySelector(".hint"); if (h) h.textContent = err.message; return; }
  if (!$("cal-learn")) return;
  const rows = (L.signatures || []).map(s => `<tr title="${esc(s.sig)}"><td>${esc(s.kind)}${s.subkind ? " / " + esc(s.subkind) : ""}</td><td class="mono" style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(s.sig)}</td><td>${s.kept}</td><td>${s.false}</td><td style="color:${s.active ? (s.factor < 1 ? "var(--bad)" : "var(--ok)") : "var(--muted)"}">x${Number(s.factor).toFixed(2)}${s.active ? "" : " (n<" + (L.params ? L.params.min_n : 3) + ")"}</td><td>${s.n_new_affected}</td></tr>`).join("");
  const sug = (L.suggestions || []).map((s, i) => `<div class="field"><label class="mono" title="${esc((s.examples || []).join(" / "))}">${esc(s.message || s.pattern)}</label><button type="button" class="btn sm" data-sug="${i}">Label as other</button></div>`).join("");
  box.innerHTML = `<h4>Learning from your verdicts<span class="sp"></span><button type="button" class="btn sm" id="cal-rescore">Rescore all</button></h4>` +
    (rows ? `<table class="t"><tr><th>Kind</th><th>Signals</th><th>Kept</th><th>False</th><th>Factor</th><th>New affected</th></tr>${rows}</table>` : `<div class="hint">no verdicts yet - Keep (Y) / False positive (X) on events teaches the scores (3 verdicts per signature before it counts)</div>`) +
    (sug ? `<div class="colh" style="margin-top:12px">Ticker noise you keep rejecting</div>${sug}` : "");
  $("cal-rescore").onclick = () => post("/api/learning/rescore").then(x => { toast(`rescored ${x.rescored} of ${x.examined} new events`); renderLearning(rid); }).catch(err => toast(err.message, 5000));
  box.querySelectorAll("button[data-sug]").forEach(b => b.onclick = async () => {
    const s = L.suggestions[+b.dataset.sug];
    try { const r = await post("/api/calibration/labels", { text: s.pattern, label: s.suggest_label || "other" }); toast(`rule saved, ${r.relabelled} lines relabelled - re-detect to rebuild events`); openCalibration(rid); } catch (err) { toast(err.message, 5000); }
  });
}

/* ---------------- right-click menu on a recording (#ctx) ---------------- */
function closeCtx() { $("ctx").hidden = true; }
function openCtx(ev, id) {
  const r = ST.recs.find(x => x.id === id); if (!r) return;
  closePop();
  const m = $("ctx");
  const items = [{ cap: r.kind === "vod" && r.title ? r.title : r.name }, ...recMenuItems(id)];
  m.innerHTML = items.map((it, i) => it === "-" ? `<div class="sep"></div>` : it.cap ? `<div class="cap" title="${esc(it.cap)}">${esc(it.cap)}</div>` : `<div class="it" data-i="${i}">${icon(it.ic || "chev")}<span>${esc(it.label)}</span></div>`).join("");
  m.querySelectorAll(".it").forEach(el => el.onclick = () => { closeCtx(); Promise.resolve(items[+el.dataset.i].act()).catch(err => toast(err.message, 5000)); });
  m.hidden = false;
  const w = m.offsetWidth, h = m.offsetHeight;
  m.style.left = Math.min(ev.clientX, innerWidth - w - 8) + "px"; m.style.top = Math.min(ev.clientY, innerHeight - h - 8) + "px";
}

/* ---------------- the one search field + transcript search (Said panel) ---------------- */
function wireSearch() {
  const s = $("search"), g = $("gsearch"); let qT = null;
  s.oninput = () => {
    g.classList.toggle("has", !!s.value.trim()); renderGsMenu();
    clearTimeout(qT); qT = setTimeout(() => {
      ST.q = s.value.trim(); loadRecs(); runSearch(ST.q);
      if (ST.q && !(ST.panelOpen && (ST.tab === "rec" || ST.tab === "said"))) openPanel("rec");
    }, 250);
  };
  s.onkeydown = (e) => {
    if (e.key === "Enter") { e.preventDefault(); clearTimeout(qT); ST.q = s.value.trim(); loadRecs(); runSearch(ST.q).then(() => openPanel("said")); }
    else if (e.key === "Escape") { e.stopPropagation(); if (s.value) clearSearch(); else s.blur(); }
  };
  $("search-clr").onclick = () => { clearSearch(); s.focus(); };
}
function focusSearch() { const s = $("search"); s.focus(); s.select(); }
function clearSearch() {
  const s = $("search"); s.value = ""; $("gsearch").classList.remove("has");
  ST.q = ""; ST.search = { q: "", hits: [], fts: ST.search.fts, pending: false };
  loadRecs(); renderSaid();
}
function renderGsMenu() {
  const v = $("search").value.trim(); const m = $("gsmenu");
  if (!v) { m.innerHTML = ""; return; }
  const n = ST.q === v ? ST.recs.length : null;
  m.innerHTML = `<button type="button" class="mi" id="gs-recs">${icon("rec")}<span>${n == null ? "Filtering recordings" : `${n} recording${n === 1 ? "" : "s"}`} named \u201c${esc(v)}\u201d<small>the Library list follows the text</small></span></button>
    <button type="button" class="mi act" id="gs-said">${icon("tx")}<span>Search what was said: \u201c${esc(v)}\u201d<small>every transcript, ranked</small></span><span class="k">Enter</span></button>`;
  $("gs-recs").onmousedown = (e) => { e.preventDefault(); openPanel("rec"); };
  $("gs-said").onmousedown = (e) => { e.preventDefault(); ST.q = v; runSearch(v).then(() => openPanel("said")); };
}
async function loadTranscript(id, segId) {
  let r;
  try { r = await api(`/api/recordings/${id}/transcript`); }
  catch (err) { ST.transcript.failed = id; toast(err.message, 5000); return false; }
  if (ST.sel !== id) return false;    // a late answer for a recording the user already left
  ST.transcript = { rec_id: id, segments: r.segments, summary: r.summary, filter: ST.transcript.filter, cur: segId || null, failed: null };
  updateTxCount();
  if (ST.ctab === "tx") renderTranscript(segId);
  return true;
}
async function runSearch(q) {
  ST.search.q = q;
  if (!q || q.length < 2) { ST.search.hits = []; renderSaid(); return; }
  ST.search.pending = true; renderSaid();
  try { const r = await api(`/api/search?q=${encodeURIComponent(q)}&limit=200${ST.drive ? "&drive=" + encodeURIComponent(ST.drive) : ""}`); if (r.q !== ST.search.q) return; ST.search.hits = r.hits; ST.search.fts = r.fts; }
  catch (err) { toast(err.message, 5000); }
  ST.search.pending = false;
  setStatus(`search "${q}": ${ST.search.hits.length} hits`);
  renderSaid();
}
function markText(text, q) {
  // [brackets] come from the FTS snippet; otherwise highlight the raw query tokens
  if (text.includes("[") && text.includes("]")) return esc(text).replace(/\[([^\]]+)\]/g, "<mark>$1</mark>");
  let out = esc(text);
  for (const t of (q || "").split(/\s+/).filter(x => x.length >= 2)) { out = out.replace(new RegExp("(" + t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ")", "ig"), "<mark>$1</mark>"); }
  return out;
}
function renderSaid() {
  const el = $("tab-said"); const S = ST.search;
  let html = `<div class="phd"><h2>What was said</h2><span class="sp"></span><button type="button" class="ib sm" data-menu="library" data-tip="Library actions">${icon("more")}</button></div>`;
  if (S.q && S.q.length >= 2) {
    const hits = S.hits; const recs = new Set(hits.map(h => h.rec_id));
    html += `<div class="psub t-s muted"><span>\u201c<b style="color:var(--text)">${esc(S.q)}</b>\u201d \u00b7 ${S.pending && !hits.length ? "searching..." : `${hits.length} hits in ${recs.size} recording${recs.size === 1 ? "" : "s"}`}</span>
      ${S.fts ? `<span class="badge" title="full-text index (FTS5): ranked by relevance (bm25), the last word matches as a prefix">ranked</span>` : `<span class="badge warn" title="this SQLite has no FTS5: plain LIKE matching, every word must appear, not ranked">LIKE \u00b7 unranked</span>`}
      <span class="sp"></span><button type="button" class="btn ghost sm" id="t-clear">Clear</button></div><div class="pbody">`;
    if (!hits.length) html += `<div class="empty">${S.pending ? `<div class="spin"></div>` : `<b>No line says that</b>Only transcribed recordings are searched; try fewer words (the last one matches as a prefix).`}</div>`;
    for (const h of hits) html += `<div class="hit" data-rec="${h.rec_id}" data-seg="${h.seg_id}"><div class="where"><span class="tc">${tc(h.t0)}</span><b title="${esc(h.rec_name)}">${esc(recLabelFor(h.rec_id, h.rec_name))}</b><span>${esc(h.drive || "")}</span></div><div class="snip">${markText(h.snippet || h.text, S.q)}</div></div>`;
    html += `</div>`;
  } else {
    html += `<div class="pbody"><div class="empty"><b>Search everything that was said</b>Type in the search bar at the top and press Enter: every transcript in the library, ranked by relevance.<div class="acts"><button type="button" class="btn sm" id="t-focus">${icon("search")}Search <span class="kbd">/</span></button></div></div></div>`;
  }
  el.innerHTML = html;
  const c = $("t-clear"); if (c) c.onclick = clearSearch;
  const f = $("t-focus"); if (f) f.onclick = focusSearch;
  el.querySelectorAll(".hit").forEach(h => h.onclick = async () => {
    el.querySelectorAll(".hit").forEach(x => x.classList.toggle("cur", x === h));
    const rid = +h.dataset.rec, sid = +h.dataset.seg;
    showCtab("tx");
    await select(rid, { transcript: true, seg: sid });
    const row = $("rec-" + rid); if (row) row.scrollIntoView({ block: "nearest" });
  });
}

/* ---------------- Transcript tab (the selected recording) ---------------- */
function renderTranscript(scrollSeg) {
  const el = $("tab-transcript"); if (!el) return;
  const r = ST.detail; const t = ST.transcript;
  renderCtxExtra();
  if (!r) { el.innerHTML = `<div class="empty"><b>No recording selected</b>Pick a recording, or type in the search bar to search every transcript.</div>`; return; }
  if (t.rec_id !== r.id) { el.innerHTML = `<div class="empty">loading...</div>`; if (t.failed !== r.id) loadTranscript(r.id); return; }
  if (deferIfEditing(el, () => renderTranscript())) return;
  const jp = ST.jobprog[r.id]; const queued = r.jobs && r.jobs.some(j => j.stage === "transcribe" && j.state === "queued");
  const sum = t.summary || { n: 0 };
  let html = "";
  if (jp && jp.stage === "transcribe") html += `<div class="sline"><span class="sd acc pulse"></span><span id="tx-prog">${esc(jp.message || "working")} ${pct(jp.progress)}</span><div class="pg" style="flex:1"><i id="tx-progbar" style="width:${pct(jp.progress)}"></i></div></div>`;
  else if (queued) html += `<div class="sline"><span class="sd acc"></span><span>Transcribe queued \u00b7 waits for the stt lane${ST.counts && ST.counts.paused ? " (lanes are paused)" : ""}</span><span class="sp"></span><button type="button" class="btn ghost sm" id="tx-q">Queue...</button></div>`;
  html += `<div class="txhead">` + (sum.n ? `<span>${sum.n} lines \u00b7 ${fmtDur(sum.speech_s)} of speech \u00b7 <span title="${esc(fmtDate(sum.utc))}">${esc(sum.model || "")}</span></span>` :
    (r.stages && r.stages.transcribe ? `<span class="badge warn">${esc(r.stages.transcribe.note || "no speech")}</span>` : `<span>not transcribed</span>`));
  if (sum.n) html += `<span class="sp"></span><span class="search-inp">${icon("search")}<input class="inp sm" type="text" id="t-filter" placeholder="Filter these lines" value="${esc(t.filter)}"></span>`;
  html += `</div><div id="tx-lines"></div>`;
  el.innerHTML = html;
  const q = $("tx-q"); if (q) q.onclick = () => openPanel("jobs");
  renderTxLines();
  const fi = $("t-filter"); if (fi) { let ft = null; fi.oninput = () => { clearTimeout(ft); ft = setTimeout(() => { ST.transcript.filter = fi.value; renderTxLines(); }, 200); }; }
  if (scrollSeg) { const s = $("seg-" + scrollSeg); if (s) { s.scrollIntoView({ block: "center" }); s.classList.add("flash"); } }
}
function renderTxLines() {
  const box = $("tx-lines"); const r = ST.detail; const t = ST.transcript; if (!box || !r) return;
  const jp = ST.jobprog[r.id];
  const f = (t.filter || "").toLowerCase();
  const lines = t.segments.filter(s => !f || s.text.toLowerCase().includes(f));
  if (!t.segments.length) { box.innerHTML = `<div class="empty"><b>${jp && jp.stage === "transcribe" ? "Transcribing..." : "No transcript yet"}</b>Transcribe runs faster-whisper on the voice tracks (${esc(ST.settings ? ST.settings.stt.model : "")}): your mic and the squad separately.</div>`; return; }
  if (!lines.length) { box.innerHTML = `<div class="empty">No line matches the filter.</div>`; return; }
  const near = ST.event && ST.event.rec_id === r.id ? ST.event.t_s : null;
  box.innerHTML = `<div class="txcols">` + lines.map(s => {
    const who = s.speaker === "me" ? "you" : s.speaker === "squad" ? "squad" : "";
    return `<div class="tline ${s.id === t.cur ? "cur" : ""} ${near != null && Math.abs(s.t0 - near) <= 5 ? "near" : ""} ${s.speaker === "me" ? "me" : ""}" data-seg="${s.id}" data-t="${s.t0}" id="seg-${s.id}" title="${esc(fmtDate((recStart(r) || 0) + s.t0))} - click: play from here | alt+click: copy timecode"><span class="tc ${s.avg_logprob != null && s.avg_logprob < -0.8 ? "lo" : ""}" title="confidence ${s.avg_logprob}">${tc(s.t0)}</span><span>${who ? `<span class="who">${who}</span>` : ""}${f ? markText(s.text, f) : esc(s.text)}</span></div>`;
  }).join("") + `</div>`;
  box.querySelectorAll(".tline").forEach(l => l.onclick = (ev) => {
    ST.transcript.cur = +l.dataset.seg; box.querySelectorAll(".tline").forEach(x => x.classList.toggle("cur", x === l));
    if (ev.altKey || !window.PL) copyText(tc(+l.dataset.t)); else PL.playAt(r.id, Math.max(0, +l.dataset.t - 1));
  });
}

/* ---------------- clip search (Find panel) ---------------- */
// field id -> filter key; the kinds come from the kinds filter, the drive from the Library's drive select
const FINDER_FIELDS = { "f-status": "status", "f-score": "min_score", "f-player": "player", "f-dist": "min_dist", "f-streak": "streak",
  "f-reckind": "rec_kind", "f-said": "said", "f-saidw": "said_window", "f-from": "from", "f-to": "to", "f-order": "order" };
const FINDER_DEFAULTS = { status: "notfalse", min_score: "", player: "", min_dist: "", streak: "", rec_kind: "any", said: "", said_window: "", from: "", to: "", order: "score" };
const FINDER_LS = "wclips.finder";
function finderLoad() { try { return Object.assign({}, FINDER_DEFAULTS, JSON.parse(localStorage.getItem(FINDER_LS) || "{}")); } catch (e) { return Object.assign({}, FINDER_DEFAULTS); } }
function finderSave() { try { localStorage.setItem(FINDER_LS, JSON.stringify(ST.finder)); } catch (e) { } }
let finderT = null;
const findShown = () => ST.tab === "clips" && ST.panelOpen;
// a filter changed: results that are on screen follow it (debounced); otherwise Find / Enter runs it
function finderChanged() {
  finderMarks();
  ST.found.lastIdx = null;                        // a different search: "where N continues" starts over
  // off screen: the next visit searches again; the count beside Find no longer describes the filters
  if (!findShown()) { ST.found.ran = false; $("f-count").textContent = ""; return; }
  clearTimeout(finderT); finderT = setTimeout(runFind, 350);
}
// fields that differ from the default are highlighted, so a narrowed search is visible at a glance
function finderMarks() {
  let n = 0;
  for (const [id, k] of Object.entries(FINDER_FIELDS)) {
    const el = $(id); const set = String(ST.finder[k] || "") !== String(FINDER_DEFAULTS[k] || "");
    if (set && k !== "order") n++;
    if (el && el.closest("label")) el.closest("label").classList.toggle("set", set);
  }
  $("f-nset").outerHTML = n ? `<span id="f-nset" class="badge acc">${n} set</span>` : `<span id="f-nset" class="faint">defaults</span>`;
  $("f-reset").hidden = !n;
  $("f-drive").textContent = ST.drive ? ST.drive : "all drives";
}
function wireFinder() {
  ST.finder = finderLoad();
  for (const [id, k] of Object.entries(FINDER_FIELDS)) {
    const el = $(id); el.value = ST.finder[k] || "";
    const upd = () => { ST.finder[k] = el.value.trim(); finderSave(); finderChanged(); };
    el.addEventListener(el.tagName === "SELECT" || el.type === "date" ? "change" : "input", upd);
    el.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); upd(); clearTimeout(finderT); openPanel("clips"); runFind(); } });
  }
  $("f-find").onclick = () => { ST.found.lastIdx = null; openPanel("clips"); runFind(); };
  $("f-reset").onclick = (e) => {
    e.stopPropagation();
    ST.finder = Object.assign({}, FINDER_DEFAULTS); finderSave();
    for (const [id, k] of Object.entries(FINDER_FIELDS)) $(id).value = ST.finder[k] || "";
    finderChanged(); toast("filters back to the defaults");
  };
  finderMarks();
  paintKindsBtns();
  wireFinderCollapse();
}
// the Filters disclosure: only the clip search form folds; state in localStorage wclips.finderCollapsed
function wireFinderCollapse() {
  const fd = $("finder"), d = $("f-disc");
  const apply = (c) => { fd.classList.toggle("collapsed", c); d.setAttribute("aria-expanded", String(!c)); };
  let c = false;
  try { c = localStorage.getItem("wclips.finderCollapsed") === "1"; localStorage.removeItem("wclips.finderH"); } catch (e) { }
  apply(c);
  const toggle = () => { c = !c; apply(c); try { localStorage.setItem("wclips.finderCollapsed", c ? "1" : "0"); } catch (e) { } };
  d.onclick = (e) => { if (e.target.closest("#f-reset")) return; toggle(); };
  d.onkeydown = (e) => { if ((e.key === "Enter" || e.key === " ") && e.target === d) { e.preventDefault(); toggle(); } };
}
function finderQuery() {
  const f = ST.finder; const p = new URLSearchParams();
  p.set("kinds", Object.keys(ST.chips).filter(k => ST.chips[k]).join(","));
  for (const k of ["status", "min_score", "player", "min_dist", "streak", "rec_kind", "said", "said_window", "order"]) if (f[k]) p.set(k, f[k]);
  // the date fields are local days: from = that midnight, to = the end of that day
  const day = (v, plus) => { const d = new Date(v + "T00:00:00"); if (isNaN(d)) return null; d.setDate(d.getDate() + plus); return Math.floor(d.getTime() / 1000); };
  if (f.from) { const x = day(f.from, 0); if (x != null) p.set("from", x); }
  if (f.to) { const x = day(f.to, 1); if (x != null) p.set("to", x); }
  if (ST.drive) p.set("drive", ST.drive);
  p.set("limit", "300");
  return p;
}
async function runFind() {
  clearTimeout(finderT);
  const tok = ++ST.found.token; ST.found.pending = true; ST.found.ran = true; ST.found.error = null;
  if (!Object.values(ST.chips).some(Boolean)) { ST.found = Object.assign(ST.found, { events: [], total: 0, pending: false }); renderClips(); return; }
  $("f-count").textContent = "searching...";
  if (ST.tab === "clips" && !ST.found.events.length) renderClips();
  try {
    const r = await api("/api/find-clips?" + finderQuery().toString());
    if (tok !== ST.found.token) return;
    Object.assign(ST.found, { events: r.events, total: r.total, limit: r.limit, filter: r.filter });
  } catch (err) { if (tok !== ST.found.token) return; ST.found.error = err.message; ST.found.events = []; ST.found.total = 0; }
  ST.found.pending = false;
  renderClips();
}
function renderClips() {
  const F = ST.found;
  $("f-count").textContent = F.error ? "error" : F.pending ? "searching..." : F.ran ? `${F.total} clip${F.total === 1 ? "" : "s"}${F.total > F.events.length ? ` (top ${F.events.length})` : ""}` : "Enter in any field also searches";
  if (ST.tab !== "clips") return;
  const el = $("tab-clips");
  if (F.error) { el.innerHTML = `<div class="empty" style="color:var(--bad-text)">${esc(F.error)}</div>`; return; }
  if (!Object.values(ST.chips).some(Boolean)) { el.innerHTML = `<div class="empty"><b>Every kind is off</b>Turn on a kind in the Kinds filter.</div>`; return; }
  if (!F.ran || (F.pending && !F.events.length)) { el.innerHTML = `<div class="empty"><div class="spin"></div></div>`; return; }
  if (!F.events.length) { el.innerHTML = `<div class="empty"><b>No clips match</b>Loosen a filter (marked ones differ from the default), or Reset.</div>`; return; }
  const said = (F.filter && F.filter.said) || [];
  let html = "";
  for (const e of F.events) {
    const cur = ST.event && ST.event.id === e.id;
    html += `<div class="fev k-${esc(e.kind)} ${cur ? "cur" : ""} ${e.status === "false" ? "false" : ""}" data-id="${e.id}" data-rec="${e.rec_id}" id="fev-${e.id}" title="${esc(e.status)}${e.note ? " - " + esc(e.note) : ""}">
      <span class="kbar"></span>
      <div class="ttl"><div>${evTitle(e)}</div>
        <div><span class="kn">${esc(KIND_NAME[e.kind] || e.kind)}</span> \u00b7 <span class="tc">${tc(e.t_s)}</span> \u00b7 ${e.abs_utc ? esc(fmtWhen(e.abs_utc)) : "-"}</div>
        <div class="rn" title="${esc(e.rec_name)}">${esc(nameStem(e.rec_name))} <span class="faint">\u00b7 ${esc(e.drive || "")}</span></div>
        ${e.said_text ? `<div class="said">\u201c${markTokens(e.said_text, said)}\u201d</div>` : ""}</div>
      ${scoreCell(e)}</div>`;
  }
  el.innerHTML = html;
  el.querySelectorAll(".fev").forEach(row => row.onclick = () => openFound(+row.dataset.id));
}
// highlight search words in RAW text: one regex over the text, every piece escaped (marking escaped
// HTML token by token could match inside an earlier <mark> or an &amp; entity)
function markTokens(text, tokens) {
  const toks = (tokens || []).filter(t => t && t.length >= 2).map(t => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  if (!toks.length) return esc(text);
  return String(text).split(new RegExp("(" + toks.join("|") + ")", "ig")).map((p, i) => i % 2 ? `<mark>${esc(p)}</mark>` : esc(p)).join("");
}
async function openFound(id) {
  const e = ST.found.events.find(x => x.id === id); if (!e) return;
  // the last result asked for wins: N / P step from it at once, and an older, slower open can't select over it
  const tok = ++ST.found.openTok; ST.found.target = id;
  ST.found.lastIdx = ST.found.events.findIndex(x => x.id === id);
  document.querySelectorAll(".fev").forEach(x => x.classList.toggle("cur", +x.dataset.id === id));
  await select(e.rec_id, { event: e.id, play: true, isCurrent: () => tok === ST.found.openTok });
  const row = $("fev-" + id); if (row && tok === ST.found.openTok) row.scrollIntoView({ block: "nearest" });
}
function stepFound(dir) {
  const list = ST.found.events; if (!list.length) return;
  const curId = ST.found.target != null ? ST.found.target : (ST.event && ST.event.id);
  const i = curId != null ? list.findIndex(e => e.id === curId) : -1;
  let n;
  if (i >= 0) n = i + dir;
  // the one just triaged dropped out of the results (a verdict re-runs the search): continue from its old
  // place instead of jumping back to the top
  else if (ST.found.lastIdx != null) n = dir > 0 ? ST.found.lastIdx : ST.found.lastIdx - 1;
  else n = dir > 0 ? 0 : list.length - 1;
  openFound(list[Math.max(0, Math.min(list.length - 1, n))].id);
}

/* ---------------- Import a stream VOD (media/vods.py) ---------------- */
const VOD_HOSTS = [["YouTube", /(^|\.)(youtube\.com|youtu\.be|youtube-nocookie\.com)$/], ["Twitch", /(^|\.)twitch\.tv$/],
  ["Kick", /(^|\.)kick\.com$/], ["TikTok", /(^|\.)tiktok\.com$/], ["Rumble", /(^|\.)rumble\.com$/]];
function vodProvider(u) {
  try { const h = new URL(/^https?:\/\//i.test(u) ? u : "https://" + u).hostname.toLowerCase(); const m = VOD_HOSTS.find(([, re]) => re.test(h)); return m ? m[0] : null; }
  catch (e) { return null; }
}
let vodMeta = null, vodTok = 0;
function openVod() {
  const d = $("dlg-vod"); d.returnValue = "";
  if (!d.open) d.showModal();
  $("vod-url").focus(); $("vod-url").select();
}
async function vodLook() {
  const url = $("vod-url").value.trim(); const info = $("vod-info"); const go = $("vod-go");
  const tok = ++vodTok;                      // any answer still on its way is for an older text: dropped
  vodMeta = null; go.disabled = true; $("vod-note").textContent = "";
  if (!url) return;
  const prov = vodProvider(url);
  if (!prov) { info.innerHTML = `<div class="hint" style="color:var(--bad-text)">Not a supported site -- YouTube, Twitch, Kick, TikTok and Rumble links work.</div>`; return; }
  // a streamer's page (profile / videos tab) is not one VOD: the VOD browser on that streamer. Parsed locally on the
  // server (inspect:false = no network), so a profile link opens at once instead of after the site refuses it
  if (window.VB && await vodOpenChannel(url, tok)) return;
  if (tok !== vodTok) return;
  info.innerHTML = `<div class="hint">${provBadge(prov)} asking ${prov} about the link...</div>`;
  try {
    const m = await post("/api/vods/inspect", { url });
    if (tok !== vodTok || $("vod-url").value.trim() !== url) return;
    vodMeta = m; vodMeta._asked = url;
    const sz = m.size_bytes ? fmtBytes(m.size_bytes) : "size unknown";
    info.innerHTML = `<div class="vodcard"><span class="t">${esc(m.title || m.id)}</span>
      <b>Site</b><span>${provBadge(m.provider)} ${m.was_live ? "past live stream" : "video"}</span>
      <b>Channel</b><span>${esc(m.channel || "-")}</span>
      <b>Started</b><span>${m.start_utc ? fmtDate(m.start_utc) : "-"}</span>
      <b>Length</b><span>${m.duration_s ? fmtDur(m.duration_s) : "-"}</span>
      <b>Quality</b><span>${m.height ? m.height + "p" : "?"}${m.fps ? Math.round(m.fps) : ""} ${esc(m.vcodec || "")}, ${sz}</span>
      <b>Saved to</b><span class="mono t-xs">${esc(m.dest)}</span></div>`;
    if (window.VB && m.channel_key) vodChannelRow(info, m);
    if (m.already) { $("vod-note").innerHTML = `already imported: ${esc(m.already.name)}${m.already.rec_id ? ` <a href="#" id="vod-open">open it</a>` : ""}`; const o = $("vod-open"); if (o) o.onclick = (e) => { e.preventDefault(); $("dlg-vod").close(); openPanel("rec"); select(m.already.rec_id); }; }
    else go.disabled = false;
  } catch (err) {
    if (tok !== vodTok) return;
    info.innerHTML = `<div class="hint" style="color:var(--bad-text)">${esc(err.message)}</div>`;
  }
}
// a profile / channel link -> close this dialog and open the VOD browser on that streamer; true when it did
async function vodOpenChannel(url, tok) {
  const r = await post("/api/vodbrowse/resolve", { text: url, inspect: false }).catch(() => null);
  const d = $("dlg-vod");
  // the dialog was closed (vodTok bumped) or the text changed meanwhile: the user moved on
  if (tok !== vodTok || !d.open || $("vod-url").value.trim() !== url) return false;
  if (!(r && r.kind === "channel" && r.key)) return false;
  d.close();
  if (r.provider) VB.open({ provider: r.provider, key: r.key, name: r.name, url: r.url || url });
  else VB.resolveAndOpen(url);
  return true;
}
// under a looked-up VOD: "More from <channel>" (the VOD browser on that streamer) + a favorite star
async function vodChannelRow(info, m) {
  const row = document.createElement("div"); row.className = "frow vodchan";
  const name = m.channel || m.channel_key;
  row.innerHTML = `<button type="button" class="btn sm" id="vod-more">More from ${esc(name)}...</button>
    <button type="button" class="btn sm" id="vod-star" title="favorite streamer: listed in the VOD browser, checked for new VODs">&#9734; favorite</button>`;
  info.appendChild(row);
  $("vod-more").onclick = () => {
    $("dlg-vod").close();
    VB.open({ provider: m.provider, key: m.channel_key, name: m.channel, url: m.channel_url,
      preselect_url: m.url, preselect_id: m.id, preselect_title: m.title || "", already: m.already || null });
  };
  const star = $("vod-star"); let favId = null;
  const paint = () => { star.innerHTML = favId ? "&#9733; favorite" : "&#9734; favorite"; star.classList.toggle("pri", !!favId); };
  try {
    const favs = await api("/api/vodbrowse/favorites");
    const f = (Array.isArray(favs) ? favs : favs.favorites || []).find(x => x.provider === m.provider && (x.channel_key || x.key) === m.channel_key);
    favId = f ? f.id : null;
  } catch (e) { /* the list is only for the star's state */ }
  paint();
  star.onclick = async () => {
    star.disabled = true;
    try {
      if (favId) { await del(`/api/vodbrowse/favorites/${favId}`); favId = null; toast(`${name} removed from favorites`); }
      else { const f = await post("/api/vodbrowse/favorites", { provider: m.provider, key: m.channel_key, name: m.channel, url: m.channel_url }); favId = (f && (f.id || (f.favorite && f.favorite.id))) || null; toast(`${name} added to favorites`); }
    } catch (err) { toast(err.message, 4000); }
    star.disabled = false; paint();
  };
}
async function vodGo() {
  if (!vodMeta) return;
  if (vodMeta._asked !== $("vod-url").value.trim()) { vodLook(); return; }   // the box changed since Look
  const go = $("vod-go"); go.disabled = true;
  try {
    const meta = Object.assign({}, vodMeta); delete meta._asked;
    const r = await post("/api/vods", { url: meta.url, meta });
    toast(r.already_queued ? "that download is already queued" : `downloading into ${r.dest} -- progress in Jobs`, 5000);
    $("vod-note").textContent = r.already_queued ? "already queued" : "queued -- Jobs shows the download";
    $("vod-url").value = ""; vodMeta = null;
    if (ST.tab === "jobs") loadJobs();
  } catch (err) { $("vod-note").textContent = err.message; go.disabled = false; }
}
function wireVod() {
  // closing the dialog drops any look-up still on its way (its answer must not reopen anything)
  // the close event is queued and can land after the dialog was opened again (a profile link closes it and Import
  // VOD reopens it at once): only a dialog that is still closed counts
  $("dlg-vod").addEventListener("close", () => { if (!$("dlg-vod").open) ++vodTok; });
  $("vod-look").onclick = vodLook;
  $("vod-go").onclick = vodGo;
  const u = $("vod-url");
  u.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); vodMeta ? vodGo() : vodLook(); } });
  u.addEventListener("paste", () => setTimeout(vodLook, 0));
  u.addEventListener("input", () => { vodMeta = null; ++vodTok; $("vod-go").disabled = true; const p = vodProvider(u.value.trim()); $("vod-note").textContent = u.value.trim() ? (p ? `${p} link` : "not a supported site") : ""; });
}

/* ---------------- Transcribe all (palette, Library menu, Transcript tab) ---------------- */
const TALL_SCOPES = [
  ["missing", "Not transcribed yet", "recordings with no transcript (or whose last try failed)"],
  ["outdated", "Not transcribed yet + outdated", "also redoes transcripts made by an older version that read only one audio track (no separate mic transcript)"],
  ["all", "Everything", "re-transcribes every recording and replaces the transcripts that are there"],
];
let tallScope = "outdated";
async function openTranscribeAll() {
  const dlg = $("dlg-tall"); const body = $("tall-body"); const go = $("tall-go");
  body.innerHTML = `<div class="hint">counting...</div>`; $("tall-note").textContent = ""; go.disabled = true;
  // a <dialog> keeps returnValue across openings and Escape closes it WITHOUT setting one: clear the
  // last "go" so backing out with Escape can never queue anything
  dlg.returnValue = "";
  if (!dlg.open) dlg.showModal();
  let p;
  try { p = await api("/api/transcribe-all"); } catch (err) { body.innerHTML = `<div class="hint" style="color:var(--bad-text)">${esc(err.message)}</div>`; return; }
  const est = (s) => s.est_s != null ? `about ${fmtDur(s.est_s)}` : "";
  body.innerHTML = TALL_SCOPES.map(([k, label, tip]) => {
    const s = p.scopes[k];
    return `<label class="opt${s.to_queue ? "" : " none"}" title="${esc(tip)}"><input type="radio" name="tall" value="${k}" ${s.to_queue ? "" : "disabled"}>
      <span><b>${esc(label)}</b><small>${esc(tip)}</small></span>
      <span class="num">${s.n} rec \u00b7 ${s.hours} h${s.already_queued ? `<br><small>${s.already_queued} already queued</small>` : ""}${est(s) ? `<br><small>${est(s)}</small>` : ""}</span></label>`;
  }).join("") + `<div class="hint">Whisper ${esc(p.model)} on the GPU, one recording at a time beside detect, newest first.
    ${p.rate ? "" : "The time estimate appears once a recording has been transcribed with this version."}
    Jobs: drag to reorder, Pause to hold, Clear jobs to drop the queue.</div>`;
  const pick = (k) => { const r = body.querySelector(`input[value="${k}"]:not(:disabled)`); if (r) { r.checked = true; tallScope = k; return true; } return false; };
  if (!pick(tallScope)) ["outdated", "missing", "all"].some(pick);
  const sync = () => {
    const sel = body.querySelector("input[name=tall]:checked"); const s = sel && p.scopes[sel.value];
    go.disabled = !s || !s.to_queue || !p.stt_enabled;
    go.textContent = s && s.to_queue ? `Queue ${s.to_queue}` : "Queue";
    $("tall-note").textContent = !p.stt_enabled ? "speech-to-text is off (Settings > Speech-to-text)" : "";
  };
  body.querySelectorAll("input[name=tall]").forEach(r => r.onchange = () => { tallScope = r.value; sync(); });
  sync();
}
$("dlg-tall").addEventListener("close", () => {
  const dlg = $("dlg-tall"); const go = dlg.returnValue === "go"; dlg.returnValue = "";
  if (!go || $("tall-go").disabled) return;
  const scope = tallScope;
  if (scope === "all" && !confirm("Re-transcribe EVERY recording? The transcripts that are there are replaced as each one finishes.")) return;
  post("/api/transcribe-all", { scope }).then(x => {
    toast(`transcribe: queued ${x.queued} recordings${x.candidates > x.queued ? ` (${x.candidates - x.queued} were already queued)` : ""}`, 4000);
    if (ST.tab === "jobs") loadJobs();
  }).catch(err => toast(err.message, 5000));
});

/* ---------------- jobs (Jobs panel) ---------------- */
async function loadJobs() {
  try {
    const r = await api("/api/jobs?limit=300");
    ST.jobs = r.jobs; ST.counts = r.counts;
  } catch (err) { toast(err.message, 5000); return; }
  renderPill(); renderJobs();
}
const jobsRetryFailed = () => post("/api/jobs/retry-failed").then(r => { toast(`retried ${r.retried}`); loadJobs(); }).catch(err => toast(err.message, 5000));
const jobsClearFinished = () => post("/api/jobs/clear").then(r => { toast(`removed ${r.removed}`); loadJobs(); }).catch(err => toast(err.message, 5000));
function jobsClearAll() {
  const c = ST.counts || {};
  const n = (c.queued || 0) + (c.done || 0) + (c.failed || 0) + (c.skipped || 0);
  if (!n) { toast("nothing to clear"); return; }
  if (!confirm(`Clear ${n} jobs (${c.queued || 0} queued, ${(c.done || 0) + (c.failed || 0) + (c.skipped || 0)} finished)?\n${c.running ? c.running + " running job(s) keep going.\n" : ""}Queued work is dropped; Scan / Detect / Transcribe queue it again.`)) return;
  post("/api/jobs/clear", { all: true }).then(r => { toast(`removed ${r.removed} jobs`); loadJobs(); }).catch(err => toast(err.message, 5000));
}

// drive-aware parallelism (Settings > Jobs): what each library drive can feed at once (measured), and what runs there now
async function loadDrives() {
  const box = $("dr-drives"); if (!box || $("drawer").hidden) return;
  try { ST.drives = await api("/api/drives"); } catch (err) { box.innerHTML = `<div class="hint">drives: ${esc(err.message)}</div>`; return; }
  box.innerHTML = renderDrives() || `<div class="hint">no library drives</div>`;
  wireDrives(box);
}
function renderDrives() {
  const d = ST.drives; if (!d) return "";
  const cap = d.capacity || {};
  let html = `<div class="subhead">Drives<span class="spacer"></span><span title="video-lane jobs at once across all drives (Settings > Jobs)">GPU ${cap.gpu_running || 0}/${cap.gpu_parallel || "-"}</span><span title="Settings > Jobs > per-drive parallel">mode ${esc(String(cap.mode || "auto"))}</span><button type="button" class="btn sm" id="dr-bench-all" title="read-only throughput test of every library drive (~20 s, ~1.5 GB read each; waits until the drive is idle)">Benchmark all</button></div>`;
  for (const v of d.drives || []) {
    const b = v.bench; const mb = b && b.mb_s ? ["1", "2", "4"].map(n => b.mb_s[n] != null ? `${n}x ${Math.round(b.mb_s[n])}` : null).filter(Boolean).join(" / ") + " MB/s" : "not benchmarked";
    const sl = v.slots || {};
    const running = (v.running || []).map(j => `${esc(j.stage)} #${j.rec_id || "-"}`).join(", ");
    html += `<div class="drv"><div class="top"><b>${esc(v.drive)}</b><span class="badge ${v.kind === "ssd" ? "ok" : v.kind === "hdd" ? "warn" : ""}">${esc(v.kind || "?")}</span>
        <span class="mono t-xs">${esc(mb)}</span>${b && !b.cache_bypass ? `<span class="badge warn" title="${esc(b.note || "")}">cache not bypassed</span>` : ""}
        ${v.bench_state ? `<span class="badge acc">benchmark ${esc(v.bench_state)}</span>` : ""}<span class="spacer"></span><button type="button" class="btn ghost sm" data-bench="${esc(v.drive)}">Benchmark</button></div>
      <div class="meta"><span title="footage jobs this drive can feed at once">slots: detect ${sl.detect ?? "-"} | proxy ${sl.proxy ?? "-"} | transcribe ${sl.transcribe ?? "-"}</span><span>${v.files} files ${fmtBytes(v.bytes)}</span><span>${v.queued || 0} queued</span>${running ? `<span style="color:var(--accent-2)">running: ${running}</span>` : ""}${b ? `<span title="${esc(fmtDate(b.utc))}">measured ${fmtAgo(b.utc)}</span>` : ""}</div></div>`;
  }
  return html;
}
function wireDrives(el) {
  const go = (body) => post("/api/drives/benchmark", body).then(r => toast(`benchmark started: ${r.started.join(", ")} (waits until the drive is idle)`, 4000)).catch(err => toast(err.message, 5000)).finally(loadDrives);
  const all = el.querySelector("#dr-bench-all"); if (all) all.onclick = () => go({});
  el.querySelectorAll("button[data-bench]").forEach(b => b.onclick = () => go({ drive: b.dataset.bench }));
}

function renderJobs() {
  // a re-render mid-drag would drop the row being dragged: the live feed's refresh waits for the drop
  if (ST.jobDrag) { ST.jobDragStale = true; return; }
  const el = $("tab-jobs"); const c = ST.counts || {};
  const nq = ST.jobs.filter(j => j.state === "queued").length;
  let html = `<div class="phd"><h2>Jobs</h2><span class="sp"></span><button type="button" class="btn sm ${c.paused ? "pri" : ""}" id="j-pause">${icon(c.paused ? "play" : "pausej")}${c.paused ? "Resume" : "Pause"}</button><button type="button" class="ib sm" data-menu="jobs" data-tip="Retry failed, clear finished, clear the queue">${icon("more")}</button></div>
    <div class="stat4"><div><b>${c.running || 0}</b><span>running</span></div><div><b>${c.queued || 0}</b><span>queued</span></div><div><b class="${c.failed ? "bad" : ""}">${c.failed || 0}</b><span>failed</span></div><div><b>${(c.done || 0) + (c.skipped || 0)}</b><span>finished</span></div></div>`;
  if (c.paused) html += `<div class="psub"><span class="badge warn">lanes paused</span><span class="t-xs muted">running jobs finish, nothing new starts until you resume</span></div>`;
  html += `<div class="pbody" id="jlist">`;
  if (!ST.jobs.length) html += `<div class="empty"><b>No jobs</b>A scan queues a probe for every new recording.</div>`;
  const GROUPS = [["running", "Running"], ["queued", nq > 1 ? "Queued \u00b7 drag to reorder (top runs first)" : "Queued"], ["failed", "Failed"], ["done", "Finished"]];
  const groupOf = (s) => s === "skipped" ? "done" : s;
  for (const [g, label] of GROUPS) {
    const js = ST.jobs.filter(j => groupOf(j.state) === g); if (!js.length) continue;
    html += `<div class="sect-t">${label}</div>`;
    for (const j of js) {
      const cls = j.state === "done" ? "ok" : j.state === "failed" ? "bad" : j.state === "running" ? "acc" : j.state === "skipped" ? "warn" : "";
      const q = j.state === "queued";
      let what = j.rec_name || "";
      if (j.stage === "download" && j.arg) { try { const a = JSON.parse(j.arg); what = `${a.provider || ""}: ${a.title || a.url || ""}`; } catch (e) { } }
      const act = j.state === "failed" || j.state === "skipped" ? `<button type="button" class="btn ghost sm" data-act="retry" data-id="${j.id}">Retry</button>`
        : q ? `<button type="button" class="btn ghost sm" data-act="skip" data-id="${j.id}">Skip</button>`
        : j.state === "running" && (j.stage === "download" || j.stage === "proxy") ? `<button type="button" class="btn ghost sm" data-act="stop" data-id="${j.id}" title="stop it now (a download's partial files are removed)">Stop</button>` : "";
      html += `<div class="jrow${q ? " jq" : ""}" data-rec="${j.rec_id || ""}" data-job="${j.id}" id="job-${j.id}"${q ? ` draggable="true"` : ""}>
        <span class="grip" title="drag to reorder">${icon("grip")}</span>
        <div style="min-width:0"><div class="jl1"><b>${esc(j.stage)}</b><span class="faint num t-xs">#${j.id}</span><span class="badge ${cls}">${esc(j.state)}</span><span class="age">${j.started_utc ? fmtAgo(j.started_utc) : fmtAgo(j.created_utc)}</span></div>
          ${what ? `<div class="what" title="${esc(what)}">${esc(j.stage === "download" ? what : recLabelFor(j.rec_id, what))}</div>` : ""}
          <div class="msg ${j.state === "failed" ? "err" : ""}" id="jmsg-${j.id}" title="${esc(j.error || j.message || "")}">${esc(j.error || j.message || "")}</div>
          <div class="bar" id="jbar-${j.id}" ${j.state === "running" ? "" : "hidden"}><i style="width:${pct(j.progress)}"></i></div></div>
        <div>${act}</div></div>`;
    }
  }
  html += `</div>`;
  el.innerHTML = html;
  $("j-pause").onclick = togglePause;
  wireJobDrag(el);
  // a 409 means the row was stale (the job already started or finished): reload instead of leaving the button there
  el.querySelectorAll("button[data-act]").forEach(b => b.onclick = (e) => { e.stopPropagation(); b.disabled = true; post(`/api/jobs/${b.dataset.id}/${b.dataset.act}`).catch(err => toast(err.message + " - list refreshed", 4000)).finally(loadJobs); });
  el.querySelectorAll(".jrow[data-rec]").forEach(row => row.onclick = () => { if (row.dataset.rec) { showTab("rec"); select(+row.dataset.rec); } });
}
// left-click drag a queued job to reorder the queue: the drop sends the whole visible queued order
function wireJobDrag(el) {
  const rows = () => [...el.querySelectorAll(".jrow.jq")];
  const clearMarks = () => el.querySelectorAll(".jrow.jq").forEach(r => r.classList.remove("drop-above", "drop-below"));
  let dragEl = null;
  const end = () => {
    clearMarks(); if (dragEl) dragEl.classList.remove("dragging"); dragEl = null;
    ST.jobDrag = false;
    if (ST.jobDragStale) { ST.jobDragStale = false; loadJobs(); }
  };
  for (const row of rows()) {
    row.addEventListener("dragstart", (e) => {
      dragEl = row; ST.jobDrag = true; row.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", row.dataset.job);
    });
    row.addEventListener("dragover", (e) => {
      if (!dragEl || row === dragEl) return;
      e.preventDefault(); e.dataTransfer.dropEffect = "move";
      const r = row.getBoundingClientRect(); const below = e.clientY > r.top + r.height / 2;
      clearMarks(); row.classList.add(below ? "drop-below" : "drop-above");
    });
    row.addEventListener("drop", (e) => {
      e.preventDefault();
      if (!dragEl || row === dragEl) { end(); return; }
      const below = row.classList.contains("drop-below");
      row.parentNode.insertBefore(dragEl, below ? row.nextSibling : row);
      const ids = rows().map(r => +r.dataset.job);
      const moved = dragEl.dataset.job;
      end();
      post("/api/jobs/reorder", { ids }).then(r => { if (r.moved) toast(`job #${moved} moved - queue reordered`); })
        .catch(err => toast(err.message, 5000)).finally(loadJobs);
    });
    row.addEventListener("dragend", end);
  }
}

/* ---------------- live updates ---------------- */
function connectSSE() {
  if (ST.sse) ST.sse.close();
  const es = new EventSource("/api/stream"); ST.sse = es;
  let recT = null, jobT = null;
  const bumpRecs = () => { clearTimeout(recT); recT = setTimeout(async () => { await refreshStatus(); await loadRecs(); if (ST.sel) select(ST.sel); }, 400); };
  const bumpJobs = () => { clearTimeout(jobT); jobT = setTimeout(async () => { loadDrives(); /* only while Settings is open */ if (ST.tab === "jobs" && ST.panelOpen) await loadJobs(); else { try { ST.counts = await api("/api/jobs/counts"); renderPill(); } catch (e) { } } }, 400); };
  es.addEventListener("library", () => bumpRecs());
  es.addEventListener("recording", (e) => {
    const d = JSON.parse(e.data); const i = ST.recs.findIndex(r => r.id === d.rec.id);
    if (i >= 0) ST.recs[i] = Object.assign(ST.recs[i], d.rec);
    renderRecs(); if (ST.sel === d.rec.id) select(d.rec.id); refreshStatus();
  });
  es.addEventListener("transcript", (e) => {
    const d = JSON.parse(e.data);
    delete ST.jobprog[d.rec_id];
    if (ST.sel === d.rec_id) { ST.transcript.rec_id = null; if (ST.ctab === "tx") loadTranscript(d.rec_id); }
    bumpRecs();
    if (ST.search.q) runSearch(ST.search.q);
  });
  es.addEventListener("events", (e) => {
    const d = JSON.parse(e.data);
    if (d.action === "detected") delete ST.jobprog[d.rec_id];
    // clip search results on screen follow new / re-scored / re-labelled events (a short debounce:
    // a library-wide detect publishes one of these per recording)
    if (findShown() && ST.found.ran) { clearTimeout(finderT); finderT = setTimeout(runFind, 1500); }
    else if (ST.found.ran) { ST.found.ran = false; $("f-count").textContent = ""; }
    // refresh the data only: the selected event is re-matched inside loadEvents, playback is left alone
    if (d.action === "rescored") { if (ST.sel) loadEvents(ST.sel); return; }
    if (ST.sel === d.rec_id) loadEvents(d.rec_id);
    bumpRecs();
  });
  es.addEventListener("job", (e) => {
    const d = JSON.parse(e.data); const j = d.job || {};
    if (d.action === "progress") {
      if (j.rec_id) ST.jobprog[j.rec_id] = { stage: j.stage, progress: j.progress, message: j.message };
      const bar = $("bar-" + j.rec_id); if (bar) { bar.hidden = false; bar.firstElementChild.style.width = pct(j.progress); }
      const jb = $("jbar-" + j.id); if (jb) { jb.hidden = false; jb.firstElementChild.style.width = pct(j.progress); }
      const jm = $("jmsg-" + j.id); if (jm) jm.textContent = j.message || "";
      if (ST.sel === j.rec_id) {
        const label = `${j.message || "working"} ${pct(j.progress)}`;
        if (j.stage === "transcribe" && ST.ctab === "tx") { const b = $("tx-progbar"); if (b) { b.style.width = pct(j.progress); $("tx-prog").textContent = label; } else renderTranscript(); }
        if (j.stage === "detect" && ST.tab === "events") { const b = $("ev-progbar"); if (b) { b.style.width = pct(j.progress); $("ev-prog").textContent = label; } else renderEvents(); }
        const sv = $("stg-v-" + j.stage), sp = $("stg-pg-" + j.stage);
        if (sv && sp) { sv.textContent = label; sp.style.width = pct(j.progress); } else if (sv && ST.ctab === "rec") renderRecPane();
      }
      return;
    }
    if (d.action === "done" || d.action === "failed") {
      if (j.rec_id) delete ST.jobprog[j.rec_id];
      const bar = $("bar-" + j.rec_id); if (bar) bar.hidden = true;
      // a download has no recording (rec_id null): never "the selected one", but always worth a word
      if (j.stage === "download") toast(d.action === "done" ? `VOD downloaded: ${j.message || ""}` : `VOD download failed: ${j.error || ""}`, 8000);
      else if (d.action === "failed" && j.rec_id && j.rec_id === ST.sel) toast(`${j.stage} failed: ${j.error || ""}`, 7000);
      if (j.rec_id && j.rec_id === ST.sel) select(j.rec_id);
    }
    if (d.action === "started" && j.rec_id) {
      ST.jobprog[j.rec_id] = { stage: j.stage, progress: 0, message: "starting" };
      if (j.rec_id === ST.sel) { if (ST.ctab === "tx") renderTranscript(); if (ST.tab === "events") renderEvents(); if (ST.ctab === "rec") renderRecPane(); }
    }
    bumpJobs();
  });
  es.addEventListener("jobs", () => bumpJobs());
  // the Resolve link: starting / waiting for a project / a waiting hand-off ran
  let rsT = null;
  es.addEventListener("resolve", (e) => {
    const d = JSON.parse(e.data); const op = d.op || {};
    if (!ST.resolve) ST.resolve = {};
    if (d.link) { ST.resolve.link = d.link; renderResolvePill(); }
    clearTimeout(rsT); rsT = setTimeout(() => refreshResolve(false), 300);      // connected / project / timeline
    if (d.action === "done" && op.result) {
      const msg = handoffMsg(op.kind, op.result); toast(msg, 6000);
      const res = $("ho-res"); if (res) res.textContent = msg;
    } else if (d.action === "failed") {
      toast(`${op.label || "hand-off"} failed: ${op.error || ""}`, 8000);
      const res = $("ho-res"); if (res) res.textContent = op.error || "failed";
    } else if (d.action === "state" && d.link && d.link.state === "no_project") {
      toast("Resolve is up -- open a project (" + (d.link.error || "Project Manager") + ")", 5000);
    }
  });
  es.addEventListener("drives", (e) => {
    const d = JSON.parse(e.data);
    if (d.action === "bench_done" && d.bench) toast(`[${d.bench.drive}] ${Math.round((d.bench.mb_s || {})["1"] || 0)} MB/s, ${d.bench.kind || "?"} - slots updated`, 4000);
    if (d.action === "bench_failed") toast(`drive benchmark failed: ${d.error || ""}`, 6000);
    loadDrives();                                   // only when the settings modal is open
  });
  // add-on downloads (PackStatus per change, <= 4 a second per pack) and the setup flag: setup.js owns both
  es.addEventListener("packs", (e) => { if (window.SU) SU.onPackEvent(JSON.parse(e.data)); });
  es.addEventListener("setup", (e) => { if (window.SU) SU.onSetupEvent(JSON.parse(e.data)); });
  es.onerror = () => { setStatus("live feed lost - retrying"); };
  es.onopen = () => { if ($("status").textContent.startsWith("live feed")) setStatus(""); };
}

/* ---------------- settings (modal #drawer; every field autosaves) ---------------- */
// [group, section, field, label, type, extra]   type: num(step) | text | list | lines | check | select(choices key or array)
const FIELDS = [
  ["Library|where your footage lives; files are read in place and never written to", "library", "roots", "roots (one per line; read in place, never written)", "lines"],
  ["", "library", "recursive", "include subfolders", "check"],
  ["", "library", "patterns", "file patterns (; separated)", "text"],
  ["", "library", "min_age_s", "skip files modified within (s) - still recording", "num", 30],
  ["", "library", "watch_s", "re-scan for new files every (s)", "num", 10],
  ["", "library", "auto_scan_new", "watch the roots while running", "check"],
  ["Player|your names in the kill feed decide kill vs death vs squad", "player", "aliases", "your in-game name(s) (; separated, no clan tag)", "list"],
  ["", "player", "squad", "squad mates (; separated)", "list"],
  ["Audio tracks|read from your OBS profile at startup; overrides win over the measurement", "audio", "track_names", "OBS track names 1-6 (; separated; read from OBS while empty)", "list"],
  ["", "audio", "mic_track", "your mic track index (0-based, -1 = auto by name)", "num", 1],
  ["", "audio", "voip_track", "squad / Discord track index (0-based, -1 = auto)", "num", 1],
  ["", "audio", "game_track", "game audio track index (0-based, -1 = auto)", "num", 1],
  ["", "audio", "normalize_mic", "lift the quiet mic (whisper + clips)", "check"],
  ["Speech-to-text", "stt", "enabled", "transcribe the voice track", "check"],
  ["", "stt", "model", "whisper model (switching unloads the current one)", "select", "stt_models"],
  ["", "stt", "compute", "compute type", "select", "stt_compute"],
  ["", "stt", "vad", "voice activity detection first", "check"],
  ["", "stt", "word_timestamps", "word timestamps", "check"],
  ["", "stt", "batched", "batched decoding (speech chunks decoded in parallel; faster on long files)", "check"],
  ["", "stt", "batch_size", "batch size (chunks per batch, 1-32)", "num", 1],
  ["", "stt", "hotwords", "extra vocabulary (; separated; player + squad names are always added)", "text"],
  ["Jobs & drives", "jobs", "auto_transcribe", "transcribe every newly probed recording", "check"],
  ["", "jobs", "auto_detect", "detect events in every newly probed recording", "check"],
  ["", "jobs", "parallel", "footage jobs per drive (auto = from the drive benchmark)", "select", ["auto", "1", "2", "3", "4", "5", "6", "7", "8"]],
  ["", "jobs", "gpu_parallel", "video jobs at once on the GPU (all drives) - threads after a restart", "num", 1],
  ["", "jobs", "max_parallel", "ceiling per drive", "num", 1],
  ["", "jobs", "auto_bench", "benchmark each drive once in the background (when idle)", "check"],
  ["", "jobs", "io_lanes", "io lanes (probe) - restart to apply", "num", 1],
  ["", "jobs", "video_lanes", "minimum video workers (restart)", "num", 1],
  ["", "jobs", "stt_lanes", "speech-to-text lanes (one whisper model each)", "num", 1],
  ["Detection", "detection", "roi_fps", "HUD crops per second (full prep pass)", "num", 1],
  ["", "detection", "refine_window_s", "refine window +/- (s)", "num", 0.5],
  ["", "detection", "merge_s", "merge signals within (s)", "num", 0.5],
  ["", "detection", "ocr_min_conf", "OCR min confidence", "num", 0.05],
  ["", "detection", "ocr_fast_ticker", "fast OCR for the reward ticker: ~2.7x faster detect OCR, but it missed about 1 in 10 kill / assist lines on 3440x1440 footage", "check"],
  ["", "detection", "ticker_reuse", "reuse the last ticker reading while the ticker has not changed", "check"],
  ["", "detection", "ocr_device", "HUD text reading on: auto = the GPU while nothing else (a game) is using it, else the CPU; gpu; cpu", "select", "ocr_devices"],
  ["", "detection", "ticker_every", "read the ticker on every Nth keyframe -- keep 1: 2 halved the reading time but lost about 1 event in 3 in tests (fewer ticker reads = fewer kill-feed reads)", "num", "1"],
  ["Highlights", "highlights", "keywords", "voice keywords: become voice events + raise nearby events (; separated, next transcribe)", "text"],
  ["", "highlights", "rms_spike_db", "voice excitement: level over the rolling floor (dB, next transcribe)", "num", 1],
  ["", "highlights", "multikill_s", "multikill window (s)", "num", 1],
  ["", "highlights", "pre_roll_s", "clip pre-roll (s)", "num", 1],
  ["", "highlights", "post_roll_s", "clip post-roll (s)", "num", 1],
  ["Encoding|clips + proxies; OBS records on the GPU's one NVENC engine", "proxies", "enabled", "build browser proxies", "check"],
  ["", "proxies", "dir", "proxy folder (empty = data dir\\proxies)", "text"],
  ["", "proxies", "height", "proxy height (px)", "num", 8],
  ["", "proxies", "bitrate_k", "proxy bitrate (kb/s)", "num", 250],
  ["", "proxies", "codec", "proxy encoder (libx264 = CPU, keeps NVENC free for OBS; auto = NVENC only while OBS is not using it)", "select", "proxy_codecs"],
  ["", "proxies", "clip_codec", "clip encoder for the player (same choices)", "select", "proxy_codecs"],
  ["VODs|stream VOD import: YouTube, Twitch, Kick, TikTok, Rumble", "vods", "enabled", "allow Import VOD", "check"],
  ["", "vods", "dir", "folder for the provider folders (empty = Videos\\WardogClips VODs in your user folder)", "text"],
  ["", "vods", "max_height", "best quality up to (px height)", "num", 120],
  ["", "vods", "parallel", "downloads at once (restart to apply)", "num", 1],
  ["", "vods", "add_to_library", "add the VOD folder to the library roots on the first import", "check"],
  ["Resolve", "resolve", "enabled", "talk to DaVinci Resolve", "check"],
  ["", "resolve", "launch_on_start", "start Resolve with WardogClips (if it is not running) and wait for a project", "check"],
  ["", "resolve", "launch_on_send", "a hand-off while Resolve is closed starts it, waits for a project, then runs", "check"],
  ["", "resolve", "project", "open this project automatically (empty = wait for you to open one)", "text"],
  ["", "resolve", "wait_min", "give up waiting after (minutes)", "num", 1],
  ["", "resolve", "exe", "Resolve.exe (empty = C:\\Program Files\\Blackmagic Design\\DaVinci Resolve\\Resolve.exe)", "text"],
  ["", "resolve", "bin", "media pool bin", "text"],
  ["", "resolve", "selects_name", "selects timeline name ({date})", "text"],
  ["", "resolve", "color_kill", "marker colour: kill", "select", "marker_colors"],
  ["", "resolve", "color_assist", "marker colour: assist", "select", "marker_colors"],
  ["", "resolve", "color_death", "marker colour: death", "select", "marker_colors"],
  ["", "resolve", "color_spotted", "marker colour: spotted", "select", "marker_colors"],
  ["", "resolve", "color_voice", "marker colour: voice", "select", "marker_colors"],
  ["", "resolve", "color_manual", "marker colour: manual", "select", "marker_colors"],
  ["Server", "server", "open_browser", "open the browser on launch", "check"],
  ["", "server", "verbose_console", "DEBUG lines on the console", "check"],
  // the release check (GET /api/updates); "Check now" + the result line are added after these fields
  ["Updates|a newer WardogClips release on GitHub (checked at most once a day; nothing is downloaded)", "updates", "check", "check for a newer version", "check"],
  ["", "updates", "repo", "GitHub repository (owner/name; empty = never check)", "text"],
];
const GROUP_ICONS = { "Library": "folder", "Player": "target", "Audio tracks": "wave", "Speech-to-text": "mic", "Jobs & drives": "jobs", "Detection": "events", "Highlights": "star", "Encoding": "bolt", "VODs": "vods", "Resolve": "resolve", "Server": "settings", "Updates": "refresh", "Add-ons": "download", "Clip cache": "trash", "System": "info" };

async function openDrawer(group) {
  const s = ST.settings;
  let stt = null; try { stt = await api("/api/stt/status"); } catch (e) { }
  let html = "", gi = 0; const groups = [];
  const grp = (name, sub) => { groups.push(name); return `<div class="grp" id="set-g-${gi++}" data-g="${esc(name)}">${esc(name)}${sub ? `<small>${esc(sub)}</small>` : ""}</div>`; };
  for (const [g, sec, f, label, type, extra] of FIELDS) {
    if (g) { const [name, m] = g.split("|"); html += grp(name, m); }
    if (sec === "stt" && f === "enabled" && stt) html += `<div class="sline"><span class="sd ${stt.available ? (stt.last_error ? "warn" : "ok") : "bad"}"></span><span>engine: ${stt.available ? "faster-whisper ready" : "faster-whisper NOT installed (reinstall WardogClips)"}${stt.loaded ? ` \u00b7 loaded ${esc(stt.loaded)} on ${esc(stt.device)}` : ""} \u00b7 cached models: ${esc((stt.cached_models || []).join(", ") || "none")}${stt.last_error ? `<br><span style="color:var(--warn-text)">${esc(stt.last_error)}</span>` : ""}</span></div>`;
    if (sec === "jobs" && f === "parallel") html += `<div class="drvbox" id="dr-drives"><div class="hint" style="padding:8px 12px">loading drives...</div></div>`;
    if (sec === "proxies" && f === "enabled") html += `<div class="sline" id="enc-now">encoder: checking...</div>`;
    if (sec === "detection" && f === "roi_fps") html += `<div class="field"><label>Calibration page: HUD boxes, ticker wording, your name</label><button type="button" class="btn sm" id="dr-cal">${icon("calib")}Open calibration</button></div>`;
    const v = (s[sec] || {})[f]; let input;
    if (type === "num") input = `<input class="inp" type="number" step="${extra}" data-sec="${sec}" data-f="${f}" value="${v}">`;
    else if (type === "check") input = `<input class="toggle" type="checkbox" data-sec="${sec}" data-f="${f}" ${v ? "checked" : ""}>`;
    else if (type === "list") input = `<input class="inp" type="text" data-sec="${sec}" data-f="${f}" value="${esc((v || []).join("; "))}">`;
    else if (type === "lines") input = `<textarea class="inp" rows="${Math.max(3, (v || []).length + 1)}" data-sec="${sec}" data-f="${f}">${esc((v || []).join("\n"))}</textarea>`;
    else if (type === "select") { const ch = Array.isArray(extra) ? extra : ((s.choices || {})[extra] || []); input = `<select class="inp" data-sec="${sec}" data-f="${f}">${ch.map(o => `<option ${String(o) === String(v) ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`; }
    else input = `<input class="inp" type="text" data-sec="${sec}" data-f="${f}" value="${esc(v)}">`;
    html += `<div class="field"><label>${esc(label.charAt(0).toUpperCase() + label.slice(1))}</label>${input}</div>`;
    if (sec === "updates" && f === "repo") html += `<div class="field"><label>Look for a newer release now</label><button type="button" class="btn sm" id="up-check">${icon("refresh")}Check now</button></div><div class="sline" id="up-res"></div>`;
  }
  // add-ons (setup.js fills #ao-gpu / #ao-list / #ao-models and wires the buttons: SU.mountSettings)
  html += grp("Add-ons", "Optional downloads: faster speech on NVIDIA graphics cards, the YouTube helper, speech models and updates for the VOD downloader. Nothing downloads unless you click.") +
    `<div class="sline" id="ao-gpu"></div><div class="ao-list" id="ao-list"><div class="hint">loading add-ons...</div></div>` +
    `<div class="ao-sub">Speech models</div><div class="ao-list" id="ao-models"></div>` +
    `<div class="ao-acts"><button type="button" class="btn sm" id="ao-ytdlp" title="Downloads a newer VOD downloader (yt-dlp) when there is one: try this when stream downloads stop working">${icon("refresh")}Update VOD downloader</button><button type="button" class="btn sm" id="ao-setup" title="The first-run questions again: footage folders, your name, speech, stream recordings">${icon("settings")}Run setup again</button></div>`;
  const st = ST.status || {};
  html += grp("Clip cache", "player clips + per-track window audio, rebuilt on demand; proxies stay") + `<div class="field"><label id="cc-usage">-</label><button type="button" class="btn sm" id="cc-clear" title="delete every cached clip and per-track window audio (proxies stay)">${icon("trash")}Clear clip cache</button></div>`;
  html += grp("System") + `<div class="hint">data: <span class="mono t-xs">${esc(st.data_dir)}</span><br>log: <span class="mono t-xs">${esc(st.log_file)}</span> (console ${esc(st.console_level)})<br><a href="/api/system/logs?tail=300" target="_blank">open the log tail</a> \u00b7 <a href="/api/system/hw" target="_blank">ffmpeg hardware</a> \u00b7 <a href="/api/stt/status" target="_blank">speech-to-text engine</a></div>`;
  $("dbody").innerHTML = html;
  $("setnav").innerHTML = groups.map((g, i) => `<button type="button" data-i="${i}">${icon(GROUP_ICONS[g] || "chev")}${esc(g)}</button>`).join("");
  $("setnav").querySelectorAll("button").forEach(b => b.onclick = () => { const t = $("set-g-" + b.dataset.i); if (t) t.scrollIntoView({ block: "start" }); });
  const navSync = () => {
    const top = $("dbody").getBoundingClientRect().top + 40; let cur = 0;
    $("dbody").querySelectorAll(".grp").forEach((g, i) => { if (g.getBoundingClientRect().top <= top) cur = i; });
    $("setnav").querySelectorAll("button").forEach((b, i) => b.classList.toggle("on", i === cur));
  };
  $("dbody").onscroll = navSync;
  $("dr-cal").onclick = () => { closeDrawer(); openCalibration(ST.sel || 0); };
  $("dbody").querySelectorAll("[data-sec]").forEach(el => el.onchange = async () => {
    const raw = el.type === "checkbox" ? el.checked : el.value;
    const val = el.type === "number" ? parseFloat(raw) : raw;
    const row = el.closest(".field");
    try {
      const r = await post("/api/settings", { [el.dataset.sec]: { [el.dataset.f]: val } });
      ST.settings = r.settings; $("dmsg").className = "msg"; $("dmsg").textContent = "saved: " + (r.changed.join(", ") || "no change");
      if (row) { row.classList.add("saved"); setTimeout(() => row.classList.remove("saved"), 1400); }
      if (el.dataset.sec === "proxies") refreshEncoderLine();
      if (el.dataset.sec === "library") renderLegend();
      if (r.changed.some(c => c.startsWith("library."))) toast("library settings changed - Scan new applies them");
    } catch (err) { $("dmsg").className = "msg bad"; $("dmsg").textContent = "error: " + err.message; }
  });
  $("drawer").hidden = false; $("scrim").hidden = false;
  closePop(); closeCtx();
  loadDrives();
  refreshEncoderLine();
  const usage = (c) => { const u = $("cc-usage"); if (u) u.textContent = `${c.items} cached items, ${fmtBytes(c.bytes)} of ${fmtBytes(c.cap_bytes)} (oldest go first past that)`; };
  api("/api/clips-status").then(r => usage(r.cache)).catch(() => { });
  $("cc-clear").onclick = () => {
    const b = $("cc-clear"); b.disabled = true;
    post("/api/clips-cache/clear").then(r => {
      usage(r.cache);
      toast(`clip cache cleared: ${r.removed} items, ${fmtBytes(r.freed_bytes)} freed${r.left ? ` (${r.left} in use, kept)` : ""}`, 4000);
    }).catch(err => toast(err.message, 5000)).finally(() => { b.disabled = false; });
  };
  if (window.SU) SU.mountSettings();
  if (group) { const i = groups.findIndex(g => g.toLowerCase().startsWith(String(group).toLowerCase())); if (i >= 0) { const t = $("set-g-" + i); if (t) $("dbody").scrollTop = t.offsetTop - 8; } }
  navSync();
}
function openSettings(group) { return openDrawer(group); }
// Settings > Encoding: what clips / proxies actually encode with right now (re-read after a codec change)
function refreshEncoderLine() {
  refreshEncoder().then(e => {
    const el = $("enc-now"); if (!el) return;
    if (!e) { el.textContent = ""; return; }
    const lab = (c) => c === "libx264" ? "x264 (CPU)" : c === "h264_nvenc" ? "NVENC H.264" : c === "hevc_nvenc" ? "NVENC HEVC" : c;
    el.innerHTML = `<span class="sd ok"></span><span>right now clips encode with <b>${esc(lab(e.clip_effective))}</b>, proxies with <b>${esc(lab(e.proxy_effective))}</b>.` +
      ` "auto" would pick <b>${esc(lab(e.auto_now))}</b> - ${esc(e.why)}` +
      (e.nvenc_sessions != null ? ` <span class="muted">(${e.nvenc_sessions} NVENC session${e.nvenc_sessions === 1 ? "" : "s"} open on the GPU)</span>` : "") + `</span>`;
  });
}
function closeDrawer() { $("drawer").hidden = true; $("scrim").hidden = true; }

/* ---------------- command palette (Ctrl+K) ---------------- */
function paletteCommands() {
  const r = ST.detail, e = ST.event && r && ST.event.rec_id === r.id ? ST.event : null, c = ST.counts || {};
  const st = (r && r.stages) || {};
  const C = [];
  const add = (g, ic, label, act, k, kw) => C.push({ g, ic, label, act, k, kw });
  add("Panels", "rec", "Show recordings", () => openPanel("rec"));
  add("Panels", "events", "Show events", () => openPanel("events"));
  add("Panels", "clips", "Find clips", () => openPanel("clips"), "", "search filter player said streak");
  add("Panels", "tx", "What was said (transcript search)", () => { openPanel("said"); focusSearch(); });
  add("Panels", "jobs", "Show jobs", () => openPanel("jobs"), "", "queue");
  add("Panels", "vods", "Browse stream VODs", () => openVods(), "", "twitch youtube kick favorites");
  add("Panels", "zones", "Edit detection zones...", () => window.ZN && ZN.open(r ? { rec_id: r.id } : {}), "", "templates boxes");
  add("Panels", "panel", ST.panelOpen ? "Fold the side panel" : "Unfold the side panel", () => ST.panelOpen ? closePanel() : openPanel(ST.tab), "Ctrl B");
  if (e) {
    add("This event", "check", "Keep", () => setEventStatus(e.id, "kept"), "Y", "verdict");
    add("This event", "x", "False positive", () => setEventStatus(e.id, "false"), "X", "verdict");
    if (e.status !== "new") add("This event", "reset", "Reset status to new", () => setEventStatus(e.id, "new"), "U");
    add("This event", "resolve", "Marker for this event in Resolve", () => handoff("markers", { event_ids: [e.id], rec_ids: undefined, status: "any", min_score: undefined, kinds: undefined }));
    add("This event", "copy", "Copy timecode", () => copyText(tc(e.t_s)));
  }
  add("Player", "replay", "Replay from in", () => $("pl-replay").click(), "R");
  add("Player", "loop", "Loop in \u2192 out on / off", () => $("pl-loop").click(), "L");
  add("Player", "mark", "Mark here", () => $("pl-markhere").click(), "M", "manual event");
  add("Player", "wider", "Wider clip window", () => $("pl-wider").click());
  add("Player", "reset", "Reset in / out", () => $("pl-reset").click());
  if (r) {
    add("This recording", "target", st.detect ? "Re-detect this recording" : "Detect this recording", () => detectRec(r.id));
    add("This recording", "mic", (r.transcript && r.transcript.n) || st.transcribe ? "Re-transcribe this recording" : "Transcribe this recording", () => transcribeRec(r.id));
    add("This recording", "tx", "Show transcript", () => showCtab("tx"), "3");
    add("This recording", "bolt", "Build proxy for this recording", () => post(`/api/recordings/${r.id}/proxy`).then(() => toast("proxy queued")).catch(err => toast(err.message, 5000)));
    add("This recording", "calib", "Calibrate detection...", () => openCalibration(r.id), "", "hud ticker rules learning");
    add("This recording", "send", "Send to Resolve...", () => openHandoff($("btn-send")), "", "markers selects export csv edl");
    add("This recording", "folder", "Reveal in Explorer", () => post(`/api/recordings/${r.id}/reveal`).catch(err => toast(err.message, 5000)));
    add("This recording", "copy", "Copy path", () => copyText(r.path));
    add("This recording", "probe", "Re-probe", () => post(`/api/recordings/${r.id}/probe`).then(() => toast("probe queued")).catch(err => toast(err.message, 5000)));
  } else add("Library", "calib", "Calibrate detection...", () => openCalibration(0));
  add("Library", "scan", "Scan new", scan, "", "library roots files");
  add("Library", "probe", "Probe all", probeAll);
  add("Library", "tx", "Transcribe all...", openTranscribeAll, "", "whisper");
  add("Library", "target", "Detect all missing", detectAll);
  add("Library", "bolt", "Proxies for all with events", proxiesAll);
  add("Library", "link", "Import VOD from a link...", openVod, "", "download stream");
  add("Jobs", c.paused ? "play" : "pausej", c.paused ? "Resume jobs" : "Pause jobs", togglePause);
  add("Jobs", "refresh", "Retry failed jobs", jobsRetryFailed);
  add("Jobs", "check", "Clear finished jobs", jobsClearFinished);
  add("View", "info", "Event tab", () => showCtab("event"), "1");
  add("View", "info", "Recording tab", () => showCtab("rec"), "2");
  add("View", "wave", "Audio tracks", () => $("cp-audio").scrollIntoView({ block: "nearest" }), "", "always shown above the tabs");
  add("View", "tx", "Transcript tab", () => showCtab("tx"), "3");
  add("View", "filter", "Choose event kinds...", () => openKinds(null), "", "chips filter");
  if (resolveStartable()) add("App", "resolve", "Start DaVinci Resolve", resolveStartOrCheck);
  else add("App", "resolve", "Check DaVinci Resolve again", resolveStartOrCheck);
  add("App", "settings", "Open settings", () => openSettings(), "", "preferences");
  add("App", "download", "Add-ons and downloads...", () => openSettings("Add-ons"), "", "cuda gpu deno youtube yt-dlp whisper models packs");
  add("App", "settings", "Run setup again", () => { closeDrawer(); window.SU && SU.open({}); }, "", "wizard first run folders names");
  add("App", "refresh", "Check for a newer version", () => openSettings("Updates"), "", "update release github");
  add("App", "trash", "Clear clip cache...", () => openSettings("Clip cache"));
  add("App", "help", "Keyboard shortcuts", openHelp, "?");
  return C;
}
function openPalette() {
  const d = $("dlg-pal"); const q = $("pal-q"); let sel = 0, list = [];
  closePop(); closeCtx();
  const draw = () => {
    const words = q.value.toLowerCase().split(/\s+/).filter(Boolean);
    list = paletteCommands().filter(c => words.every(w => (c.label + " " + c.g + " " + (c.kw || "")).toLowerCase().includes(w)));
    const raw = q.value.trim();
    if (raw.length >= 2) {
      list.push({ g: "Search", ic: "tx", label: `Search what was said: \u201c${raw}\u201d`, act: () => { $("search").value = raw; $("gsearch").classList.add("has"); ST.q = raw; loadRecs(); runSearch(raw).then(() => openPanel("said")); } });
      list.push({ g: "Search", ic: "rec", label: `Filter recordings: \u201c${raw}\u201d`, act: () => { $("search").value = raw; $("gsearch").classList.add("has"); ST.q = raw; loadRecs(); openPanel("rec"); } });
      list.push({ g: "Search", ic: "clips", label: `Find clips with \u201c${raw}\u201d said nearby`, act: () => { $("f-said").value = raw; ST.finder.said = raw; finderSave(); finderMarks(); openPanel("clips"); runFind(); } });
    }
    sel = Math.max(0, Math.min(sel, list.length - 1));
    let g = null;
    $("pal-list").innerHTML = list.map((c, i) => (c.g !== g ? `<div class="mhdr">${esc(g = c.g)}</div>` : "") +
      `<button type="button" class="mi ${i === sel ? "act" : ""}" data-i="${i}">${icon(c.ic)}<span>${esc(c.label)}</span>${c.k ? `<span class="k">${esc(c.k)}</span>` : ""}</button>`).join("") || `<div class="empty">No command matches</div>`;
    $("pal-list").querySelectorAll(".mi").forEach(b => { b.onclick = () => run(+b.dataset.i); b.onmousemove = () => { if (sel !== +b.dataset.i) { sel = +b.dataset.i; $("pal-list").querySelectorAll(".mi").forEach(x => x.classList.toggle("act", x === b)); } }; });
    const a = $("pal-list").querySelector(".mi.act"); if (a) a.scrollIntoView({ block: "nearest" });
  };
  const run = (i) => { const c = list[i]; if (!c) return; d.close(); setTimeout(() => Promise.resolve().then(c.act).catch(err => toast(err.message, 5000)), 0); };
  q.oninput = () => { sel = 0; draw(); };
  q.onkeydown = (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); sel = Math.min(list.length - 1, sel + 1); draw(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); sel = Math.max(0, sel - 1); draw(); }
    else if (e.key === "Enter") { e.preventDefault(); run(sel); }
  };
  q.value = "";
  if (!d.open) d.showModal();
  draw(); q.focus();
}
function openHelp() { const d = $("dlg-help"); d.returnValue = ""; if (!d.open) d.showModal(); }
function renderHelp() {
  const K = (...k) => k.map(x => `<span class="kbd">${esc(x)}</span>`).join("");
  const groups = [
    ["Playback", [["Play / pause", K("Space")], ["Previous / next frame", K(",") + K(".")], ["Back / forward 1 s", K("\u2190") + K("\u2192")], ["Back / forward 5 s", K("Shift") + K("\u2190 \u2192")]]],
    ["Clip edit", [["Set in / out", K("I") + K("O")], ["Replay from in", K("R")], ["Loop in \u2192 out", K("L")], ["Mark here", K("M")]]],
    ["Review", [["Keep", K("Y")], ["False positive", K("X")], ["Reset status", K("U")], ["Next / previous event", K("N") + K("P")]]],
    ["Navigate", [["Next / previous recording", K("J") + K("K")], ["Search", K("/")], ["Command palette", K("Ctrl") + K("K")], ["Context tabs", K("1") + K("2") + K("3")]]],
    ["Panels", [["Fold / unfold the side panel", K("Ctrl") + K("B")], ["Close menu / dialog / calibration", K("Esc")], ["This overlay", K("?")]]],
    ["Zones editor", [["Nudge box 1 px (Shift: 10 px)", K("\u2190") + K("\u2191") + K("\u2192") + K("\u2193") + `<span class="faint t-xs" style="margin:0 2px">+</span>` + K("Shift")], ["Save", K("Ctrl") + K("S")], ["Grab at time", K("Enter")], ["Close the editor", K("Esc")], ["This overlay", K("?")]]]];
  $("help-body").innerHTML = `<div class="keys">${groups.map(([g, ks]) => `<div><h6>${g}</h6>${ks.map(([l, k]) => `<div class="kr"><span>${l}</span><span>${k}</span></div>`).join("")}</div>`).join("")}</div>
    <p class="faint t-xs" style="margin:16px 0 0">In Find clips, N / P step through the results across recordings. Shortcuts are off while typing in a field or while a dialog or Settings is open. Right-click a recording for its menu; the rail's active icon folds the side panel.</p>`;
}

/* ---------------- keyboard ---------------- */
function onKey(e) {
  const tag = e.target.tagName; const typing = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || e.target.isContentEditable;
  const dlgOpen = !!document.querySelector("dialog[open]");
  if (e.key === "Escape") {
    if (!$("lightbox").hidden) { $("lightbox").hidden = true; return; }
    if (!$("pop").hidden) { closePop(); return; }
    if (!$("pl-tools").hidden) { $("pl-tools").hidden = true; return; }
    if (!$("ctx").hidden) { closeCtx(); return; }
    if (!$("drawer").hidden) { closeDrawer(); return; }
    if (dlgOpen) return;                                           // the dialog closes itself
    if (typing) { e.target.blur(); return; }
    if (ST.view === "calibration") { closeCalibration(); return; }
    return;
  }
  if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === "k") {
    const pal = $("dlg-pal");
    if (pal.open) { e.preventDefault(); pal.close(); return; }
    if (dlgOpen || !$("drawer").hidden) return;
    e.preventDefault(); openPalette(); return;
  }
  if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === "b" && !dlgOpen && $("drawer").hidden) {
    e.preventDefault(); if (ST.panelOpen) closePanel(); else openPanel(ST.tab); return;
  }
  if (typing) return;
  if (dlgOpen) return;                                             // a modal owns the keyboard
  if (!$("drawer").hidden) return;                                 // so does Settings
  if (e.key === "/") { e.preventDefault(); focusSearch(); return; }
  if (e.ctrlKey || e.altKey || e.metaKey) return;
  if (ST.view === "zones") return;                                 // the zones editor owns the keys (arrows nudge a box)
  if (e.key === "?") { e.preventDefault(); openHelp(); return; }
  const k = e.key.length === 1 ? e.key.toLowerCase() : e.key;
  if (k === "j" || k === "k") { stepRec(k === "j" ? 1 : -1); return; }
  if (ST.view !== "normal") return;
  // n / p would start the (hidden) player behind the calibration view; in Find clips they step the results
  if (k === "n" || k === "p") { if (findShown()) stepFound(k === "n" ? 1 : -1); else stepEvent(k === "n" ? 1 : -1); return; }
  if (k === "y") { verdictKey("kept"); return; }
  if (k === "x") { verdictKey("false"); return; }
  if (k === "u") { if (ST.event && ST.event.status !== "new") verdictKey("new"); return; }
  if (k === "1" || k === "2" || k === "3") { showCtab(CTABS[+k - 1]); return; }
}

boot().catch(err => { setStatus("boot failed: " + err.message); console.error(err); });
