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
// the UI language's conventions (i18n.js): en-US "Sat, Nov 7, 3:45 PM" / "Sat, Nov 7, 2026"
const fmtWhen = (utc) => utc ? fmtDateTime(utc, "when") : "-";
const fmtDay = (utc) => utc ? fmtDateTime(utc, "day") : "-";
// seconds with the UI language's unit ("1.5 s"); job states and stages by their UI names (the API values stay)
const secTxt = (v, d) => I18N.intl("num", { style: "unit", unit: "second", unitDisplay: "short", maximumFractionDigits: d == null ? 1 : d }).format(Number(v) || 0);
const stateName = (s) => I18N.has("app.state." + s) ? t("app.state." + s) : s;
const stageName = (s) => I18N.has("app.stage." + s) ? t("app.stage." + s) : s;
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
  if (setupDone && roots.length && !ST.recs.length && !ST.drive && !ST.q) { toast(t("app.boot.empty_scanning"), 4000); scan(); }
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
  if (window.VB) VB.open(opts || {}); else toast(t("app.err.vb_missing"));
}
async function toggleZones() {
  if (!window.ZN) { toast(t("app.err.zones_missing")); return; }
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
    el.innerHTML = `<button type="button" class="btn sm" id="d-detect">${icon("target")}${st.detect ? te("app.act.redetect") : te("app.act.detect")}</button>
      <button type="button" class="btn sm" id="d-transcribe">${icon("mic")}${(r.transcript && r.transcript.n) || st.transcribe ? te("app.act.retranscribe") : te("app.act.transcribe")}</button>
      <button type="button" class="ib sm" data-menu="rec" data-tip="${te("app.ctx.rec_more_tip")}">${icon("more")}</button>`;
    $("d-detect").onclick = () => detectRec(r.id);
    $("d-transcribe").onclick = () => transcribeRec(r.id);
  } else if (ST.ctab === "tx") {
    const sum = ST.transcript.rec_id === r.id && ST.transcript.summary ? ST.transcript.summary : (r.transcript || { n: 0 });
    el.innerHTML = `<button type="button" class="btn sm" id="t-run">${icon("mic")}${sum.n || st.transcribe ? te("app.act.retranscribe") : te("app.act.transcribe")}</button>
      <button type="button" class="btn sm ghost" id="t-all" title="${te("app.ctx.transcribe_all_title")}">${te("app.act.transcribe_all")}</button>`;
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
  const when = fmtWhen(recStart(r) || r.mtime);
  return t(r.kind === "replay" ? "app.rec.label_replay" : r.kind === "vod" ? "app.rec.label_vod" : "app.rec.label_recording", { when });
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
  return /^replay\b/i.test(stem) ? t("app.rec.label_saved", { name: stem, when }) : `${stem} · ${when}`;
}
function statusBadge(r) {
  if (r.missing) return `<span class="badge warn" title="${te("app.badge.missing_title")}">${te("app.badge.missing")}</span>`;
  if (r.status === "error") return `<span class="badge bad" title="${esc(srvF(r, "error"))}">${icon("warn")}${te("app.badge.probe_failed")}</span>`;
  if (r.status === "new") return `<span class="badge" title="${te("app.badge.new_title")}">${te("app.badge.new")}</span>`;
  return "";
}
function renderCrumb() {
  const r = ST.detail; let h = "";
  if (r) h += `<span class="sep">/</span><b title="${te("app.crumb.rec_title", { path: r.path || r.name })}">${esc(recLabel(r))}</b><span class="muted num">${fmtDurShort(r.duration_s)}</span>${statusBadge(r)}`;
  if (ST.view === "calibration") h += `<span class="sep">/</span><span class="view">${te("app.crumb.calibration")}</span>`;
  else if (ST.view === "zones") h += `<span class="sep">/</span><span class="view">${te("app.crumb.zones")}</span>`;
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
    { ic: "scan", label: t("app.menu.scan"), sub: t("app.menu.scan_sub"), act: scan },
    { ic: "probe", label: t("app.menu.probe_all"), sub: t("app.menu.probe_all_sub"), act: probeAll },
    { ic: "tx", label: t("app.act.transcribe_all"), act: openTranscribeAll },
    { ic: "target", label: t("app.menu.detect_all"), act: detectAll },
    { ic: "bolt", label: t("app.menu.proxies_all"), act: proxiesAll }, "-",
    { ic: "link", label: t("app.menu.import_vod"), act: openVod },
    { ic: "vods", label: t("app.menu.browse_vods"), act: () => openVods() }],
  events: () => [
    { ic: "target", label: t("app.menu.detect_all"), sub: t("app.menu.detect_all_sub"), act: detectAll },
    { ic: "calib", label: t("app.menu.calibrate"), sub: t("app.menu.calibrate_sub"), act: () => openCalibration(ST.sel || 0) },
    { ic: "zones", label: t("app.menu.zones"), act: () => window.ZN && ZN.open(ST.detail ? { rec_id: ST.detail.id } : {}) }],
  jobs: () => [
    { ic: "refresh", label: t("app.menu.retry_failed"), act: jobsRetryFailed },
    { ic: "check", label: t("app.menu.clear_finished"), sub: t("app.menu.clear_finished_sub"), act: jobsClearFinished },
    { ic: "trash", label: t("app.menu.clear_all"), sub: t("app.menu.clear_all_sub"), act: jobsClearAll, danger: true }],
  rec: (arg) => recMenuItems(arg ? +arg : ST.sel),
  event: () => {
    const e = ST.event; if (!e) return [{ label: t("app.menu.no_event"), disabled: true }];
    return [{ ic: "copy", label: t("app.menu.copy_tc"), act: () => copyText(tc(e.t_s)) },
      { ic: "send", label: t("app.menu.send"), sub: t("app.menu.send_sub"), act: () => openHandoff($("btn-send")) },
      { ic: "calib", label: t("app.menu.calibrate_here"), act: () => openCalibration(e.rec_id) },
      ...(e.kind === "manual" ? ["-", { ic: "trash", label: t("app.menu.delete_manual"), danger: true, act: () => deleteManual(e) }] : [])];
  },
};
function recMenuItems(id) {
  const r = ST.recs.find(x => x.id === id) || (ST.detail && ST.detail.id === id ? ST.detail : null); if (!r) return [{ label: t("app.menu.no_rec"), disabled: true }];
  const st = r.stages || {};
  return [
    { ic: "target", label: st.detect ? t("app.act.redetect") : t("app.menu.detect_kd"), act: () => detectRec(id) },
    { ic: "events", label: t("app.menu.show_events"), act: () => { select(id, { events: true }); openPanel("events"); } },
    { ic: "mic", label: r.n_segments || st.transcribe ? t("app.act.retranscribe") : t("app.act.transcribe"), act: () => transcribeRec(id) },
    ...(window.SL ? [SL.menuItem(r)] : []),
    { ic: "tx", label: t("app.menu.show_tx"), act: () => { select(id, { transcript: true }); showCtab("tx"); } },
    { ic: "zones", label: t("app.menu.zones"), act: () => window.ZN && ZN.open({ rec_id: id }) },
    { ic: "qtop", label: t("app.menu.queue_top"), act: () => window.QM && QM.recToTop(id) },
    ...(r.kind === "vod" && r.channel_key ? [{ ic: "vods", label: t("app.menu.more_vods", { channel: r.channel || r.channel_key }),
      act: () => window.VB && VB.open({ provider: provOf(r.name), key: r.channel_key, name: r.channel, url: r.channel_url }) }] : []),
    "-",
    { ic: "folder", label: t("app.menu.reveal"), act: () => post(`/api/recordings/${id}/reveal`) },
    { ic: "copy", label: t("app.menu.copy_path"), act: () => copyText(r.path) },
    { ic: "probe", label: t("app.menu.reprobe"), act: async () => { await post(`/api/recordings/${id}/probe`); toast(t("app.toast.probe_queued")); } }];
}

/* the kinds filter: ONE compact button + popover (Events, the timeline, Find clips) */
function kindsBtn() {
  const on = KINDS.filter(k => ST.chips[k]);
  return `<button type="button" class="btn sm kbtn" data-kinds title="${te("app.kinds.btn_title")}">${icon("filter")}<span class="kds">${on.slice(0, 5).map(k => `<i class="k-${k}"></i>`).join("")}</span>${on.length === KINDS.length ? te("app.kinds.all") : te("app.kinds.n_of", { n: fmtNum(on.length), total: fmtNum(KINDS.length) })}${icon("chevd")}</button>`;
}
function paintKindsBtns() { document.querySelectorAll("[data-kinds-slot]").forEach(s => { s.innerHTML = kindsBtn(); }); }
function openKinds(anchor) {
  const counts = {}; for (const e of (ST.events.rec_id === ST.sel ? ST.events.list : [])) counts[e.kind] = (counts[e.kind] || 0) + 1;
  const draw = () => {
    const p = $("pop");
    p.innerHTML = `<div class="mhdr">${te("app.kinds.header")}</div>` + KINDS.map(k => `<button type="button" class="kopt k-${k} ${ST.chips[k] ? "on" : ""}" data-k="${k}"><span class="cbx ${ST.chips[k] ? "on" : ""}"><svg viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7"/></svg></span><span class="kd"></span><span>${esc(KIND_NAME[k])}${k === "revive" || k === "squad" ? ` <small class="faint">${te("app.kinds.off_default")}</small>` : ""}</span><span class="num t-xs muted" title="${te("app.kinds.count_title")}">${counts[k] || ""}</span></button>`).join("") +
      `<div class="msep"></div><div class="acts" style="padding:4px"><button type="button" class="btn ghost sm" data-all="1">${te("app.kinds.btn_all")}</button><button type="button" class="btn ghost sm" data-all="0">${te("app.kinds.btn_highlights")}</button></div>`;
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
  try { ST.status = await api("/api/status"); } catch (err) { setStatus(t("app.status.unreachable")); return; }
  ST.counts = ST.status.jobs;
  renderLegend(); renderPill();
  const drives = (ST.status.library.drives || []).map(d => d.drive).filter(Boolean);
  const sel = $("sel-drive"); const cur = sel.value;
  sel.innerHTML = `<option value="">${te("app.status.all_drives")}</option>` + drives.map(d => `<option value="${esc(d)}" ${d === cur ? "selected" : ""}>${esc(d)}</option>`).join("");
}
function renderLegend() {
  const s = ST.status; if (!s) return;
  const tot = s.library.total;
  const per = (s.library.drives || []).map(d => t(d.missing ? "app.legend.drive_missing" : "app.legend.drive", { drive: d.drive || "?", count: d.n, hours: fmtHours(d.secs || 0), size: fmtBytes(d.bytes), missing: fmtNum(d.missing) })).join("\n");
  const lg = $("legend");
  lg.innerHTML = te("app.legend.summary", { count: tot.files, hours: fmtUnit(Math.round(tot.secs / 3600), "hour"), size: fmtBytes(tot.bytes), probed: fmtNum(tot.probed) }) + (tot.missing ? ` · <span class="bad">${te("app.legend.missing", { count: tot.missing })}</span>` : "");
  lg.title = per;
  const ls = s.library.last_scan; const watch = ST.settings && ST.settings.library && ST.settings.library.auto_scan_new;
  $("sb-watch").textContent = `${watch ? t("app.legend.watch_on") : t("app.legend.watch_off")} · ${ls ? t("app.legend.last_scan", { ago: fmtAgo(ls.started_utc), elapsed: fmtUnit(ls.elapsed_s || 0, "second") }) : t("app.legend.not_scanned")}${s.ffmpeg && !s.ffmpeg.found ? " · " + t("app.legend.ffmpeg_missing") : ""}`;
  $("sb-watch").title = $("sb-watch").textContent;     // ellipsized in long languages
}
function renderPill() {
  const c = ST.counts || {}; const p = $("pill-jobs");
  const dot = c.paused ? "warn" : c.running ? "acc pulse" : c.failed ? "bad" : "ok";
  p.querySelector(".sd").className = "sd " + dot;
  $("pill-jobs-t").innerHTML = (c.paused ? te("app.pill.paused") : c.running ? te("app.pill.running", { count: c.running }) : te("app.pill.idle")) + (c.queued ? ` · ${te("app.pill.queued", { count: c.queued })}` : "") + (c.failed ? ` · <span class="bad">${te("app.pill.failed", { count: c.failed })}</span>` : "");
  p.classList.toggle("paused", !!c.paused);
  const b = $("btn-pause"); b.innerHTML = icon(c.paused ? "play" : "pausej"); b.dataset.tip = c.paused ? t("app.pause.resume_tip") : t("app.pause.pause_tip");
  b.classList.toggle("paused", !!c.paused);
  const jp = $("j-pause"); if (jp) { jp.innerHTML = icon(c.paused ? "play" : "pausej") + (c.paused ? te("app.pause.resume") : te("app.pause.pause")); jp.classList.toggle("pri", !!c.paused); }
  renderRail();
}
async function togglePause() {
  try { const r = await post("/api/jobs/pause", { paused: !ST.counts.paused }); ST.counts.paused = r.paused; renderPill(); toast(r.paused ? t("app.toast.paused") : t("app.toast.resumed")); }
  catch (err) { toast(err.message, 5000); }
}
// a server text field ("" when empty): the translation when the server sent <field>_i18n, else its English
function srvF(o, f) { return o && o[f] ? srvText(o, f) : ""; }
const encLabel = (c) => c === "libx264" ? "x264" : c === "h264_nvenc" ? "NVENC" : c === "hevc_nvenc" ? "NVENC HEVC" : (c || "-");
function refreshEncoder() {
  return api("/api/system/encoder").then(e => {
    ST.encoder = e; const el = $("sb-enc");
    el.textContent = encLabel(e.clip_effective) + (e.proxy_effective && e.proxy_effective !== e.clip_effective ? ` / ${encLabel(e.proxy_effective)}` : "");
    el.dataset.tip = t("app.enc.tip", { clip: encLabel(e.clip_effective), proxy: encLabel(e.proxy_effective), auto: encLabel(e.auto_now), why: srvF(e, "why") });
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
  const n = (L.waiting || []).length; const w = n ? " " + t("app.resolve.waiting_n", { count: n }) : "";
  let text, dot = "", tip;
  if (!r.enabled) { text = t("app.resolve.off"); tip = t("app.resolve.off_tip"); }
  else if (!r.available) { text = t("app.resolve.not_installed"); dot = "bad"; tip = srvF(r, "error"); }
  else if (L.state === "starting") { text = t("app.resolve.starting") + w; dot = "warn pulse"; tip = t("app.resolve.starting_tip"); }
  else if (L.state === "no_project") { text = t("app.resolve.open_project") + w; dot = "warn"; tip = srvF(L, "error") || t("app.resolve.open_project_tip"); }
  else if (L.state === "failed") { text = t("app.resolve.failed"); dot = "bad"; tip = srvF(L, "error"); }
  else if (r.connected && r.has_project) { text = t("app.resolve.project", { project: r.project }) + w; dot = "ok"; tip = t(r.timeline ? "app.resolve.project_tl_tip" : "app.resolve.project_tip", { product: r.product || "Resolve", version: r.version || "", project: r.project, timeline: r.timeline || "" }); }
  else if (r.connected) { text = t("app.resolve.no_project") + w; dot = "warn"; tip = t("app.resolve.no_project_tip"); }
  else { text = t("app.resolve.not_running") + w; tip = srvF(r, "error"); }
  $("pill-resolve-t").textContent = text; p.querySelector(".sd").className = "sd " + dot;
  const startable = resolveStartable();
  $("pill-resolve-start").hidden = !(startable && !r.connected);
  p.dataset.tip = (tip ? tip + " | " : "") + (n || ["starting", "no_project", "failed"].includes(L.state) ? t("app.resolve.click_wait") : startable ? t("app.resolve.click_start") : t("app.resolve.click_check"));
  const box = $("ho-wait"); if (box) box.innerHTML = renderResolveWait();
  const hb = $("ho-badge"); if (hb) hb.outerHTML = handoffBadge();
  wireResolveWait();
}
// the status-bar pill: while hand-offs wait (or the start failed) it opens what waits, with Cancel /
// "Use <project>" / "Start Resolve again"; otherwise it starts Resolve or checks again
function openResolveWait() {
  const p = $("pill-resolve");
  showPop(p, `<div class="handoff"><h5>${icon("resolve")}DaVinci Resolve<span class="sp"></span>${handoffBadge()}</h5><div id="ho-wait">${renderResolveWait()}</div>
    <div class="acts"><button type="button" class="btn sm" id="ho-check">${resolveStartable() ? te("app.resolve.start") : te("app.resolve.check")}</button><button type="button" class="btn ghost sm" id="ho-open">${te("app.resolve.send")}</button></div></div>`, "handoff");
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
    try { const x = await post("/api/resolve/start"); ST.resolve.link = x.link; renderResolvePill(); toast(t("app.resolve.starting_toast"), 4000); }
    catch (err) { toast(err.message, 6000); }
    return;
  }
  $("pill-resolve-t").textContent = t("app.resolve.checking"); refreshResolve(true);
}
// what waits for Resolve, with Cancel / "use the open project"
function renderResolveWait() {
  const L = resolveLink(); const ops = L.waiting || [];
  if (!ops.length && !["starting", "no_project", "failed"].includes(L.state)) return "";
  const st = { starting: t("app.resolve.st_starting"), no_project: srvF(L, "error") || t("app.resolve.st_no_project"), failed: srvF(L, "error") || t("app.resolve.st_failed"), ready: t("app.resolve.st_ready"), down: t("app.resolve.st_down") }[L.state] || L.state;
  let h = `<div class="howait"><b>${ops.length ? te("app.resolve.handoffs_waiting", { count: ops.length }) : "Resolve"}</b> <span class="muted">${esc(st)}</span>`;
  if (L.state === "no_project" && ST.resolve && ST.resolve.connected && ST.resolve.project) h += ` <button type="button" class="btn sm" id="ho-useopen" title="${te("app.resolve.use_open_title", { project: ST.resolve.project })}">${te("app.resolve.use_open", { project: ST.resolve.project })}</button>`;
  if (L.state === "failed") h += ` <button type="button" class="btn sm" id="ho-retry">${te("app.resolve.start_again")}</button>`;
  if (ops.length) h += ` <button type="button" class="btn ghost sm" id="ho-cancelall">${te("app.resolve.cancel")}</button>`;
  h += ops.map(o => `<div class="t-xs">#${o.id} ${esc(srvF(o, "label"))} - ${esc(stateName(o.state))}${o.note ? ` <span style="color:var(--warn)">(${esc(srvF(o, "note"))})</span>` : ""}</div>`).join("");
  return h + `</div>`;
}
function wireResolveWait() {
  const u = $("ho-useopen"); if (u) u.onclick = (e) => { e.stopPropagation(); post("/api/resolve/use-open-project").then(x => { ST.resolve.link = x.link; renderResolvePill(); }).catch(err => toast(err.message, 5000)); };
  const c = $("ho-cancelall"); if (c) c.onclick = (e) => { e.stopPropagation(); post("/api/resolve/cancel", {}).then(x => { ST.resolve.link = x.link; renderResolvePill(); toast(t("app.resolve.canceled", { count: x.cancelled })); }).catch(err => toast(err.message, 5000)); };
  const rt = $("ho-retry"); if (rt) rt.onclick = (e) => { e.stopPropagation(); resolveStartOrCheck(); };
}
function handoffMsg(kind, r) {
  if (r.note) return t(kind === "markers" ? "app.ho.note_markers" : "app.ho.note_selects", { note: srvText(r, "note") });
  const failed = (r.failed || []).length ? " " + t("app.ho.n_failed", { count: r.failed.length }) : "";
  if (kind === "markers") return t("app.ho.markers_sent", { count: r.sent, project: r.project }) + ((r.imported || []).length ? " " + t("app.ho.files_imported", { count: r.imported.length }) : "") + ((r.bins || []).length ? " " + t("app.ho.media_bin", { bins: fmtList(r.bins) }) : "") + failed;
  return t("app.ho.timeline_made", { timeline: r.timeline, project: r.project, clips: fmtNum(r.clips), events: fmtNum(r.events) }) + failed;
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
  const label = { markers: t("app.ho.sending_markers"), selects: t("app.ho.building_selects"), export: t("app.ho.exporting") }[action];
  if (res) res.textContent = label;
  try {
    let r;
    if (action === "markers" || action === "selects") {
      r = await post(`/api/resolve/${action}`, sel);
      if (r.queued) {
        // Resolve is down / has no project: it is being started and the hand-off runs once a project is open
        if (ST.resolve) ST.resolve.link = r.link;
        renderResolvePill();
        const msg = t(action === "markers" ? "app.ho.queued_markers" : "app.ho.queued_selects", { state: r.link.state === "starting" ? t("app.ho.state_starting") : r.link.state === "no_project" ? t("app.ho.state_no_project") : t("app.ho.state_connecting") });
        toast(msg, 6000); if ($("ho-res")) $("ho-res").textContent = msg;
        return r;
      }
      const msg = handoffMsg(action, r);
      toast(msg, 5000); if ($("ho-res")) $("ho-res").textContent = msg + (action === "markers" && r.failed.length ? "\n" + r.failed.map(f => `#${f.event_id}: ${srvF(f, "error")}`).join("\n") : "");
      if (action === "markers" && ST.sel) loadEvents(ST.sel);       // the "in Resolve" badges
    } else {
      r = await post("/api/export", Object.assign(sel, { format: ST.handoff.fmt || "both" }));
      if ($("ho-res")) $("ho-res").innerHTML = te("app.ho.export_res", { events: fmtNum(r.events), clips: fmtNum(r.clips), dir: r.out_dir }) + "<br>" + r.files.map(f => `<a href="${esc(f.url)}">${esc(f.name)}</a>`).join(" | ");
      toast(t("app.ho.exported", { kinds: fmtList(r.files.map(f => f.kind)) }));
    }
    return r;
  } catch (err) { toast(err.message, 8000); if ($("ho-res")) $("ho-res").textContent = err.message; }
}
async function handoffPreview() {
  const el = $("ho-prev"); if (!el) return;
  if (ST.handoff.scope === "rec" && !ST.detail) { el.textContent = t("app.ho.pick_rec"); return; }
  try { const r = await post("/api/resolve/preview", handoffSelection()); if ($("ho-prev")) $("ho-prev").innerHTML = tHtml("app.ho.preview_html", { events: fmtNum(r.events), clips: fmtNum(r.clips), duration: fmtDur(r.seconds), recordings: t("app.rec.count", { count: r.recordings }) }); }
  catch (err) { if ($("ho-prev")) $("ho-prev").textContent = err.message; }
}
function handoffBadge() { const r = ST.resolve || {}; return `<span class="badge ${r.connected ? "ok" : ""}" id="ho-badge">${r.connected ? (r.project ? te("app.ho.badge_project", { project: r.project }) : te("app.ho.badge_no_project")) : te("app.ho.badge_off")}</span>`; }
function renderHandoff() {
  const h = ST.handoff; const noRec = h.scope === "rec" && !ST.detail;
  const opt = (v, cur, label) => `<option value="${v}" ${String(v) === String(cur) ? "selected" : ""}>${esc(label)}</option>`;
  return `<div class="handoff"><h5>${icon("resolve")}${te("app.ho.title")}<span class="sp"></span>${handoffBadge()}</h5>
    <div class="hrow"><span>${te("app.ho.from")}</span><div class="seg" id="ho-scope"><button type="button" data-s="rec" class="${h.scope === "rec" ? "on" : ""}">${te("app.ho.this_rec")}</button><button type="button" data-s="all" class="${h.scope === "all" ? "on" : ""}">${te("app.ho.every_rec")}</button></div></div>
    <div class="hrow"><span>${te("app.ho.status")}</span><select class="inp" id="ho-status">${opt("kept", h.status, t("app.ho.status_kept"))}${opt("kept,new", h.status, t("app.ho.status_kept_new"))}${opt("any", h.status, t("app.ho.status_any"))}</select></div>
    <div class="hrow"><span>${te("app.ho.min_score")}</span><input class="inp" type="number" id="ho-min" step="0.1" min="0" value="${h.min_score}"></div>
    <div class="hrow"><span>${te("app.ho.kinds")}</span><input class="inp" type="text" id="ho-kinds" placeholder="${te("app.ho.kinds_ph")}" value="${esc(h.kinds)}"></div>
    <div class="hrow"><span>${te("app.ho.order")}</span><select class="inp" id="ho-order">${opt("time", h.order, t("app.ho.order_time"))}${opt("score", h.order, t("app.ho.order_score"))}</select></div>
    <div class="hrow"><span>${te("app.ho.export_as")}</span><select class="inp" id="ho-fmt">${opt("both", h.fmt || "both", "CSV + EDL")}${opt("edl", h.fmt, "EDL")}${opt("csv", h.fmt, "CSV")}${opt("premiere", h.fmt, t("app.ho.fmt_premiere"))}${opt("otio", h.fmt, t("app.ho.fmt_otio"))}</select></div>
    <div class="t-xs faint" id="ho-fmt-note" ${["premiere", "otio"].includes(h.fmt) ? "" : "hidden"}>${te("app.ho.fmt_note")}</div>
    <div class="hprev" id="ho-prev">-</div>
    <div class="acts"><button type="button" class="btn pri sm" id="ho-markers" ${noRec ? "disabled" : ""} title="${te("app.ho.markers_title")}">${te("app.ho.markers")}</button>
      <button type="button" class="btn sm" id="ho-selects" ${noRec ? "disabled" : ""} title="${te("app.ho.selects_title")}">${te("app.ho.selects")}</button>
      <button type="button" class="btn sm" id="ho-export" ${noRec ? "disabled" : ""} title="${te("app.ho.export_title")}">${te("app.ho.export")}</button></div>
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
  const fm = $("ho-fmt"); if (fm) fm.onchange = () => { h.fmt = fm.value; const n = $("ho-fmt-note"); if (n) n.hidden = !["premiere", "otio"].includes(h.fmt); };
  $("ho-scope").querySelectorAll("button").forEach(b => b.onclick = (e) => { e.stopPropagation(); h.scope = b.dataset.s; const p = $("pop"); p.innerHTML = renderHandoff(); hydrateIcons(p); wireHandoff(); });
  $("ho-markers").onclick = (e) => { e.stopPropagation(); handoff("markers"); };
  $("ho-selects").onclick = (e) => { e.stopPropagation(); handoff("selects"); };
  $("ho-export").onclick = (e) => { e.stopPropagation(); handoff("export"); };
  wireResolveWait();
  handoffPreview();
}

/* ---------------- library actions ---------------- */
async function scan() {
  setStatus(t("app.scan.running"));
  try {
    const s = await post("/api/library/scan");
    setStatus(t(s.growing ? "app.scan.result_growing" : "app.scan.result", { found: fmtNum(s.found), new: fmtNum(s.new), moved: fmtNum(s.moved), missing: fmtNum(s.missing), growing: fmtNum(s.growing) }));
    const bad = s.roots.filter(r => !r.ok); if (bad.length) toast(t("app.scan.unreachable", { folders: fmtList(bad.map(r => r.root)) }), 6000);
    await refreshStatus(); await loadRecs();
  } catch (err) { setStatus(t("app.scan.failed")); toast(err.message, 6000); }
}
const probeAll = () => post("/api/recordings/probe-all").then(r => toast(t("app.toast.probes_queued", { queued: fmtNum(r.queued), candidates: fmtNum(r.candidates) }))).catch(err => toast(err.message, 5000));
const detectAll = () => post("/api/detect-all").then(x => toast(t("app.toast.detect_queued", { queued: fmtNum(x.queued), candidates: fmtNum(x.candidates) }))).catch(err => toast(err.message, 5000));
const proxiesAll = () => post("/api/proxies/build-all").then(r => toast(t("app.toast.proxies_queued", { queued: fmtNum(r.queued), candidates: fmtNum(r.candidates) }))).catch(err => toast(err.message, 5000));

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
  if (r.missing) b.push(`<span class="badge bad">${te("app.badge.missing")}</span>`);
  else if (r.status === "probed") b.push(`<span class="badge ok">${te("app.badge.probed")}</span>`);
  else if (r.status === "error") b.push(`<span class="badge bad" title="${esc(srvF(r, "error"))}">${te("app.badge.error")}</span>`);
  else b.push(`<span class="badge">${te("app.badge.new")}</span>`);
  if (r.kind === "replay") b.push(`<span class="badge replay">${te("app.badge.replay")}</span>`);
  if (r.kind === "vod") b.push(`<span class="badge vod" title="${esc(r.source_url || t("app.badge.vod_title"))}">${esc(provOf(r.name) || t("app.badge.vod"))}</span>`);
  return b.join(" ");
}
function recEventsCell(r) {
  const det = r.stages && r.stages.detect;
  const k = r.n_kills || 0, d = r.n_deaths || 0, n = r.n_events || 0;
  const dots = (k ? `<span class="k-kill" title="${te("app.cell.kills", { count: k })}">${fmtNum(k)}</span>` : "") + (d ? `<span class="k-death" title="${te("app.cell.deaths", { count: d })}">${fmtNum(d)}</span>` : "");
  if (dots) return `<span class="kdots" title="${te("app.cell.events", { count: n })}">${dots}</span>`;
  if (n) return `<span class="ti" title="${te("app.cell.events_no_kd", { count: n })}">${icon("events")}${fmtNum(n)}</span>`;
  if (det && det.state === "done") return `<span class="zero" title="${te("app.cell.found_nothing")}">${te("app.cell.events", { count: 0 })}</span>`;
  return `<span class="zero">${te("app.cell.not_detected")}</span>`;
}
function recTxCell(r) {
  const jp = ST.jobprog[r.id];
  if (jp && jp.stage === "transcribe") return `<span class="ti acc" title="${te("app.cell.transcribing")}">${icon("tx")}${pct(jp.progress)}</span>`;
  if (r.n_segments) return `<span class="ti" title="${te("app.cell.tx_lines", { count: r.n_segments })}">${icon("tx")}${fmtNum(r.n_segments)}</span>`;
  const ts = r.stages && r.stages.transcribe;
  if (ts && ts.state === "done") return `<span class="ti zero" title="${esc(srvF(ts, "note") || t("app.cell.nobody_spoke"))}">${icon("tx")}${te("app.cell.lines", { count: 0 })}</span>`;
  if (ts && ts.state === "failed") return `<span class="ti" style="color:var(--bad-text)" title="${esc(srvF(ts, "note"))}">${icon("tx")}${te("app.cell.failed")}</span>`;
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
  sub.innerHTML = (ST.q ? `<span class="chip on" style="cursor:default">${icon("search")}${te("app.recs.name_contains", { q: ST.q })}<button type="button" class="ib sm" style="width:20px;height:20px" id="rec-clrq" title="${te("app.recs.clear_search_title")}">${icon("x")}</button></span>` : "") +
    (bad ? `<span class="badge bad">${icon("warn")}${te("app.recs.need_attention", { count: bad })}</span><span class="t-xs muted">${te("app.recs.need_attention_why")}</span>` : "");
  sub.hidden = !sub.innerHTML;
  const cq = $("rec-clrq"); if (cq) cq.onclick = clearSearch;
  const secs = ST.recs.reduce((a, r) => a + (r.duration_s || 0), 0);
  $("rec-foot").textContent = `${t("app.rec.count", { count: ST.recs.length })}${secs ? " \u00b7 " + fmtHours(secs) : ""}${ST.drive ? " \u00b7 " + ST.drive : ""}`;
  if (!ST.recs.length) {
    el.innerHTML = `<div class="empty"><b>${ST.q ? te("app.recs.none_q", { q: ST.q }) : te("app.recs.none")}</b>${ST.q ? te("app.recs.none_q_hint") : te("app.recs.none_hint")}<div class="acts">${ST.q ? `<button type="button" class="btn sm" id="rec-clr2">${te("app.recs.clear_search")}</button>` : `<button type="button" class="btn sm" id="rec-scan">${icon("scan")}${te("app.menu.scan")}</button>`}</div></div>`;
    const a = $("rec-clr2"); if (a) a.onclick = clearSearch; const b = $("rec-scan"); if (b) b.onclick = scan;
    return;
  }
  const byDay = new Map();
  for (const r of ST.recs) { const s = recStart(r) || r.mtime; const d = fmtDateTime(s, "date"); if (!byDay.has(d)) byDay.set(d, { utc: s, rows: [] }); byDay.get(d).rows.push(r); }
  let html = "";
  for (const [, g] of byDay) {
    const secs = g.rows.reduce((a, r) => a + (r.duration_s || 0), 0);
    html += `<div class="day"><b>${esc(fmtDay(g.utc))}</b><span>${te("app.rec.count", { count: g.rows.length })}${secs ? " \u00b7 " + fmtDur(secs) : ""}</span></div>`;
    for (const r of g.rows) {
      const jp = ST.jobprog[r.id];
      const title = r.kind === "vod" && r.title ? r.title : nameStem(r.name);
      html += `<div class="row ${r.id === ST.sel ? "sel" : ""} ${r.missing ? "missing" : ""} ${r.status === "error" ? "err" : ""}" data-id="${r.id}" id="rec-${r.id}" title="${esc(r.name)}${r.error ? "\n" + esc(srvF(r, "error")) : ""}">
        ${recThumb(r)}<div class="main"><div class="l1"><span class="tm">${esc(fmtDateTime(recStart(r) || r.mtime, "hm"))}</span>${r.kind === "vod" ? provBadge(provOf(r.name)) : ""}<span class="t">${esc(title)}</span></div>
          <div class="l2">${r.status === "error" ? `<span class="err">${esc(srvF(r, "error") || t("app.badge.probe_failed"))}</span>` : recEventsCell(r) + recTxCell(r)}</div></div>
        <div class="r"><span class="num muted t-xs">${r.status === "error" ? "\u2013" : fmtDurShort(r.duration_s)}</span>
          <span class="rr">${statusBadge(r)}<button type="button" class="ib sm info" data-info="${r.id}" title="${te("app.recs.details_title")}" aria-label="${te("app.recs.details_aria")}">${icon("info")}</button></span></div>
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
  const st = Object.entries(r.stages || {}).map(([k, v]) => `<span class="badge ${v.state === "done" ? "ok" : v.state === "failed" ? "bad" : ""}" title="${esc(fmtDate(v.utc))}${v.note ? " - " + esc(srvF(v, "note")) : ""}">${esc(I18N.has("app.stage." + k) ? t("app.stage." + k) : k)}</span>`).join(" ");
  const p = showPop(anchor, `<h5>${esc(r.kind === "vod" && r.title ? r.title : r.name)}</h5><dl class="kv">
    <dt>${te("app.info.file")}</dt><dd class="mono">${esc(r.path || r.name)}</dd>
    <dt>${te("app.info.drive")}</dt><dd>${esc(r.drive || "-")} · ${fmtBytes(r.size)}</dd>
    <dt>${te("app.info.video")}</dt><dd>${r.width ? `${r.width}×${r.height} ${esc(r.vcodec || "")} ${r.fps ? te("app.info.fps", { fps: fmtNum(Math.round(r.fps)) }) : ""} ${fmtMbps(r.bitrate)}` : te("app.info.unknown")}</dd>
    <dt>${r.kind === "replay" ? te("app.info.replay_saved") : te("app.info.recorded")}</dt><dd>${fmtDate(r.name_utc)}</dd>
    <dt>${te("app.info.modified")}</dt><dd>${fmtDate(r.mtime)}</dd>
    <dt>${te("app.info.duration")}</dt><dd>${fmtDur(r.duration_s)}</dd>
    <dt>${te("app.info.status")}</dt><dd>${recBadges(r)}${r.error ? `<div class="t-xs" style="margin-top:4px;color:var(--bad-text)">${esc(srvF(r, "error"))}</div>` : ""}</dd>
    <dt>${te("app.info.stages")}</dt><dd>${st || `<span class="muted">${te("app.info.none_yet")}</span>`}</dd>
    <dt>${te("app.info.events")}</dt><dd>${r.n_events ? te("app.info.events_n", { events: fmtNum(r.n_events), kills: fmtNum(r.n_kills || 0), deaths: fmtNum(r.n_deaths || 0) }) : r.stages && r.stages.detect ? te("app.info.nothing_detected") : te("app.cell.not_detected")}</dd>
    <dt>${te("app.info.transcript")}</dt><dd>${r.n_segments ? te("app.cell.lines", { count: r.n_segments }) : r.stages && r.stages.transcribe ? esc(srvF(r.stages.transcribe, "note") || t("app.info.no_speech")) : te("app.info.not_transcribed")}</dd>
    ${r.source_url ? `<dt>${te("app.info.source")}</dt><dd><a href="${esc(r.source_url)}" target="_blank" rel="noopener">${esc(r.channel || r.source_url)}</a></dd>` : ""}
    </dl><div class="acts"><button type="button" class="btn sm" id="ri-open">${te("app.info.open")}</button><button type="button" class="btn sm ghost" id="ri-reveal">${icon("folder")}${te("app.info.reveal")}</button>${r.status === "error" || r.missing ? `<button type="button" class="btn sm ghost" id="ri-probe">${icon("probe")}${te("app.menu.reprobe")}</button>` : ""}</div>`, "info");
  p.querySelector("#ri-open").onclick = () => { closePop(); select(id); };
  p.querySelector("#ri-reveal").onclick = () => post(`/api/recordings/${id}/reveal`).catch(err => toast(err.message, 5000));
  const pr = p.querySelector("#ri-probe"); if (pr) pr.onclick = () => post(`/api/recordings/${id}/probe`).then(() => { toast(t("app.toast.probe_queued")); closePop(); }).catch(err => toast(err.message, 5000));
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
  if (!r) { el.innerHTML = `<div class="empty"><b>${te("app.ev.no_rec")}</b>${te("app.ev.no_rec_hint")}</div>`; return; }
  if (!e) { el.innerHTML = `<div class="empty"><b>${te("app.ev.no_event")}</b>${te("app.ev.no_event_hint")}<div class="acts"><button type="button" class="btn sm" id="ev-open-list">${icon("events")}${te("app.menu.show_events")}</button></div></div>`; $("ev-open-list").onclick = () => openPanel("events"); return; }
  el.innerHTML = renderEventCard(e, r);
  hydrateIcons(el);
  wireEventCard(e, r);
}
function eventSignals(e) {
  const src = e.source || {}; const lines = [];
  if (src.ticker) lines.push([t("app.sig.ticker"), src.ticker.join(" | "), 1]);
  if (src.feed) lines.push([t("app.sig.feed"), src.feed, 1]);
  if (src.money_delta != null) lines.push([t("app.sig.money"), (src.money_delta >= 0 ? "+$" : "-$") + fmtNum(Math.abs(Number(src.money_delta)))]);
  if (src.xp) lines.push([t("app.sig.xp"), src.xp]);
  if (src.distance_m) lines.push([t("app.sig.distance"), fmtUnit(Number(src.distance_m), "meter")]);
  if (src.dark_s != null) lines.push([t("app.sig.dark"), secTxt(src.dark_s)]);
  if (src.refined != null) lines.push([t("app.sig.refine"), t("app.sig.refine_v", { seconds: secTxt(src.refined) })]);
  if (src.voice) lines.push([t("app.sig.voice"), fmtList(src.voice)]);
  if (e.kind === "voice" && src.text) lines.push([t("app.sig.said"), src.text]);
  if (e.kind === "voice" && src.over_floor_db != null) lines.push([t("app.sig.level"), t("app.sig.level_v", { rms: fmtNum(Number(src.rms_db), { maximumFractionDigits: 0 }), over: fmtNum(Number(src.over_floor_db), { minimumFractionDigits: 1, maximumFractionDigits: 1 }), duration: secTxt(src.dur_s) })]);
  if (src.near_kind) lines.push([t("app.sig.near"), t("app.sig.near_v", { kind: KIND_LABEL[src.near_kind] || src.near_kind, time: tc(src.near_t) })]);
  if (src.learned && src.learned.factor != null && Math.abs(src.learned.factor - 1) > 0.005) lines.push([t("app.sig.learned"), t("app.sig.learned_v", { factor: fmtNum(Number(src.learned.factor), { minimumFractionDigits: 2, maximumFractionDigits: 2 }), sig: src.learned.sig })]);
  if (e.clip_in != null || e.clip_out != null) lines.push([t("app.sig.clip"), `${tc(e.clip_in ?? e.t_s)} - ${tc(e.clip_out ?? e.t_s)}`]);
  return lines;
}
function renderEventCard(e, r) {
  const src = e.source || {};
  const crops = [];
  if (e.crop_ticker) crops.push(`<figure data-src="/crops/${esc(e.crop_ticker)}?v=${e.created_utc}" title="${te("app.ev.full_size")}"><img src="/crops/${esc(e.crop_ticker)}?v=${e.created_utc}" alt="${te("app.ev.ticker_alt")}"><figcaption>${te("app.ev.ticker")}</figcaption></figure>`);
  if (e.crop_feed) crops.push(`<figure data-src="/crops/${esc(e.crop_feed)}?v=${e.created_utc}" title="${te("app.ev.full_size")}"><img src="/crops/${esc(e.crop_feed)}?v=${e.created_utc}" alt="${te("app.ev.feed_alt")}"><figcaption>${te("app.ev.feed")}</figcaption></figure>`);
  const cropHtml = crops.join("") || `<div class="nocrop">${icon(e.kind === "voice" ? "mic" : "mark")}<div style="margin-top:8px">${e.kind === "voice" ? te("app.ev.nocrop_voice") : e.kind === "manual" ? te("app.ev.nocrop_manual") : te("app.ev.nocrop")}</div></div>`;
  const sig = eventSignals(e);
  const near = ST.transcript.rec_id === r.id ? ST.transcript.segments.filter(s => s.t1 >= e.t_s - 15 && s.t0 <= e.t_s + 15) : null;
  const tx = near === null ? `<div class="muted t-s" style="padding:8px">${te("app.ev.tx_loading")}</div>` : near.length ? near.map(s => txLineHtml(s, r, Math.abs(s.t0 - e.t_s) <= 5)).join("")
    : `<div class="muted t-s" style="padding:8px">${te(ST.transcript.segments.length ? "app.ev.tx_none" : "app.ev.tx_none_untranscribed", { seconds: secTxt(15) })}</div>`;
  const stc = { kept: "ok", false: "bad" }[e.status] || "";
  return `<div class="evt">
    <div class="crops">${cropHtml}</div>
    <div class="evmain">
      <div class="evhead"><span class="kb k-${esc(e.kind)}">${kindLine(e)}</span><h3>${evTitle(e, { bare: true, nosub: true })}</h3>
        ${e.status !== "new" ? `<span class="badge ${stc}">${esc(I18N.has("app.status." + e.status) ? t("app.status." + e.status) : e.status)}</span>` : ""}
        ${e.resolve_marker ? `<span class="badge acc" title="${te("app.ev.in_resolve_title", { marker: e.resolve_marker })}">${te("app.ev.in_resolve")}</span>` : ""}</div>
      <div class="evmeta"><span>${tHtml("app.ev.at_html", { time: tc(e.t_s) })}</span><span>${esc(fmtWhen((recStart(r) || 0) + e.t_s))}</span>${e.amount ? `<span><b>+$${esc(fmtNum(Number(e.amount)))}</b></span>` : ""}${src.distance_m ? `<span><b>${esc(fmtUnit(Number(src.distance_m), "meter"))}</b></span>` : ""}${e.kind === "voice" && e.subkind !== "keyword" && src.dur_s != null ? `<span>${tHtml("app.ev.for_html", { duration: secTxt(src.dur_s, 1) })}</span>` : ""}</div>
      <div class="verdict">
        <button type="button" class="vbtn keep ${e.status === "kept" ? "on" : ""}" data-st="kept" data-tip="${te("app.ev.keep")}" data-k="Y">${icon("check")}${te("app.ev.keep")} <span class="kbd">Y</span></button>
        <button type="button" class="vbtn false ${e.status === "false" ? "on" : ""}" data-st="false" data-tip="${te("app.ev.false")}" data-k="X">${icon("x")}${te("app.ev.false")} <span class="kbd">X</span></button>
        ${e.status !== "new" ? `<button type="button" class="btn ghost sm" data-st="new" data-tip="${te("app.ev.back_new")}" data-k="U">${te("app.ev.reset")}</button>` : ""}
        <span class="sp"></span>
        <button type="button" class="btn sm" id="ev-resolve" title="${te("app.ev.marker_title")}">${icon("resolve")}${te("app.ev.marker")}</button>
        <button type="button" class="btn sm" id="ev-makeclip" title="${te("app.ev.make_clip_title")}">${icon("cut")}${te("app.ev.make_clip")}</button>
        <button type="button" class="ib sm" data-menu="event" data-tip="${te("app.ev.more_tip")}">${icon("more")}</button>
      </div>
      <div class="evmeters"><div class="evmeter"><div class="l"><span>${te("app.ev.score")}</span><b>${fmtNum(e.score, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</b></div><div class="b"><i style="width:${Math.min(100, e.score / 3 * 100).toFixed(1)}%"></i></div></div>
        <div class="evmeter"><div class="l"><span>${te("app.ev.confidence")}</span><b>${pct(e.confidence)}</b></div><div class="b"><i class="ok" style="width:${((e.confidence || 0) * 100).toFixed(1)}%"></i></div></div></div>
      <div class="field-l">${te("app.ev.note")} <span class="faint">${te("app.ev.note_hint")}</span></div>
      <input class="inp" type="text" id="ev-note" placeholder="${te("app.ev.note_ph")}" value="${esc(e.note || "")}">
      <details class="disc-d" id="ev-sig" ${ST.sigOpen ? "open" : ""}><summary>${icon("chev", "chev")}${te("app.ev.signals")} <span class="faint">${sig.length}</span></summary>
        ${sig.length ? `<dl class="kv">${sig.map(([k, v, m]) => `<dt>${esc(k)}</dt><dd class="num">${esc(v)}</dd>`).join("")}</dl>` : `<div class="muted t-s" style="padding:0 12px 12px">${te("app.ev.no_signals")}</div>`}</details>
    </div>
    <div class="evtx"><div class="colh">${icon("tx")}${te("app.ev.said_around")} <span class="faint" style="font-weight:400">±${esc(secTxt(15))}</span></div><div id="ev-tx">${tx}</div></div>
  </div>`;
}
function txLineHtml(s, r, near) {
  const who = s.speaker === "me" ? te("app.tx.you") : s.speaker === "squad" ? te("app.tx.squad") : "";
  return `<div class="tline ${near ? "near" : ""} ${s.speaker === "me" ? "me" : ""}" data-t="${s.t0}" title="${te("app.tx.line_title", { when: fmtDate((recStart(r) || 0) + s.t0) })}"><span class="tc ${s.avg_logprob != null && s.avg_logprob < -0.8 ? "lo" : ""}">${tc(s.t0)}</span><span>${who ? `<span class="who">${who}</span>` : ""}${esc(s.text)}</span></div>`;
}
let _noteT = null;
function wireEventCard(e, r) {
  const el = $("cp-event");
  el.querySelectorAll(".verdict [data-st]").forEach(b => b.onclick = () => setEventStatus(e.id, b.dataset.st));
  $("ev-resolve").onclick = () => handoff("markers", { event_ids: [e.id], rec_ids: undefined, status: "any", min_score: undefined, kinds: undefined });
  if ($("ev-makeclip")) $("ev-makeclip").onclick = () => window.MC && MC.make({ event: e });
  el.querySelectorAll(".crops figure[data-src]").forEach(f => f.onclick = () => openLightbox(f.dataset.src));
  el.querySelectorAll("#ev-tx .tline[data-t]").forEach(l => l.onclick = (ev) => { if (ev.altKey) copyText(tc(+l.dataset.t)); else if (window.PL) PL.seekAbs(r.id, Math.max(0, +l.dataset.t - 0.5)); });
  $("ev-sig").ontoggle = () => { ST.sigOpen = $("ev-sig").open; saveShell(); };
  const note = $("ev-note");
  const saveNote = async (quiet) => {
    clearTimeout(_noteT);
    if (note.value === (e.note || "")) return;
    try { const u = await post(`/api/events/${e.id}`, { note: note.value }); Object.assign(e, u); if (!quiet) toast(t("app.toast.note_saved")); } catch (err) { toast(err.message, 5000); }
  };
  note.oninput = () => { clearTimeout(_noteT); _noteT = setTimeout(() => saveNote(true), 800); };
  note.onchange = () => saveNote(false);
  note.onkeydown = (ev) => { if (ev.key === "Enter") note.blur(); };
  // one attempt per recording: a failed load must not loop renderDetail -> loadTranscript -> renderDetail
  if (ST.transcript.rec_id !== r.id && ST.transcript.failed !== r.id) loadTranscript(r.id).then(ok => { if (ok && ST.event && ST.event.id === e.id) renderEventPane(); });
}
async function deleteManual(e) {
  try { await del(`/api/events/${e.id}`); ST.event = null; if (window.PL) PL.clearEvent(); await loadEvents(e.rec_id); renderStage(); renderDetail(); toast(t("app.toast.manual_deleted")); } catch (err) { toast(err.message, 5000); }
}

function renderRecPane() {
  const el = $("cp-rec"); if (ST.view !== "normal") return;
  if (deferIfEditing(el, renderRecPane)) return;
  const r = ST.detail;
  if (!r) { el.innerHTML = `<div class="empty"><b>${te("app.ev.no_rec")}</b>${te("app.rp.no_rec_hint")}</div>`; return; }
  const start = recStart(r);
  const st = r.stages || {}; const jp = ST.jobprog[r.id];
  const ROLE = { mic: "acc", voip: "acc", voice: "acc", game: "ok", desktop: "", music: "", silent: "", dup: "", other: "warn", unknown: "" };
  const roleName = (role) => I18N.has("app.role." + role) ? t("app.role." + role) : role;
  // a stream VOD's one track is the streamer's mix: the OBS track names don't apply (audio.obs_layout)
  const names = r.kind === "vod" ? [] : ((ST.settings && ST.settings.audio && ST.settings.audio.track_names) || []);
  const tr = (st.transcribe && st.transcribe.tracks) || {};
  const tracks = r.tracks && r.tracks.length ? `<table class="t trk"><tr><th>${te("app.rp.th_track")}</th><th>${te("app.rp.th_role")}</th><th>${te("app.rp.th_speech")}</th><th>${te("app.rp.th_level")}</th><th>${te("app.rp.th_lines")}</th></tr>` +
    r.tracks.map(tk => `<tr title="${esc([names[tk.idx] ? t("app.rp.obs_name", { name: names[tk.idx] }) : "", tk.codec, tk.channels ? t("app.rp.channels", { count: tk.channels }) : ""].filter(Boolean).join(" · "))}"><td>A${tk.idx + 1}${names[tk.idx] ? ` <span class="muted">${esc(names[tk.idx])}</span>` : ""}</td><td><span class="badge ${ROLE[tk.role] || ""}">${esc(roleName(tk.role))}</span></td><td>${tk.speech_share == null ? "-" : pct(tk.speech_share)}</td><td class="nw">${tk.rms_mean_db == null ? "-" : fmtNum(tk.rms_mean_db, { maximumFractionDigits: 0 }) + " dB"}</td><td>${tr[tk.idx] ? fmtNum(tr[tk.idx].lines) : "-"}</td></tr>`).join("") + `</table>` : `<div class="muted t-s">${te("app.rp.no_tracks")}</div>`;
  // pipeline: each stage shows its result, or the live state (queued / running % / failed) from the job lanes
  const queued = (stage) => (r.jobs || []).find(j => j.stage === stage && (j.state === "queued" || j.state === "running"));
  const ts = r.transcript || { n: 0 };
  const ev = r.event_counts || {};
  const stg = (stage, label, val) => {
    const s = st[stage]; const live = jp && jp.stage === stage; const q = queued(stage);
    let dot = s ? (s.state === "done" ? "ok" : s.state === "failed" ? "bad" : "") : "";
    let v = s ? (s.state === "done" ? (val || te("app.rp.done")) : esc(stateName(s.state))) : te("app.rp.never");
    let cls = s && s.state === "failed" ? "bad" : "";
    if (live) { dot = "acc pulse"; v = `${esc(srvF(jp, "message") || t("app.rp.working"))} ${pct(jp.progress)}`; cls = ""; }
    else if (q) { dot = "acc"; v = `${esc(stateName(q.state))} <span class="faint">#${q.id}</span>`; cls = ""; }
    return `<div class="stg" title="${s ? esc(fmtDate(s.utc)) + (s.note ? " - " + esc(srvF(s, "note")) : "") : ""}"><div class="n"><span class="sd ${dot}"></span>${label}</div><div class="v ${cls}" id="stg-v-${stage}">${v}</div>${live ? `<div class="pg"><i id="stg-pg-${stage}" style="width:${pct(jp.progress)}"></i></div>` : ""}</div>`;
  };
  const evTxt = Object.entries(ev).map(([k, n]) => t("app.rp.kind_count", { kind: KIND_LABEL[k] || k, count: n })).join(", ");
  const pipeline = stg("probe", te("app.rp.probe"), r.width ? `${r.width}×${r.height} ${r.fps ? te("app.info.fps", { fps: fmtNum(Math.round(r.fps)) }) : ""}` : "") +
    stg("transcribe", te("app.rp.transcribe"), ts.n ? te("app.rp.lines_model", { lines: t("app.cell.lines", { count: ts.n }), model: ts.model || "" }) : esc(srvF(st.transcribe, "note") || t("app.info.no_speech"))) +
    stg("detect", te("app.rp.detect"), evTxt ? esc(evTxt) : te("app.rp.nothing_found")) +
    stg("proxy", te("app.rp.proxy"), r.proxy_path ? te("app.rp.built") : te("app.rp.done"));
  const jobs = (r.jobs || []).slice(0, 6).map(j => `<div class="jobline"><span class="badge ${j.state === "done" ? "ok" : j.state === "failed" ? "bad" : j.state === "running" ? "acc" : ""}">${esc(stateName(j.state))}</span><span class="stage">${esc(stageName(j.stage))}</span><span class="faint">#${j.id}</span><span class="faint t-xs" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(srvF(j, "error") || srvF(j, "message"))}">${esc(srvF(j, "error") || srvF(j, "message"))}</span></div>`).join("") || `<div class="muted t-s">${te("app.rp.no_jobs")}</div>`;
  const tsum = ts.n ? `${te("app.rp.tsum", { segments: fmtNum(ts.n), speech: fmtDur(ts.speech_s), model: ts.model || "" })} <span class="muted">${esc(fmtAgo(ts.utc))}</span>` :
    (st.transcribe ? `<span class="muted">${esc(srvF(st.transcribe, "note") || t("app.rp.no_speech_found"))}</span>` : `<span class="muted">${te("app.info.not_transcribed")}</span>`);
  // the essentials up front; the file / codec facts, the roles explanation and the job history behind disclosures
  el.innerHTML = `<div class="rgrid"><div>
      <div class="pipeline">${pipeline}</div>
      <div class="qm-recact"><button type="button" class="btn sm" id="d-qtop" title="${te("app.rp.qtop_title")}">${icon("qtop")}${te("app.menu.queue_top")}</button></div>
      <dl class="kv">
        <dt>${r.kind === "replay" ? te("app.info.replay_saved") : te("app.info.recorded")}</dt><dd>${fmtDate(r.name_utc)}${r.kind === "replay" && start ? ` <span class="muted">${te("app.rp.buffer_starts", { date: fmtDate(start) })}</span>` : ""}</dd>
        <dt>${te("app.info.duration")}</dt><dd>${fmtDur(r.duration_s)} <span class="muted">${te("app.rp.size_on", { size: fmtBytes(r.size), drive: r.drive })}</span></dd>
        <dt>${te("app.info.status")}</dt><dd>${recBadges(r)}</dd>
        <dt>${te("app.info.transcript")}</dt><dd>${tsum}</dd>
        <dt>${te("app.rp.zones")}</dt><dd>${r.zone_template_id ? te("app.rp.template", { id: r.zone_template_id }) : esc(r.hud_profile || t("app.rp.builtin"))} · <button type="button" class="lnk" id="d-zones">${te("app.rp.edit")}</button></dd>
        ${r.kind === "vod" && r.source_url ? `<dt>${te("app.info.source")}</dt><dd><a href="${esc(r.source_url)}" target="_blank" rel="noopener">${esc(r.channel || r.source_url)}</a></dd>` : ""}
        ${r.error ? `<dt>${te("app.rp.error")}</dt><dd style="color:var(--bad-text)">${esc(srvF(r, "error"))}</dd>` : ""}
      </dl>
      <details class="disc-d" id="rec-det" ${ST.recDetOpen ? "open" : ""}><summary>${icon("chev", "chev")}${te("app.rp.details")} <span class="faint">${te("app.rp.details_sub")}</span></summary>
        <dl class="kv">
          <dt>${te("app.info.file")}</dt><dd class="path">${esc(r.path)}</dd>
          <dt>${te("app.info.video")}</dt><dd>${r.width ? `${r.width}×${r.height} · ${esc(r.vcodec)} ${esc(r.pix_fmt || "")} · ${r.fps ? te("app.info.fps", { fps: fmtNum(r.fps, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) }) : ""} · ${fmtMbps(r.bitrate)}` : "-"}</dd>
          <dt>${te("app.rp.keyint")}</dt><dd>${r.keyint_s ? esc(secTxt(r.keyint_s)) : "-"}</dd>
          <dt>${te("app.rp.file_modified")}</dt><dd>${fmtDate(r.mtime)}</dd>
        </dl></details></div>
    <div>
      <div class="colh">${te("app.rp.tracks")} <span class="infoi" data-tip="${te("app.rp.tracks_tip")}">${icon("info")}</span></div>${tracks}
      <details class="disc-d" id="rec-jobs" ${ST.recJobsOpen ? "open" : ""}><summary>${icon("chev", "chev")}${te("app.rp.recent_jobs")} <span class="faint">${(r.jobs || []).length}</span></summary><div class="jobs-in">${jobs}</div></details>
      <div class="colh" style="margin-top:16px">${te("app.rp.handoff")}</div>
      <div class="muted t-s" style="margin-bottom:8px">${te("app.rp.handoff_hint")}</div>
      <button type="button" class="btn" id="d-send">${icon("send")}${te("app.resolve.send")}</button></div></div>`;
  $("d-send").onclick = (e) => { e.stopPropagation(); openHandoff($("d-send")); };
  $("d-zones").onclick = () => window.ZN && ZN.open({ rec_id: r.id });
  $("d-qtop").onclick = () => window.QM && QM.recToTop(r.id);
  $("rec-det").ontoggle = () => { ST.recDetOpen = $("rec-det").open; };
  $("rec-jobs").ontoggle = () => { ST.recJobsOpen = $("rec-jobs").open; };
}

async function detectRec(id) {
  try { const r = await post(`/api/recordings/${id}/detect`); toast(r.already_queued ? t("app.toast.already_queued") : t("app.toast.detect_queued_one")); if (ST.tab === "events") renderEvents(); }
  catch (err) { toast(err.message, 5000); }
}
async function transcribeRec(id) {
  try { const r = await post(`/api/recordings/${id}/transcribe`); toast(r.already_queued ? t("app.toast.already_queued") : t("app.toast.tx_queued")); if (ST.ctab === "tx") renderTranscript(); }
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
// a subkind in the UI language (detect's own names); a ticker label the game printed stays as read, prettified
const subName = (s) => I18N.has("app.subkind." + s) ? t("app.subkind." + s) : subLabel(s);
function kindLine(e) { return esc(KIND_NAME[e.kind] || e.kind) + (e.subkind && !titleHasSub(e) ? " · " + esc(I18N.has("app.subkind." + e.subkind) ? t("app.subkind." + e.subkind) : e.subkind) : ""); }
function evTitle(e, o) {
  o = o || {};
  const d = !o.bare && e.source && e.source.distance_m ? ` <small>[${esc(fmtUnit(Number(e.source.distance_m), "meter"))}]</small>` : "";
  const amt = !o.bare && e.amount ? ` <small>+$${esc(fmtNum(Number(e.amount)))}</small>` : "";
  if (e.kind === "kill") return `${esc(e.victim || KIND_LABEL.kill)}${d}${amt}${e.subkind && !o.nosub ? ` <small>${esc(subName(e.subkind))}</small>` : ""}`;
  if (e.kind === "death") return `${te("app.ev.killed_by", { name: e.killer || "?" })}${d}`;
  if (e.kind === "squad") return `${esc(e.killer || "")} &rarr; ${esc(e.victim || "")}${d}`;
  if (e.kind === "manual") return esc(e.note || t("app.ev.manual_mark"));
  if (e.kind === "voice") {
    const s = e.source || {};
    if (e.subkind === "keyword") return `"${esc((s.voice || []).join(", "))}" <small>${esc((s.text || "").slice(0, 80))}</small>`;
    return `${te("app.ev.excitement", { db: s.over_floor_db != null ? fmtNum(Number(s.over_floor_db), { minimumFractionDigits: 1, maximumFractionDigits: 1 }) : "?" })}${s.near_kind ? ` <small>${te("app.ev.near_kind", { kind: KIND_LABEL[s.near_kind] || s.near_kind })}</small>` : ""}`;
  }
  return `${esc(e.subkind ? subName(e.subkind) : (KIND_NAME[e.kind] || e.kind))}${amt}`;
}
function kindBadge(k) { return `<span class="kb k-${esc(k)}">${esc(KIND_NAME[k] || k)}</span>`; }
const scoreCell = (e) => `<div class="score">${e.status === "kept" ? `<span class="kept" title="${te("app.ev.kept_title")}">${icon("check")}</span>` : ""}${fmtNum(e.score, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}<i><b style="width:${Math.min(100, e.score / 3 * 100).toFixed(1)}%"></b></i></div>`;

function renderEvents() {
  const el = $("tab-events"); const r = ST.detail;
  const head = (n, acts) => `<div class="phd"><h2>${te("app.evs.title")}</h2>${n != null ? `<span class="cnt">${fmtNum(n)}</span>` : ""}<span class="sp"></span>${kindsBtn()}${acts || ""}<button type="button" class="ib sm" data-menu="events" data-tip="${te("app.evs.more_tip")}">${icon("more")}</button></div>`;
  if (!r) { el.innerHTML = head(null) + `<div class="pbody"><div class="empty"><b>${te("app.ev.no_rec")}</b>${te("app.evs.no_rec_hint")}</div></div>`; return; }
  if (ST.events.rec_id !== r.id) { el.innerHTML = head(null) + `<div class="pbody"><div class="empty">${te("app.evs.loading")}</div></div>`; loadEvents(r.id); return; }
  const jp = ST.jobprog[r.id]; const queued = r.jobs && r.jobs.some(j => j.stage === "detect" && j.state === "queued");
  const all = ST.events.list; const counts = {};
  for (const e of all) if (e.status !== "false") counts[e.kind] = (counts[e.kind] || 0) + 1;
  const list = all.filter(e => ST.chips[e.kind] !== false);
  const detecting = jp && jp.stage === "detect";
  let html = head(all.length, `<button type="button" class="btn sm" id="e-run" ${detecting ? "disabled" : ""} title="${r.stages && r.stages.detect ? te("app.evs.redetect_title") : te("app.evs.detect_title")}">${icon("target")}${r.stages && r.stages.detect ? te("app.act.redetect") : te("app.act.detect")}</button>`);
  html += `<div class="psub"><span class="faint t-xs" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:100%" title="${esc(r.name)}">${esc(recLabel(r))}</span></div>`;
  if (detecting) html += `<div class="psub"><span class="badge acc" id="ev-prog">${esc(srvF(jp, "message") || t("app.rp.working"))} ${pct(jp.progress)}</span><div class="pg"><i id="ev-progbar" style="width:${pct(jp.progress)}"></i></div></div>`;
  else if (queued) html += `<div class="psub"><span class="badge">${te("app.evs.detect_queued")}</span><button type="button" class="btn ghost sm" id="e-q">${te("app.evs.queue")}</button></div>`;
  html += `<div class="pbody" id="evlist">`;
  if (!all.length) html += `<div class="empty"><b>${detecting ? te("app.evs.detecting") : r.stages && r.stages.detect ? te("app.evs.nothing") : te("app.evs.not_yet")}</b>${te("app.evs.detect_hint")}</div>`;
  else if (!list.length) html += `<div class="empty"><b>${te("app.evs.all_hidden", { count: all.length })}</b>${te("app.evs.all_hidden_hint")}<div class="acts"><button type="button" class="btn sm" id="e-showall">${te("app.evs.show_all")}</button></div></div>`;
  for (const e of list) html += `<div class="ev k-${esc(e.kind)} ${ST.event && e.id === ST.event.id ? "cur" : ""} ${e.status === "false" ? "false" : ""}" data-id="${e.id}" id="ev-${e.id}" title="${esc(I18N.has("app.status." + e.status) ? t("app.status." + e.status) : e.status)}${e.note ? " - " + esc(e.note) : ""}">
      <span class="kbar"></span><span class="tc">${tc(e.t_s)}</span>
      <div class="ttl"><div>${evTitle(e, { nosub: true })}</div><div><span class="kn">${kindLine(e)}</span>${e.resolve_marker ? " " + te("app.evs.in_resolve") : ""}${e.note ? " \u00b7 " + esc(e.note) : ""}</div></div>
      <div class="rgt">${scoreCell(e)}<span class="conf" title="${te("app.evs.conf_title")}">${te("app.evs.conf", { pct: pct(e.confidence) })}</span></div></div>`;
  html += `</div><div class="pfoot">${KINDS.filter(k => counts[k]).map(k => esc(t("app.rp.kind_count", { kind: KIND_LABEL[k], count: counts[k] }))).join(" · ") || te("app.evs.none")}</div>`;
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
    const label = status === "false" ? t("app.verdict.false") : status === "new" ? t("app.verdict.reset") : status === "kept" ? t("app.verdict.kept") : status;
    toast(u.rescored ? t("app.verdict.rescored", { label, count: u.rescored }) : label, u.rescored ? 3500 : 1600);
    const i = ST.events.list.findIndex(x => x.id === id); if (i >= 0) ST.events.list[i] = Object.assign(ST.events.list[i], u);
    if (ST.event && ST.event.id === id) ST.event = ST.events.list[i];
    const f = ST.found.events.find(x => x.id === id); if (f) { f.status = u.status; f.score = u.score != null ? u.score : f.score; renderClips(); }
    renderEvents(); renderDetail(); if (window.PL) PL.renderTimeline(); loadRecs();
  } catch (err) { toast(err.message, 5000); }
}
function verdictKey(status) {
  if (!ST.event) { toast(t("app.verdict.no_event")); return; }
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
  const frame = rid ? `<img src="/api/recordings/${rid}/frame?t=${c.t}&w=1200" alt="${te("app.cal.frame_alt")}">` : `<div class="empty">${te("app.cal.pick_rec")}</div>`;
  const tickerRows = c.ticker.map((g) => `<tr><td class="mono" title="${esc(g.examples.join(" / "))}">${esc(g.pattern)}</td><td>${g.n}</td><td>${esc([...g.cash.map(x => "$" + x), ...g.xp.map(x => x + "XP")].join(" "))}</td><td><select class="inp sm" data-pat="${esc(g.pattern)}">${c.known_labels.map(l => `<option ${l === g.label ? "selected" : ""}>${l}</option>`).join("")}</select></td></tr>`).join("");
  const nameRows = c.names.map(n => `<tr><td class="mono">${esc(n.name)}</td><td>${n.n}</td><td>${n.as_killer} / ${n.as_victim}</td><td><button type="button" class="btn sm" data-alias="${esc(n.name)}" data-as="player">${te("app.cal.me")}</button> <button type="button" class="btn sm" data-alias="${esc(n.name)}" data-as="squad">${te("app.cal.squad")}</button></td></tr>`).join("");
  const rules = c.labels.map(l => `<div class="field"><label class="mono">${esc(l.pattern)} &rarr; ${esc(l.label)}</label><button type="button" class="btn ghost sm" data-del="${l.id}">${te("app.cal.remove")}</button></div>`).join("") || `<div class="hint">${te("app.cal.rules_none", { words: "KILL, KILL ASSIST, SPOTTED TARGET DESTROYED, REVIVED, CONTROL ZONE, SUPPLIES, TIP" })}</div>`;
  el.innerHTML = `<div class="vh"><h2>${te("app.cal.title")}</h2><span class="muted t-s">${esc(c.profile)} ${c.width}×${c.height} ${rid ? te("app.cal.this_rec") : te("app.cal.every_rec")}${c.zones_source ? " " + te("app.cal.boxes", { source: c.zones_source }) : ""}</span><span class="sp"></span>
      <button type="button" class="btn sm" id="cal-back" data-tip="${te("app.cal.back_tip")}" data-k="Esc">${icon("x")}${te("app.cal.close")}</button></div>
    <div class="vbody"><div class="cal"><div>
      <div class="ctl">${te("app.cal.frame_at")} <input class="inp" type="number" id="cal-t" step="1" min="0" value="${c.t}"> ${te("app.cal.seconds_unit")} <button type="button" class="btn sm" id="cal-grab">${te("app.cal.grab")}</button>${rid ? `<button type="button" class="btn sm" id="cal-detect">${icon("target")}${te("app.cal.run_detect")}</button>` : ""}<span class="muted t-xs">${te("app.cal.aliases", { aliases: c.aliases.join(", ") || "-", squad: c.squad.join(", ") || "-" })}</span></div>
      <div class="calframe" id="cal-frame">${frame}${rid ? roi("ticker", "ticker") + roi("feed", "feed") + `<div class="money" style="left:${pc(tk.x, c.width)};top:${pc(tk.y + c.money_y, c.height)};width:${pc(tk.w, c.width)}"></div>` : ""}</div>
      <div class="hint">${te("app.cal.hint")}</div>
    </div><div>
      <div class="card"><h4>${te("app.cal.ticker_seen")}<span class="sp"></span><span class="faint">${te("app.cal.sightings", { count: c.ticker.reduce((a, g) => a + g.n, 0) })}</span></h4>
        ${c.ticker.length ? `<table class="t"><tr><th>${te("app.cal.th_text")}</th><th>${te("app.cal.th_n")}</th><th>${te("app.cal.th_amounts")}</th><th>${te("app.cal.th_label")}</th></tr>${tickerRows}</table>` : `<div class="hint">${te("app.cal.run_detect_first")}</div>`}</div>
      <div class="card"><h4>${te("app.cal.names_seen")} <span class="faint">${te("app.cal.names_sub")}</span></h4>
        ${c.names.length ? `<table class="t"><tr><th>${te("app.cal.th_name")}</th><th>${te("app.cal.th_n")}</th><th>${te("app.cal.th_killer_victim")}</th><th></th></tr>${nameRows}</table>` : `<div class="hint">${te("app.cal.none_yet")}</div>`}</div>
      <div class="card"><h4>${te("app.cal.your_rules")}</h4>${rules}</div>
      <div class="card learn" id="cal-learn"><h4>${te("app.cal.learning")}<span class="sp"></span><button type="button" class="btn sm" id="cal-rescore">${te("app.cal.rescore")}</button></h4><div class="hint">${te("app.cal.loading")}</div></div>
    </div></div></div>`;
  hydrateIcons(el);
  $("cal-back").onclick = closeCalibration;
  $("cal-grab").onclick = () => { c.t = parseFloat($("cal-t").value) || 0; renderCalibration(); };
  $("cal-t").onkeydown = (e) => { if (e.key === "Enter") $("cal-grab").click(); };
  if ($("cal-detect")) $("cal-detect").onclick = () => detectRec(rid);
  el.querySelectorAll("select[data-pat]").forEach(s => s.onchange = async () => {
    try { const r = await post("/api/calibration/labels", { text: s.dataset.pat, label: s.value }); toast(t("app.cal.rule_saved", { count: r.relabelled })); openCalibration(rid); }
    catch (err) { toast(err.message, 5000); }
  });
  el.querySelectorAll("button[data-alias]").forEach(b => b.onclick = async () => {
    try { const r = await post("/api/calibration/alias", { name: b.dataset.alias, as: b.dataset.as }); ST.settings.player.aliases = r.aliases; ST.settings.player.squad = r.squad; toast(t("app.cal.alias_saved")); openCalibration(rid); }
    catch (err) { toast(err.message, 5000); }
  });
  el.querySelectorAll("button[data-del]").forEach(b => b.onclick = async () => {
    try { await del(`/api/calibration/labels/${b.dataset.del}`); openCalibration(rid); } catch (err) { toast(err.message, 5000); }
  });
  $("cal-rescore").onclick = () => post("/api/learning/rescore").then(x => { toast(t("app.cal.rescored", { rescored: fmtNum(x.rescored), examined: fmtNum(x.examined) })); renderLearning(rid); }).catch(err => toast(err.message, 5000));
  renderLearning(rid);
}
// learning: per-signature kept / false counts and the factor they give look-alike events
async function renderLearning(rid) {
  const box = $("cal-learn"); if (!box) return;
  let L; try { L = await api("/api/learning"); } catch (err) { const h = box.querySelector(".hint"); if (h) h.textContent = err.message; return; }
  if (!$("cal-learn")) return;
  const rows = (L.signatures || []).map(s => `<tr title="${esc(s.sig)}"><td>${esc(KIND_NAME[s.kind] || s.kind)}${s.subkind ? " / " + esc(subName(s.subkind)) : ""}</td><td class="mono" style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(s.sig)}</td><td>${s.kept}</td><td>${s.false}</td><td style="color:${s.active ? (s.factor < 1 ? "var(--bad)" : "var(--ok)") : "var(--muted)"}">x${fmtNum(Number(s.factor), { minimumFractionDigits: 2, maximumFractionDigits: 2 })}${s.active ? "" : " " + te("app.cal.min_n", { n: L.params ? L.params.min_n : 3 })}</td><td>${s.n_new_affected}</td></tr>`).join("");
  const sug = (L.suggestions || []).map((s, i) => `<div class="field"><label class="mono" title="${esc((s.examples || []).join(" / "))}">${esc(s.message || s.pattern)}</label><button type="button" class="btn sm" data-sug="${i}">${te("app.cal.label_other")}</button></div>`).join("");
  box.innerHTML = `<h4>${te("app.cal.learning")}<span class="sp"></span><button type="button" class="btn sm" id="cal-rescore">${te("app.cal.rescore")}</button></h4>` +
    (rows ? `<table class="t"><tr><th>${te("app.cal.th_kind")}</th><th>${te("app.cal.th_signals")}</th><th>${te("app.cal.th_kept")}</th><th>${te("app.cal.th_false")}</th><th>${te("app.cal.th_factor")}</th><th>${te("app.cal.th_affected")}</th></tr>${rows}</table>` : `<div class="hint">${te("app.cal.no_verdicts")}</div>`) +
    (sug ? `<div class="colh" style="margin-top:12px">${te("app.cal.noise")}</div>${sug}` : "");
  $("cal-rescore").onclick = () => post("/api/learning/rescore").then(x => { toast(t("app.cal.rescored", { rescored: fmtNum(x.rescored), examined: fmtNum(x.examined) })); renderLearning(rid); }).catch(err => toast(err.message, 5000));
  box.querySelectorAll("button[data-sug]").forEach(b => b.onclick = async () => {
    const s = L.suggestions[+b.dataset.sug];
    try { const r = await post("/api/calibration/labels", { text: s.pattern, label: s.suggest_label || "other" }); toast(t("app.cal.rule_saved", { count: r.relabelled })); openCalibration(rid); } catch (err) { toast(err.message, 5000); }
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
  m.innerHTML = `<button type="button" class="mi" id="gs-recs">${icon("rec")}<span>${n == null ? te("app.gs.filtering", { q: v }) : te("app.gs.named", { count: n, q: v })}<small>${te("app.gs.named_sub")}</small></span></button>
    <button type="button" class="mi act" id="gs-said">${icon("tx")}<span>${te("app.gs.said", { q: v })}<small>${te("app.gs.said_sub")}</small></span><span class="k">Enter</span></button>`;
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
  setStatus(t("app.search.status", { q, count: ST.search.hits.length }));
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
  let html = `<div class="phd"><h2>${te("app.said.title")}</h2><span class="sp"></span><button type="button" class="ib sm" data-menu="library" data-tip="${te("app.said.lib_tip")}">${icon("more")}</button></div>`;
  if (S.q && S.q.length >= 2) {
    const hits = S.hits; const recs = new Set(hits.map(h => h.rec_id));
    html += `<div class="psub t-s muted"><span>\u201c<b style="color:var(--text)">${esc(S.q)}</b>\u201d · ${S.pending && !hits.length ? te("app.said.searching") : te("app.said.hits", { count: hits.length, recordings: t("app.rec.count", { count: recs.size }) })}</span>
      ${S.fts ? `<span class="badge" title="${te("app.said.ranked_title")}">${te("app.said.ranked")}</span>` : `<span class="badge warn" title="${te("app.said.like_title")}">${te("app.said.like")}</span>`}
      <span class="sp"></span><button type="button" class="btn ghost sm" id="t-clear">${te("app.said.clear")}</button></div><div class="pbody">`;
    if (!hits.length) html += `<div class="empty">${S.pending ? `<div class="spin"></div>` : `<b>${te("app.said.none")}</b>${te("app.said.none_hint")}`}</div>`;
    for (const h of hits) html += `<div class="hit" data-rec="${h.rec_id}" data-seg="${h.seg_id}"><div class="where"><span class="tc">${tc(h.t0)}</span><b title="${esc(h.rec_name)}">${esc(recLabelFor(h.rec_id, h.rec_name))}</b><span>${esc(h.drive || "")}</span></div><div class="snip">${window.SL ? SL.hit(h) : ""}${markText(h.snippet || h.text, S.q)}</div></div>`;
    html += `</div>`;
  } else {
    html += `<div class="pbody"><div class="empty"><b>${te("app.said.empty")}</b>${te("app.said.empty_hint")}<div class="acts"><button type="button" class="btn sm" id="t-focus">${icon("search")}${te("app.said.search")} <span class="kbd">/</span></button></div></div></div>`;
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
  const r = ST.detail; const tx = ST.transcript;
  renderCtxExtra();
  if (!r) { el.innerHTML = `<div class="empty"><b>${te("app.ev.no_rec")}</b>${te("app.tx.no_rec_hint")}</div>`; return; }
  if (tx.rec_id !== r.id) { el.innerHTML = `<div class="empty">${te("app.evs.loading")}</div>`; if (tx.failed !== r.id) loadTranscript(r.id); return; }
  if (deferIfEditing(el, () => renderTranscript())) return;
  const jp = ST.jobprog[r.id]; const queued = r.jobs && r.jobs.some(j => j.stage === "transcribe" && j.state === "queued");
  const sum = tx.summary || { n: 0 };
  let html = "";
  if (jp && jp.stage === "transcribe") html += `<div class="sline"><span class="sd acc pulse"></span><span id="tx-prog">${esc(srvF(jp, "message") || t("app.rp.working"))} ${pct(jp.progress)}</span><div class="pg" style="flex:1"><i id="tx-progbar" style="width:${pct(jp.progress)}"></i></div></div>`;
  else if (queued) html += `<div class="sline"><span class="sd acc"></span><span>${ST.counts && ST.counts.paused ? te("app.tx.queued_paused") : te("app.tx.queued")}</span><span class="sp"></span><button type="button" class="btn ghost sm" id="tx-q">${te("app.evs.queue")}</button></div>`;
  html += `<div class="txhead">` + (sum.n ? `<span>${te("app.tx.head", { lines: t("app.cell.lines", { count: sum.n }), speech: fmtDur(sum.speech_s) })} <span title="${esc(fmtDate(sum.utc))}">${esc(sum.model || "")}</span>${window.SL ? SL.head(sum) : ""}</span>` :
    (r.stages && r.stages.transcribe ? `<span class="badge warn">${esc(srvF(r.stages.transcribe, "note") || t("app.info.no_speech"))}</span>` : `<span>${te("app.info.not_transcribed")}</span>`));
  if (sum.n) html += `<span class="sp"></span><span class="search-inp">${icon("search")}<input class="inp sm" type="text" id="t-filter" placeholder="${te("app.tx.filter_ph")}" value="${esc(tx.filter)}"></span>`;
  html += `</div><div id="tx-lines"></div>`;
  el.innerHTML = html;
  const q = $("tx-q"); if (q) q.onclick = () => openPanel("jobs");
  renderTxLines();
  const fi = $("t-filter"); if (fi) { let ft = null; fi.oninput = () => { clearTimeout(ft); ft = setTimeout(() => { ST.transcript.filter = fi.value; renderTxLines(); }, 200); }; }
  if (scrollSeg) { const s = $("seg-" + scrollSeg); if (s) { s.scrollIntoView({ block: "center" }); s.classList.add("flash"); } }
}
function renderTxLines() {
  const box = $("tx-lines"); const r = ST.detail; const tx = ST.transcript; if (!box || !r) return;
  const jp = ST.jobprog[r.id];
  const f = (tx.filter || "").toLowerCase();
  const lines = tx.segments.filter(s => !f || s.text.toLowerCase().includes(f) || (s.text_en || "").toLowerCase().includes(f));
  if (!tx.segments.length) { box.innerHTML = `<div class="empty"><b>${jp && jp.stage === "transcribe" ? te("app.tx.transcribing") : te("app.tx.none")}</b>${te("app.tx.none_hint", { model: ST.settings ? ST.settings.stt.model : "" })}</div>`; return; }
  if (!lines.length) { box.innerHTML = `<div class="empty">${te("app.tx.no_match")}</div>`; return; }
  const near = ST.event && ST.event.rec_id === r.id ? ST.event.t_s : null;
  box.innerHTML = `<div class="txcols">` + lines.map(s => {
    const who = s.speaker === "me" ? te("app.tx.you") : s.speaker === "squad" ? te("app.tx.squad") : "";
    return `<div class="tline ${s.id === tx.cur ? "cur" : ""} ${near != null && Math.abs(s.t0 - near) <= 5 ? "near" : ""} ${s.speaker === "me" ? "me" : ""}" data-seg="${s.id}" data-t="${s.t0}" id="seg-${s.id}" title="${te("app.tx.line_title", { when: fmtDate((recStart(r) || 0) + s.t0) })}"><span class="tc ${s.avg_logprob != null && s.avg_logprob < -0.8 ? "lo" : ""}" title="${te("app.tx.conf_title", { value: s.avg_logprob == null ? "-" : fmtNum(s.avg_logprob) })}">${tc(s.t0)}</span><span>${who ? `<span class="who">${who}</span>` : ""}${window.SL ? SL.line(s, tx.summary) : ""}${f ? markText(s.text, f) : esc(s.text)}${window.SL ? SL.lineEnglish(s) : ""}</span></div>`;
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
  $("f-nset").outerHTML = n ? `<span id="f-nset" class="badge acc">${te("app.find.n_set", { count: n })}</span>` : `<span id="f-nset" class="faint">${te("app.find.defaults")}</span>`;
  $("f-reset").hidden = !n;
  $("f-drive").textContent = ST.drive ? ST.drive : t("app.find.all_drives");
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
    finderChanged(); toast(t("app.find.reset_toast"));
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
  $("f-count").textContent = t("app.find.searching");
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
  $("f-count").textContent = F.error ? t("app.find.error") : F.pending ? t("app.find.searching") : F.ran ? (F.total > F.events.length ? t("app.find.count_top", { count: F.total, top: fmtNum(F.events.length) }) : t("app.find.count", { count: F.total })) : t("app.find.enter_hint");
  if (ST.tab !== "clips") return;
  const el = $("tab-clips");
  if (F.error) { el.innerHTML = `<div class="empty" style="color:var(--bad-text)">${esc(F.error)}</div>`; return; }
  if (!Object.values(ST.chips).some(Boolean)) { el.innerHTML = `<div class="empty"><b>${te("app.find.all_off")}</b>${te("app.find.all_off_hint")}</div>`; return; }
  if (!F.ran || (F.pending && !F.events.length)) { el.innerHTML = `<div class="empty"><div class="spin"></div></div>`; return; }
  if (!F.events.length) { el.innerHTML = `<div class="empty"><b>${te("app.find.none")}</b>${te("app.find.none_hint")}</div>`; return; }
  const said = (F.filter && F.filter.said) || [];
  let html = "";
  for (const e of F.events) {
    const cur = ST.event && ST.event.id === e.id;
    html += `<div class="fev k-${esc(e.kind)} ${cur ? "cur" : ""} ${e.status === "false" ? "false" : ""}" data-id="${e.id}" data-rec="${e.rec_id}" id="fev-${e.id}" title="${esc(I18N.has("app.status." + e.status) ? t("app.status." + e.status) : e.status)}${e.note ? " - " + esc(e.note) : ""}">
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
  if (!prov) { info.innerHTML = `<div class="hint" style="color:var(--bad-text)">${te("app.vod.unsupported")}</div>`; return; }
  // a streamer's page (profile / videos tab) is not one VOD: the VOD browser on that streamer. Parsed locally on the
  // server (inspect:false = no network), so a profile link opens at once instead of after the site refuses it
  if (window.VB && await vodOpenChannel(url, tok)) return;
  if (tok !== vodTok) return;
  info.innerHTML = `<div class="hint">${provBadge(prov)} ${te("app.vod.asking", { site: prov })}</div>`;
  try {
    const m = await post("/api/vods/inspect", { url });
    if (tok !== vodTok || $("vod-url").value.trim() !== url) return;
    vodMeta = m; vodMeta._asked = url;
    const sz = m.size_bytes ? fmtBytes(m.size_bytes) : t("app.vod.size_unknown");
    info.innerHTML = `<div class="vodcard"><span class="t">${esc(m.title || m.id)}</span>
      <b>${te("app.vod.site")}</b><span>${provBadge(m.provider)} ${m.was_live ? te("app.vod.past_live") : te("app.vod.video")}</span>
      <b>${te("app.vod.channel")}</b><span>${esc(m.channel || "-")}</span>
      <b>${te("app.vod.started")}</b><span>${m.start_utc ? fmtDate(m.start_utc) : "-"}</span>
      <b>${te("app.vod.length")}</b><span>${m.duration_s ? fmtDur(m.duration_s) : "-"}</span>
      <b>${te("app.vod.quality")}</b><span>${m.height ? m.height + "p" : "?"}${m.fps ? Math.round(m.fps) : ""} ${esc(m.vcodec || "")}, ${esc(sz)}</span>
      <b>${te("app.vod.saved_to")}</b><span class="mono t-xs">${esc(m.dest)}</span></div>`;
    if (window.VB && m.channel_key) vodChannelRow(info, m);
    if (m.already) { $("vod-note").innerHTML = `${te("app.vod.already", { name: m.already.name })}${m.already.rec_id ? ` <a href="#" id="vod-open">${te("app.vod.open_it")}</a>` : ""}`; const o = $("vod-open"); if (o) o.onclick = (e) => { e.preventDefault(); $("dlg-vod").close(); openPanel("rec"); select(m.already.rec_id); }; }
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
  row.innerHTML = `<button type="button" class="btn sm" id="vod-more">${te("app.vod.more_from", { name })}</button>
    <button type="button" class="btn sm" id="vod-star" title="${te("app.vod.fav_title")}">&#9734; ${te("app.vod.fav")}</button>`;
  info.appendChild(row);
  $("vod-more").onclick = () => {
    $("dlg-vod").close();
    VB.open({ provider: m.provider, key: m.channel_key, name: m.channel, url: m.channel_url,
      preselect_url: m.url, preselect_id: m.id, preselect_title: m.title || "", already: m.already || null });
  };
  const star = $("vod-star"); let favId = null;
  const paint = () => { star.innerHTML = (favId ? "&#9733; " : "&#9734; ") + te("app.vod.fav"); star.classList.toggle("pri", !!favId); };
  try {
    const favs = await api("/api/vodbrowse/favorites");
    const f = (Array.isArray(favs) ? favs : favs.favorites || []).find(x => x.provider === m.provider && (x.channel_key || x.key) === m.channel_key);
    favId = f ? f.id : null;
  } catch (e) { /* the list is only for the star's state */ }
  paint();
  star.onclick = async () => {
    star.disabled = true;
    try {
      if (favId) { await del(`/api/vodbrowse/favorites/${favId}`); favId = null; toast(t("app.vod.fav_removed", { name })); }
      else { const f = await post("/api/vodbrowse/favorites", { provider: m.provider, key: m.channel_key, name: m.channel, url: m.channel_url }); favId = (f && (f.id || (f.favorite && f.favorite.id))) || null; toast(t("app.vod.fav_added", { name })); }
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
    toast(r.already_queued ? t("app.vod.dl_already") : t("app.vod.dl_started", { dir: r.dest }), 5000);
    $("vod-note").textContent = r.already_queued ? t("app.vod.note_already") : t("app.vod.note_queued");
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
  u.addEventListener("input", () => { vodMeta = null; ++vodTok; $("vod-go").disabled = true; const p = vodProvider(u.value.trim()); $("vod-note").textContent = u.value.trim() ? (p ? t("app.vod.link", { site: p }) : t("app.vod.not_supported")) : ""; });
}

/* ---------------- Transcribe all (palette, Library menu, Transcript tab) ---------------- */
// the scopes; label app.tall.<scope>, explanation app.tall.<scope>_tip
const TALL_SCOPES = ["missing", "outdated", "all"];
let tallScope = "outdated";
async function openTranscribeAll() {
  const dlg = $("dlg-tall"); const body = $("tall-body"); const go = $("tall-go");
  body.innerHTML = `<div class="hint">${te("app.tall.counting")}</div>`; $("tall-note").textContent = ""; go.disabled = true;
  // a <dialog> keeps returnValue across openings and Escape closes it WITHOUT setting one: clear the
  // last "go" so backing out with Escape can never queue anything
  dlg.returnValue = "";
  if (!dlg.open) dlg.showModal();
  let p;
  try { p = await api("/api/transcribe-all"); } catch (err) { body.innerHTML = `<div class="hint" style="color:var(--bad-text)">${esc(err.message)}</div>`; return; }
  const est = (s) => s.est_s != null ? te("app.tall.about", { duration: fmtDur(s.est_s) }) : "";
  body.innerHTML = TALL_SCOPES.map((k) => {
    const s = p.scopes[k]; const label = t("app.tall." + k), tip = t("app.tall." + k + "_tip");
    return `<label class="opt${s.to_queue ? "" : " none"}" title="${esc(tip)}"><input type="radio" name="tall" value="${k}" ${s.to_queue ? "" : "disabled"}>
      <span><b>${esc(label)}</b><small>${esc(tip)}</small></span>
      <span class="num">${te("app.tall.recs_hours", { recordings: fmtNum(s.n), hours: fmtUnit(Number(s.hours) || 0, "hour", Number(s.hours) % 1 ? 1 : 0) })}${s.already_queued ? `<br><small>${te("app.tall.already", { count: s.already_queued })}</small>` : ""}${est(s) ? `<br><small>${est(s)}</small>` : ""}</span></label>`;
  }).join("") + `<div class="hint">${te("app.tall.hint", { model: p.model })}
    ${p.rate ? "" : te("app.tall.hint_rate")}
    ${te("app.tall.hint_jobs")}</div>`;
  const pick = (k) => { const r = body.querySelector(`input[value="${k}"]:not(:disabled)`); if (r) { r.checked = true; tallScope = k; return true; } return false; };
  if (!pick(tallScope)) ["outdated", "missing", "all"].some(pick);
  const sync = () => {
    const sel = body.querySelector("input[name=tall]:checked"); const s = sel && p.scopes[sel.value];
    go.disabled = !s || !s.to_queue || !p.stt_enabled;
    go.textContent = s && s.to_queue ? t("app.tall.queue_n", { count: s.to_queue }) : t("app.tall.queue");
    $("tall-note").textContent = !p.stt_enabled ? t("app.tall.stt_off") : "";
  };
  body.querySelectorAll("input[name=tall]").forEach(r => r.onchange = () => { tallScope = r.value; sync(); });
  sync();
}
$("dlg-tall").addEventListener("close", () => {
  const dlg = $("dlg-tall"); const go = dlg.returnValue === "go"; dlg.returnValue = "";
  if (!go || $("tall-go").disabled) return;
  const scope = tallScope;
  if (scope === "all" && !confirm(t("app.tall.confirm_all"))) return;
  post("/api/transcribe-all", { scope }).then(x => {
    toast(x.candidates > x.queued ? t("app.tall.queued_some", { queued: fmtNum(x.queued), already: fmtNum(x.candidates - x.queued) }) : t("app.tall.queued", { queued: fmtNum(x.queued) }), 4000);
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
const jobsRetryFailed = () => post("/api/jobs/retry-failed").then(r => { toast(t("app.jobs.retried", { count: r.retried })); loadJobs(); }).catch(err => toast(err.message, 5000));
const jobsClearFinished = () => post("/api/jobs/clear").then(r => { toast(t("app.jobs.removed", { count: r.removed })); loadJobs(); }).catch(err => toast(err.message, 5000));
function jobsClearAll() {
  const c = ST.counts || {};
  const n = (c.queued || 0) + (c.done || 0) + (c.failed || 0) + (c.skipped || 0);
  if (!n) { toast(t("app.jobs.nothing")); return; }
  if (!confirm(t("app.jobs.confirm_clear", { total: fmtNum(n), queued: fmtNum(c.queued || 0), finished: fmtNum((c.done || 0) + (c.failed || 0) + (c.skipped || 0)) }) + "\n" + (c.running ? t("app.jobs.confirm_running", { count: c.running }) + "\n" : "") + t("app.jobs.confirm_dropped"))) return;
  post("/api/jobs/clear", { all: true }).then(r => { toast(t("app.jobs.removed_jobs", { count: r.removed })); loadJobs(); }).catch(err => toast(err.message, 5000));
}

// drive-aware parallelism (Settings > Jobs): what each library drive can feed at once (measured), and what runs there now
async function loadDrives() {
  const box = $("dr-drives"); if (!box || $("drawer").hidden) return;
  try { ST.drives = await api("/api/drives"); } catch (err) { box.innerHTML = `<div class="hint">${te("app.drv.error", { error: err.message })}</div>`; return; }
  box.innerHTML = renderDrives() || `<div class="hint">${te("app.drv.none")}</div>`;
  wireDrives(box);
}
function renderDrives() {
  const d = ST.drives; if (!d) return "";
  const cap = d.capacity || {};
  let html = `<div class="subhead">${te("app.drv.title")}<span class="spacer"></span><span title="${te("app.drv.gpu_title")}">${te("app.drv.gpu", { running: cap.gpu_running || 0, parallel: cap.gpu_parallel || "-" })}</span><span title="${te("app.drv.mode_title")}">${te("app.drv.mode", { mode: String(cap.mode || "auto") })}</span><button type="button" class="btn sm" id="dr-bench-all" title="${te("app.drv.bench_all_title")}">${te("app.drv.bench_all")}</button></div>`;
  for (const v of d.drives || []) {
    const b = v.bench; const mb = b && b.mb_s ? t("app.drv.mbs", { list: ["1", "2", "4"].map(n => b.mb_s[n] != null ? `${n}x ${fmtNum(Math.round(b.mb_s[n]))}` : null).filter(Boolean).join(" / ") }) : t("app.drv.not_benched");
    const sl = v.slots || {};
    const running = (v.running || []).map(j => `${stageName(j.stage)} #${j.rec_id || "-"}`).join(", ");
    html += `<div class="drv"><div class="top"><b>${esc(v.drive)}</b><span class="badge ${v.kind === "ssd" ? "ok" : v.kind === "hdd" ? "warn" : ""}">${esc(v.kind || "?")}</span>
        <span class="mono t-xs">${esc(mb)}</span>${b && !b.cache_bypass ? `<span class="badge warn" title="${esc(srvF(b, "note"))}">${te("app.drv.cache")}</span>` : ""}
        ${v.bench_state ? `<span class="badge acc">${te("app.drv.bench_state", { state: stateName(v.bench_state) })}</span>` : ""}<span class="spacer"></span><button type="button" class="btn ghost sm" data-bench="${esc(v.drive)}">${te("app.drv.bench")}</button></div>
      <div class="meta"><span title="${te("app.drv.slots_title")}">${te("app.drv.slots", { detect: sl.detect ?? "-", proxy: sl.proxy ?? "-", transcribe: sl.transcribe ?? "-" })}</span><span>${te("app.drv.files", { count: v.files || 0, size: fmtBytes(v.bytes) })}</span><span>${te("app.drv.queued", { count: v.queued || 0 })}</span>${running ? `<span style="color:var(--accent-2)">${te("app.drv.running", { jobs: running })}</span>` : ""}${b ? `<span title="${esc(fmtDate(b.utc))}">${te("app.drv.measured", { ago: fmtAgo(b.utc) })}</span>` : ""}</div></div>`;
  }
  return html;
}
function wireDrives(el) {
  const go = (body) => post("/api/drives/benchmark", body).then(r => toast(t("app.drv.started", { drives: fmtList(r.started) }), 4000)).catch(err => toast(err.message, 5000)).finally(loadDrives);
  const all = el.querySelector("#dr-bench-all"); if (all) all.onclick = () => go({});
  el.querySelectorAll("button[data-bench]").forEach(b => b.onclick = () => go({ drive: b.dataset.bench }));
}

function renderJobs() {
  // a re-render mid-drag would drop the row being dragged: the live feed's refresh waits for the drop
  if (ST.jobDrag) { ST.jobDragStale = true; return; }
  const el = $("tab-jobs"); const c = ST.counts || {};
  const nq = ST.jobs.filter(j => j.state === "queued").length;
  let html = `<div class="phd"><h2>${te("app.jobs.title")}</h2><span class="sp"></span><button type="button" class="btn sm ${c.paused ? "pri" : ""}" id="j-pause">${icon(c.paused ? "play" : "pausej")}${c.paused ? te("app.pause.resume") : te("app.pause.pause")}</button><button type="button" class="ib sm" data-menu="jobs" data-tip="${te("app.jobs.more_tip")}">${icon("more")}</button></div>
    <div class="stat4"><div><b>${fmtNum(c.running || 0)}</b><span>${te("app.jobs.running")}</span></div><div><b>${fmtNum(c.queued || 0)}</b><span>${te("app.jobs.queued")}</span></div><div><b class="${c.failed ? "bad" : ""}">${fmtNum(c.failed || 0)}</b><span>${te("app.jobs.failed")}</span></div><div><b>${fmtNum((c.done || 0) + (c.skipped || 0))}</b><span>${te("app.jobs.finished")}</span></div></div>`;
  if (c.paused) html += `<div class="psub"><span class="badge warn">${te("app.jobs.lanes_paused")}</span><span class="t-xs muted">${te("app.jobs.lanes_paused_hint")}</span></div>`;
  html += `<div class="pbody" id="jlist">`;
  if (!ST.jobs.length) html += `<div class="empty"><b>${te("app.jobs.none")}</b>${te("app.jobs.none_hint")}</div>`;
  const QMv = window.QM;      // queue moves (queue.js): row tools, multi-select, grouping, "Next up"
  // labels are HTML (queue.js sectionLabel returns te() text)
  const GROUPS = [["running", te("app.jobs.g_running")], ["queued", QMv ? QMv.sectionLabel(nq) : nq > 1 ? te("app.jobs.g_queued_drag") : te("app.jobs.g_queued")], ["failed", te("app.jobs.g_failed")], ["done", te("app.jobs.g_done")]];
  const groupOf = (s) => s === "skipped" ? "done" : s;
  const nextUp = QMv ? QMv.nextUp() : new Set();
  for (const [g, label] of GROUPS) {
    let js = ST.jobs.filter(j => groupOf(j.state) === g); if (!js.length) continue;
    if (g === "queued" && QMv) js = QMv.arrange(js);
    html += `<div class="sect-t">${label}${g === "queued" && QMv && nq > 1 ? `<span class="sp"></span>${QMv.groupToggle()}` : ""}</div>`;
    let prevJ = null;
    for (const j of js) {
      if (QMv) html += QMv.before(j, prevJ);
      prevJ = j;
      const cls = j.state === "done" ? "ok" : j.state === "failed" ? "bad" : j.state === "running" ? "acc" : j.state === "skipped" ? "warn" : "";
      const q = j.state === "queued";
      let what = j.rec_name || "";
      if (j.stage === "download" && j.arg) { try { const a = JSON.parse(j.arg); what = `${a.provider || ""}: ${a.title || a.url || ""}`; } catch (e) { } }
      const act = j.state === "failed" || j.state === "skipped" ? `<button type="button" class="btn ghost sm" data-act="retry" data-id="${j.id}">${te("app.jobs.retry")}</button>`
        : q ? `<button type="button" class="btn ghost sm" data-act="skip" data-id="${j.id}">${te("app.jobs.skip")}</button>`
        : j.state === "running" && (j.stage === "download" || j.stage === "proxy" || j.stage === "makeclip") ? `<button type="button" class="btn ghost sm" data-act="stop" data-id="${j.id}" title="${te("app.jobs.stop_title")}">${te("app.jobs.stop")}</button>` : "";
      html += `<div class="jrow${q ? " jq" : ""}${QMv ? QMv.rowClass(j) : ""}" data-rec="${j.rec_id || ""}" data-job="${j.id}" id="job-${j.id}"${q ? ` draggable="true"` : ""}${QMv && QMv.hidden(j) ? " hidden" : ""}>
        <span class="grip" title="${te("app.jobs.drag_title")}">${icon("grip")}</span>
        <div style="min-width:0"><div class="jl1"><b>${esc(stageName(j.stage))}</b><span class="faint num t-xs">#${j.id}</span><span class="badge ${cls}">${esc(stateName(j.state))}</span>${QMv ? QMv.badge(j, nextUp) : ""}<span class="age">${j.started_utc ? fmtAgo(j.started_utc) : fmtAgo(j.created_utc)}</span></div>
          ${what ? `<div class="what" title="${esc(what)}">${esc(j.stage === "download" ? what : recLabelFor(j.rec_id, what))}</div>` : ""}
          <div class="msg ${j.state === "failed" ? "err" : ""}" id="jmsg-${j.id}" title="${esc(srvF(j, "error") || srvF(j, "message"))}">${esc(srvF(j, "error") || srvF(j, "message"))}</div>
          <div class="bar" id="jbar-${j.id}" ${j.state === "running" ? "" : "hidden"}><i style="width:${pct(j.progress)}"></i></div></div>
        <div class="jact">${QMv ? QMv.tools(j) : ""}${act}</div></div>`;
    }
  }
  html += `</div>`;
  el.innerHTML = html;
  $("j-pause").onclick = togglePause;
  wireJobDrag(el);
  if (QMv) QMv.wire(el);
  // a 409 means the row was stale (the job already started or finished): reload instead of leaving the button there
  el.querySelectorAll("button[data-act]").forEach(b => b.onclick = (e) => { e.stopPropagation(); b.disabled = true; post(`/api/jobs/${b.dataset.id}/${b.dataset.act}`).catch(err => toast(t("app.jobs.stale", { error: err.message }), 4000)).finally(loadJobs); });
  // shift / ctrl click selects queued rows (queue.js) instead of opening the recording
  el.querySelectorAll(".jrow[data-rec]").forEach(row => row.onclick = (e) => { if (QMv && QMv.click(e, row)) return; if (row.dataset.rec) { showTab("rec"); select(+row.dataset.rec); } });
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
      post("/api/jobs/reorder", { ids }).then(r => { if (r.moved) toast(t("app.jobs.moved", { id: moved })); })
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
    // clip search results on screen follow new / re-scored / relabeled events (a short debounce:
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
      if (j.rec_id) ST.jobprog[j.rec_id] = { stage: j.stage, progress: j.progress, message: j.message, message_i18n: j.message_i18n };
      const bar = $("bar-" + j.rec_id); if (bar) { bar.hidden = false; bar.firstElementChild.style.width = pct(j.progress); }
      const jb = $("jbar-" + j.id); if (jb) { jb.hidden = false; jb.firstElementChild.style.width = pct(j.progress); }
      const jm = $("jmsg-" + j.id); if (jm) jm.textContent = srvF(j, "message");
      if (ST.sel === j.rec_id) {
        const label = `${srvF(j, "message") || t("app.rp.working")} ${pct(j.progress)}`;
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
      if (j.stage === "download") toast(d.action === "done" ? t("app.sse.vod_done", { message: srvF(j, "message") }) : t("app.sse.vod_failed", { error: srvF(j, "error") }), 8000);
      else if (d.action === "failed" && j.rec_id && j.rec_id === ST.sel) toast(t("app.sse.stage_failed", { stage: stageName(j.stage), error: srvF(j, "error") }), 7000);
      if (j.rec_id && j.rec_id === ST.sel) select(j.rec_id);
    }
    if (d.action === "started" && j.rec_id) {
      ST.jobprog[j.rec_id] = { stage: j.stage, progress: 0, message: t("app.sse.starting") };
      if (j.rec_id === ST.sel) { if (ST.ctab === "tx") renderTranscript(); if (ST.tab === "events") renderEvents(); if (ST.ctab === "rec") renderRecPane(); }
    }
    bumpJobs();
  });
  es.addEventListener("jobs", () => bumpJobs());
  es.addEventListener("workmode", (e) => { if (window.WM) WM.onEvent(JSON.parse(e.data)); });
  es.addEventListener("jobs", (e) => { if (window.WM && /paused|resumed/.test(e.data)) WM.refresh(); });
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
      toast(t("app.sse.op_failed", { label: srvF(op, "label") || t("app.sse.handoff"), error: srvF(op, "error") }), 8000);
      const res = $("ho-res"); if (res) res.textContent = srvF(op, "error") || t("app.sse.failed");
    } else if (d.action === "state" && d.link && d.link.state === "no_project") {
      toast(t("app.sse.resolve_up", { where: srvF(d.link, "error") || t("app.sse.project_manager") }), 5000);
    }
  });
  es.addEventListener("drives", (e) => {
    const d = JSON.parse(e.data);
    if (d.action === "bench_done" && d.bench) toast(t("app.sse.bench_done", { drive: d.bench.drive, speed: fmtUnit(Math.round((d.bench.mb_s || {})["1"] || 0), "megabyte-per-second"), kind: d.bench.kind || "?" }), 4000);
    if (d.action === "bench_failed") toast(t("app.sse.bench_failed", { error: srvF(d, "error") }), 6000);
    loadDrives();                                   // only when the settings modal is open
  });
  // add-on downloads (PackStatus per change, <= 4 a second per pack) and the setup flag: setup.js owns both
  es.addEventListener("packs", (e) => { if (window.SU) SU.onPackEvent(JSON.parse(e.data)); });
  es.addEventListener("setup", (e) => { if (window.SU) SU.onSetupEvent(JSON.parse(e.data)); });
  es.addEventListener("makeclip", (e) => { if (window.MC) MC.onEvent(JSON.parse(e.data)); });
  es.addEventListener("updates", (e) => { if (window.UPD) UPD.onState(JSON.parse(e.data)); });
  es.onerror = () => { ST.feedLost = true; setStatus(t("app.sse.feed_lost")); };
  es.onopen = () => { if (ST.feedLost) { ST.feedLost = false; setStatus(""); } };
}

/* ---------------- settings (modal #drawer; every field autosaves) ---------------- */
// [group id, section, field, type, extra]   type: num(step) | text | list | lines | check | select(choices key or array)
// label = app.set.f.<section>.<field>; a group id opens a group: name app.set.g.<id>, subtitle app.set.g.<id>_sub (when present)
const FIELDS = [
  ["library", "library", "roots", "lines"],
  ["", "library", "recursive", "check"],
  ["", "library", "patterns", "text"],
  ["", "library", "min_age_s", "num", 30],
  ["", "library", "watch_s", "num", 10],
  ["", "library", "auto_scan_new", "check"],
  ["player", "player", "aliases", "list"],
  ["", "player", "squad", "list"],
  ["audio", "audio", "track_names", "list"],
  ["", "audio", "mic_track", "num", 1],
  ["", "audio", "voip_track", "num", 1],
  ["", "audio", "game_track", "num", 1],
  ["", "audio", "normalize_mic", "check"],
  ["stt", "stt", "enabled", "check"],
  ["", "stt", "model", "select", "stt_models"],
  ["", "stt", "redetect_windows", "check"],
  ["", "stt", "translate", "check"],
  ["", "stt", "compute", "select", "stt_compute"],
  ["", "stt", "vad", "check"],
  ["", "stt", "word_timestamps", "check"],
  ["", "stt", "batched", "check"],
  ["", "stt", "batch_size", "num", 1],
  ["", "stt", "hotwords", "text"],
  ["jobs", "jobs", "auto_transcribe", "check"],
  ["", "jobs", "auto_detect", "check"],
  ["", "jobs", "parallel", "select", ["auto", "1", "2", "3", "4", "5", "6", "7", "8"]],
  ["", "jobs", "gpu_parallel", "num", 1],
  ["", "jobs", "max_parallel", "num", 1],
  ["", "jobs", "auto_bench", "check"],
  ["", "jobs", "io_lanes", "num", 1],
  ["", "jobs", "video_lanes", "num", 1],
  ["", "jobs", "stt_lanes", "num", 1],
  ["detection", "detection", "roi_fps", "num", 1],
  ["", "detection", "refine_window_s", "num", 0.5],
  ["", "detection", "merge_s", "num", 0.5],
  ["", "detection", "ocr_min_conf", "num", 0.05],
  ["", "detection", "ocr_fast_ticker", "check"],
  ["", "detection", "ticker_reuse", "check"],
  ["", "detection", "ocr_device", "select", "ocr_devices"],
  ["", "detection", "ticker_every", "num", "1"],
  // voice keywords per speech language: drawn by sttlang.js into #sl-kw (above rms_spike_db)
  ["highlights", "highlights", "rms_spike_db", "num", 1],
  ["", "highlights", "multikill_s", "num", 1],
  ["", "highlights", "pre_roll_s", "num", 1],
  ["", "highlights", "post_roll_s", "num", 1],
  ["encoding", "proxies", "enabled", "check"],
  ["", "proxies", "dir", "text"],
  ["", "proxies", "height", "num", 8],
  ["", "proxies", "bitrate_k", "num", 250],
  ["", "proxies", "codec", "select", "proxy_codecs"],
  ["", "proxies", "clip_codec", "select", "proxy_codecs"],
  ["clippool", "clippool", "dir", "text"],
  ["", "clippool", "mode", "select", ["copy", "exact"]],
  ["", "clippool", "crf", "num", 1],
  // the graphics cards list (hardware.js fills #hw-gpus) comes before this field
  ["hardware", "hardware", "auto_gpu_encoder", "check"],
  ["vods", "vods", "enabled", "check"],
  ["", "vods", "dir", "text"],
  ["", "vods", "max_height", "num", 120],
  ["", "vods", "parallel", "num", 1],
  ["", "vods", "add_to_library", "check"],
  ["resolve", "resolve", "enabled", "check"],
  ["", "resolve", "launch_on_start", "check"],
  ["", "resolve", "launch_on_send", "check"],
  ["", "resolve", "project", "text"],
  ["", "resolve", "wait_min", "num", 1],
  ["", "resolve", "exe", "text"],
  ["", "resolve", "bin", "text"],
  ["", "resolve", "selects_name", "text"],
  ["", "resolve", "color_kill", "select", "marker_colors"],
  ["", "resolve", "color_assist", "select", "marker_colors"],
  ["", "resolve", "color_death", "select", "marker_colors"],
  ["", "resolve", "color_spotted", "select", "marker_colors"],
  ["", "resolve", "color_voice", "select", "marker_colors"],
  ["", "resolve", "color_manual", "select", "marker_colors"],
  ["server", "server", "open_browser", "check"],
  ["", "server", "verbose_console", "check"],
  // the release check (GET /api/updates); "Check now" + the result line are added after these fields
  ["updates", "updates", "auto_check", "check"],
  ["", "updates", "channel", "select", ["stable", "beta"]],
  ["", "updates", "interval_hours", "num", 1],
  ["", "updates", "repo", "text"],
];
// keyed by stable group id (names are translated); langpick.js / workmode.js may still add entries keyed by the shown name
const GROUP_ICONS = { "library": "folder", "player": "target", "audio": "wave", "stt": "mic", "jobs": "jobs", "detection": "events", "highlights": "star", "encoding": "bolt", "hardware": "probe", "vods": "vods", "resolve": "resolve", "server": "settings", "updates": "refresh", "performance": "bolt", "addons": "download", "clipcache": "trash", "clippool": "folder", "system": "info", "language": "globe" };
// label params: the selects name field takes a literal {date} token that the user types
const LABEL_PARAMS = { "resolve.selects_name": { token: "{date}" } };
// a select choice shown in words where one exists (the option value stays the settings value)
const optLabel = (o) => I18N.has("app.set.opt." + o) ? t("app.set.opt." + o) : String(o);

async function openDrawer(group) {
  const s = ST.settings;
  let stt = null; try { stt = await api("/api/stt/status"); } catch (e) { }
  let html = "", gi = 0; const groups = [];
  // id: stable group id (openSettings, icons); the shown name and subtitle come from the catalog
  const grp = (id, name, sub) => { groups.push({ id, name }); return `<div class="grp" id="set-g-${gi++}" data-g="${esc(id)}">${esc(name)}${sub ? `<small>${esc(sub)}</small>` : ""}</div>`; };
  const grpT = (id) => grp(id, t("app.set.g." + id), I18N.has("app.set.g." + id + "_sub") ? t("app.set.g." + id + "_sub") : "");
  if (window.LP) html += grp("language", t("core.lang.group"), t("core.lang.group_sub")) + LP.settingsHtml();     // langpick.js
  for (const [g, sec, f, type, extra] of FIELDS) {
    if (g) html += grpT(g);
    if (sec === "stt" && f === "enabled" && stt) {
      const parts = [t(stt.available ? "app.set.stt.engine_ready" : "app.set.stt.engine_missing")];
      if (stt.loaded) parts.push(t("app.set.stt.loaded", { model: stt.loaded, device: stt.device || "" }));
      const cached = stt.cached_models || [];
      parts.push(cached.length ? t("app.set.stt.cached", { models: fmtList(cached) }) : t("app.set.stt.cached_none"));
      html += `<div class="sline"><span class="sd ${stt.available ? (stt.last_error ? "warn" : "ok") : "bad"}"></span><span>${parts.map(esc).join(" · ")}${stt.last_error ? `<br><span style="color:var(--warn-text)">${esc(stt.last_error)}</span>` : ""}</span></div>`;
    }
    if (sec === "stt" && f === "model") html += `<div id="sl-set"></div>`;          // speech language (sttlang.js)
    if (sec === "highlights" && f === "rms_spike_db") html += `<div id="sl-kw"></div>`;   // keywords per language
    if (sec === "jobs" && f === "parallel") html += `<div class="drvbox" id="dr-drives"><div class="hint" style="padding:8px 12px">${te("app.set.drives_loading")}</div></div>`;
    if (sec === "proxies" && f === "enabled") html += `<div class="sline" id="enc-now">${te("app.set.enc.checking")}</div>`;
    if (sec === "hardware" && f === "auto_gpu_encoder") html += `<div class="hw-gpus" id="hw-gpus"><div class="hint" style="padding:8px 12px">${te("app.set.hw_loading")}</div></div>`;
    if (sec === "detection" && f === "roi_fps") html += `<div class="field"><label>${te("app.set.cal_label")}</label><button type="button" class="btn sm" id="dr-cal">${icon("calib")}${te("app.set.cal_open")}</button></div>`;
    const v = (s[sec] || {})[f]; let input;
    if (type === "num") input = `<input class="inp" type="number" step="${extra}" data-sec="${sec}" data-f="${f}" value="${v}">`;
    else if (type === "check") input = `<input class="toggle" type="checkbox" data-sec="${sec}" data-f="${f}" ${v ? "checked" : ""}>`;
    else if (type === "list") input = `<input class="inp" type="text" data-sec="${sec}" data-f="${f}" value="${esc((v || []).join("; "))}">`;
    else if (type === "lines") input = `<textarea class="inp" rows="${Math.max(3, (v || []).length + 1)}" data-sec="${sec}" data-f="${f}">${esc((v || []).join("\n"))}</textarea>`;
    else if (type === "select") { const ch = Array.isArray(extra) ? extra : ((s.choices || {})[extra] || []); input = `<select class="inp" data-sec="${sec}" data-f="${f}">${ch.map(o => `<option value="${esc(o)}" ${String(o) === String(v) ? "selected" : ""}>${esc(optLabel(o))}</option>`).join("")}</select>`; }
    else input = `<input class="inp" type="text" data-sec="${sec}" data-f="${f}" value="${esc(v)}">`;
    html += `<div class="field"><label>${te("app.set.f." + sec + "." + f, LABEL_PARAMS[sec + "." + f])}</label>${input}</div>`;
    if (sec === "updates" && f === "repo") html += `<div class="field"><label>${te("app.set.up_label")}</label><button type="button" class="btn sm" id="up-check">${icon("refresh")}${te("app.set.up_check")}</button></div><div class="sline" id="up-res"></div>`;
  }
  if (window.WM) html += grpT("performance") + WM.settingsHtml(s);     // workmode.js
  // add-ons (setup.js fills #ao-gpu / #ao-list / #ao-models and wires the buttons: SU.mountSettings)
  html += grpT("addons") +
    `<div class="sline" id="ao-gpu"></div><div class="ao-list" id="ao-list"><div class="hint">${te("app.set.ao.loading")}</div></div>` +
    `<div class="ao-sub">${te("app.set.ao.models")}</div><div class="ao-list" id="ao-models"></div>` +
    `<div class="ao-acts"><button type="button" class="btn sm" id="ao-ytdlp" title="${te("app.set.ao.ytdlp_title")}">${icon("refresh")}${te("app.set.ao.ytdlp")}</button><button type="button" class="btn sm" id="ao-setup" title="${te("app.set.ao.setup_title")}">${icon("settings")}${te("app.set.ao.setup")}</button></div>`;
  const st = ST.status || {};
  html += grpT("clipcache") + `<div class="field"><label id="cc-usage">-</label><button type="button" class="btn sm" id="cc-clear" title="${te("app.set.cc.clear_title")}">${icon("trash")}${te("app.set.cc.clear")}</button></div>`;
  html += grpT("system") + `<div class="hint">${tHtml("app.set.sys.data_html", { path: st.data_dir || "" })}<br>${tHtml("app.set.sys.log_html", { path: st.log_file || "", level: st.console_level || "" })}<br><a href="/api/system/logs?tail=300" target="_blank">${te("app.set.sys.log_tail")}</a> · <a href="/api/system/hw" target="_blank">${te("app.set.sys.hw")}</a> · <a href="/api/stt/status" target="_blank">${te("app.set.sys.stt")}</a></div>`;
  $("dbody").innerHTML = html;
  $("setnav").innerHTML = groups.map((g, i) => `<button type="button" data-i="${i}">${icon(GROUP_ICONS[g.id] || GROUP_ICONS[g.name] || "chev")}${esc(g.name)}</button>`).join("");
  $("setnav").querySelectorAll("button").forEach(b => b.onclick = () => { const gEl = $("set-g-" + b.dataset.i); if (gEl) gEl.scrollIntoView({ block: "start" }); });
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
      ST.settings = r.settings; $("dmsg").className = "msg"; $("dmsg").textContent = r.changed.length ? t("app.set.saved", { fields: r.changed.join(", ") }) : t("app.set.saved_none");
      if (row) { row.classList.add("saved"); setTimeout(() => row.classList.remove("saved"), 1400); }
      if (el.dataset.sec === "proxies" || el.dataset.sec === "hardware") refreshEncoderLine();
      if (el.dataset.sec === "library") renderLegend();
      if (el.dataset.sec === "performance" && window.WM) WM.refresh();
      if (r.changed.some(c => c.startsWith("library."))) toast(t("app.set.library_changed"));
    } catch (err) { $("dmsg").className = "msg bad"; $("dmsg").textContent = t("app.set.save_error", { error: err.message }); }
  });
  $("drawer").hidden = false; $("scrim").hidden = false;
  closePop(); closeCtx();
  loadDrives();
  refreshEncoderLine();
  const usage = (c) => { const u = $("cc-usage"); if (u) u.textContent = t("app.set.cc.usage", { count: c.items, used: fmtBytes(c.bytes), cap: fmtBytes(c.cap_bytes) }); };
  api("/api/clips-status").then(r => usage(r.cache)).catch(() => { });
  $("cc-clear").onclick = () => {
    const b = $("cc-clear"); b.disabled = true;
    post("/api/clips-cache/clear").then(r => {
      usage(r.cache);
      toast(t(r.left ? "app.set.cc.cleared_kept" : "app.set.cc.cleared", { count: r.removed, freed: fmtBytes(r.freed_bytes), left: fmtNum(r.left || 0) }), 4000);
    }).catch(err => toast(err.message, 5000)).finally(() => { b.disabled = false; });
  };
  if (window.SU) SU.mountSettings();
  if (window.SL) SL.mountSettings();
  if (window.HW) HW.mount();
  if (window.LP) LP.mountSettings();
  if (group) {
    // a stable group id ("addons", "clipcache", "encoding") or, for older callers, the start of the English name ("Add-ons")
    const norm = (x) => String(x).toLowerCase().replace(/[^a-z0-9]/g, "");
    const want = norm(group);
    let i = groups.findIndex(g => g.id === want);
    if (i < 0 && want) i = groups.findIndex(g => g.id.startsWith(want) || norm(g.name).startsWith(want));
    if (i >= 0) { const gEl = $("set-g-" + i); if (gEl) $("dbody").scrollTop = gEl.offsetTop - 8; }
  }
  navSync();
}
function openSettings(group) { return openDrawer(group); }
// Settings > Encoding: what clips / proxies actually encode with right now (re-read after a codec change)
function refreshEncoderLine() {
  refreshEncoder().then(e => {
    const el = $("enc-now"); if (!el) return;
    if (!e) { el.textContent = ""; return; }
    const lab = (c) => c === "libx264" ? t("app.set.enc.x264_cpu") : c === "h264_nvenc" ? "NVENC H.264" : c === "hevc_nvenc" ? "NVENC HEVC" : c === "h264_amf" ? "AMD H.264" : c === "h264_qsv" ? "Intel H.264" : c;
    el.innerHTML = `<span class="sd ok"></span><span>${tHtml("app.set.enc.now_html", { clip: lab(e.clip_effective), proxy: lab(e.proxy_effective), auto: lab(e.auto_now), why: srvText(e, "why") || "" })}` +
      (e.nvenc_sessions != null ? ` <span class="muted">${te("app.set.enc.sessions", { count: e.nvenc_sessions })}</span>` : "") + `</span>`;
  });
}
function closeDrawer() { $("drawer").hidden = true; $("scrim").hidden = true; }

/* ---------------- command palette (Ctrl+K) ---------------- */
function paletteCommands() {
  const r = ST.detail, e = ST.event && r && ST.event.rec_id === r.id ? ST.event : null, c = ST.counts || {};
  const st = (r && r.stages) || {};
  const C = [];
  // g: group id (app.pal.g.<g>); label: shown text; kw: extra search words (app.pal.kw.*, translated)
  const add = (g, ic, label, act, k, kw) => C.push({ g: t("app.pal.g." + g), ic, label, act, k, kw });
  add("panels", "rec", t("app.pal.show_recs"), () => openPanel("rec"));
  add("panels", "events", t("app.pal.show_events"), () => openPanel("events"));
  add("panels", "clips", t("app.pal.find_clips"), () => openPanel("clips"), "", t("app.pal.kw.find_clips"));
  add("panels", "tx", t("app.pal.said"), () => { openPanel("said"); focusSearch(); });
  add("panels", "jobs", t("app.pal.show_jobs"), () => openPanel("jobs"), "", t("app.pal.kw.jobs"));
  add("panels", "vods", t("app.pal.vods"), () => openVods(), "", t("app.pal.kw.vods"));
  add("panels", "zones", t("app.pal.zones"), () => window.ZN && ZN.open(r ? { rec_id: r.id } : {}), "", t("app.pal.kw.zones"));
  add("panels", "panel", ST.panelOpen ? t("app.pal.fold") : t("app.pal.unfold"), () => ST.panelOpen ? closePanel() : openPanel(ST.tab), "Ctrl B");
  if (e) {
    add("event", "check", t("app.ev.keep"), () => setEventStatus(e.id, "kept"), "Y", t("app.pal.kw.verdict"));
    add("event", "x", t("app.ev.false"), () => setEventStatus(e.id, "false"), "X", t("app.pal.kw.verdict"));
    if (e.status !== "new") add("event", "reset", t("app.pal.reset_status"), () => setEventStatus(e.id, "new"), "U");
    add("event", "resolve", t("app.pal.marker"), () => handoff("markers", { event_ids: [e.id], rec_ids: undefined, status: "any", min_score: undefined, kinds: undefined }));
    add("event", "copy", t("app.menu.copy_tc"), () => copyText(tc(e.t_s)));
    add("event", "cut", t("app.ev.make_clip"), () => window.MC && MC.make({ event: e }), "", t("app.pal.kw.make_clip"));
    add("event", "cut", t("app.pal.make_clip_exact"), () => window.MC && MC.make({ event: e, mode: "exact" }), "", t("app.pal.kw.make_clip_exact"));
  }
  add("player", "replay", t("app.pal.replay"), () => $("pl-replay").click(), "R");
  add("player", "loop", t("app.pal.loop"), () => $("pl-loop").click(), "L");
  add("player", "mark", t("app.pal.mark"), () => $("pl-markhere").click(), "M", t("app.pal.kw.mark"));
  add("player", "wider", t("app.pal.wider"), () => $("pl-wider").click());
  add("player", "reset", t("app.pal.reset_inout"), () => $("pl-reset").click());
  if (r) {
    add("rec", "target", st.detect ? t("app.pal.redetect_rec") : t("app.pal.detect_rec"), () => detectRec(r.id));
    add("rec", "mic", (r.transcript && r.transcript.n) || st.transcribe ? t("app.pal.retranscribe_rec") : t("app.pal.transcribe_rec"), () => transcribeRec(r.id));
    add("rec", "tx", t("app.menu.show_tx"), () => showCtab("tx"), "3");
    add("rec", "qtop", t("app.menu.queue_top"), () => window.QM && QM.recToTop(r.id), "T", t("app.pal.kw.queue_top"));
    add("rec", "bolt", t("app.pal.proxy_rec"), () => post(`/api/recordings/${r.id}/proxy`).then(() => toast(t("app.toast.proxy_queued"))).catch(err => toast(err.message, 5000)));
    add("rec", "calib", t("app.pal.calibrate"), () => openCalibration(r.id), "", t("app.pal.kw.calibrate"));
    add("rec", "send", t("app.menu.send"), () => openHandoff($("btn-send")), "", t("app.pal.kw.send"));
    add("rec", "folder", t("app.menu.reveal"), () => post(`/api/recordings/${r.id}/reveal`).catch(err => toast(err.message, 5000)));
    add("rec", "copy", t("app.menu.copy_path"), () => copyText(r.path));
    add("rec", "probe", t("app.menu.reprobe"), () => post(`/api/recordings/${r.id}/probe`).then(() => toast(t("app.toast.probe_queued"))).catch(err => toast(err.message, 5000)));
  } else add("library", "calib", t("app.pal.calibrate"), () => openCalibration(0));
  add("library", "scan", t("app.menu.scan"), scan, "", t("app.pal.kw.scan"));
  add("library", "probe", t("app.menu.probe_all"), probeAll);
  add("library", "tx", t("app.act.transcribe_all"), openTranscribeAll, "", t("app.pal.kw.transcribe_all"));
  add("library", "target", t("app.menu.detect_all"), detectAll);
  add("library", "bolt", t("app.menu.proxies_all"), proxiesAll);
  add("library", "link", t("app.menu.import_vod"), openVod, "", t("app.pal.kw.import_vod"));
  add("jobs", c.paused ? "play" : "pausej", c.paused ? t("app.pal.resume_jobs") : t("app.pal.pause_jobs"), togglePause);
  add("jobs", "refresh", t("app.pal.retry_failed"), jobsRetryFailed);
  add("jobs", "check", t("app.pal.clear_finished"), jobsClearFinished);
  add("jobs", "qtop", t("app.pal.tx_first"), () => window.QM && QM.stageToTop("transcribe"), "", t("app.pal.kw.tx_first"));
  add("jobs", "qtop", t("app.pal.detect_first"), () => window.QM && QM.stageToTop("detect"), "", t("app.pal.kw.detect_first"));
  add("view", "info", t("app.pal.tab_event"), () => showCtab("event"), "1");
  add("view", "info", t("app.pal.tab_rec"), () => showCtab("rec"), "2");
  add("view", "wave", t("app.rp.tracks"), () => $("cp-audio").scrollIntoView({ block: "nearest" }), "", t("app.pal.kw.tracks"));
  add("view", "tx", t("app.pal.tab_tx"), () => showCtab("tx"), "3");
  add("view", "filter", t("app.pal.kinds"), () => openKinds(null), "", t("app.pal.kw.kinds"));
  if (resolveStartable()) add("app", "resolve", t("app.pal.resolve_start"), resolveStartOrCheck);
  else add("app", "resolve", t("app.pal.resolve_check"), resolveStartOrCheck);
  add("app", "settings", t("app.pal.settings"), () => openSettings(), "", t("app.pal.kw.settings"));
  add("app", "download", t("app.pal.addons"), () => openSettings("addons"), "", t("app.pal.kw.addons"));
  add("app", "settings", t("app.set.ao.setup"), () => { closeDrawer(); window.SU && SU.open({}); }, "", t("app.pal.kw.setup"));
  add("app", "refresh", t("app.pal.updates"), () => openSettings("updates"), "", t("app.pal.kw.updates"));
  add("app", "trash", t("app.pal.clear_cache"), () => openSettings("clipcache"));
  add("app", "folder", t("app.pal.clip_folder"), () => window.MC && MC.openFolder(), "", t("app.pal.kw.clip_folder"));
  add("app", "help", t("app.pal.shortcuts"), openHelp, "?");
  if (window.LP) add("app", "globe", t("core.lang.palette"), () => LP.menu(), "", t("core.lang.palette_kw"));
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
      const gs = t("app.pal.g.search");
      list.push({ g: gs, ic: "tx", label: t("app.pal.search_said", { q: raw }), act: () => { $("search").value = raw; $("gsearch").classList.add("has"); ST.q = raw; loadRecs(); runSearch(raw).then(() => openPanel("said")); } });
      list.push({ g: gs, ic: "rec", label: t("app.pal.search_recs", { q: raw }), act: () => { $("search").value = raw; $("gsearch").classList.add("has"); ST.q = raw; loadRecs(); openPanel("rec"); } });
      list.push({ g: gs, ic: "clips", label: t("app.pal.search_clips", { q: raw }), act: () => { $("f-said").value = raw; ST.finder.said = raw; finderSave(); finderMarks(); openPanel("clips"); runFind(); } });
    }
    sel = Math.max(0, Math.min(sel, list.length - 1));
    let g = null;
    $("pal-list").innerHTML = list.map((c, i) => (c.g !== g ? `<div class="mhdr">${esc(g = c.g)}</div>` : "") +
      `<button type="button" class="mi ${i === sel ? "act" : ""}" data-i="${i}">${icon(c.ic)}<span>${esc(c.label)}</span>${c.k ? `<span class="k">${esc(c.k)}</span>` : ""}</button>`).join("") || `<div class="empty">${te("app.pal.no_match")}</div>`;
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
  // key caps are key names (not translated); the descriptions come from app.help.*
  const K = (...k) => k.map(x => `<span class="kbd">${esc(x)}</span>`).join("");
  const H = (key) => te("app.help." + key);
  const plus = (key) => `<span class="faint t-xs" style="margin:0 2px">${te(key)}</span>`;
  const groups = [
    ["playback", [["play_pause", K("Space")], ["frame", K(",") + K(".")], ["seek1", K("←") + K("→")], ["seek5", K("Shift") + K("← →")]]],
    ["clip", [["set_inout", K("I") + K("O")], ["replay", K("R")], ["loop", K("L")], ["mark", K("M")]]],
    ["review", [["keep", K("Y")], ["false", K("X")], ["reset", K("U")], ["next_event", K("N") + K("P")]]],
    ["navigate", [["next_rec", K("J") + K("K")], ["search", K("/")], ["palette", K("Ctrl") + K("K")], ["ctabs", K("1") + K("2") + K("3")]]],
    ["queue", [["rec_top", K("T")], ["select_jobs", K("Shift") + K("Ctrl") + plus("app.help.plus_click")], ["jobs_updown", K("Alt") + K("↑") + K("↓")], ["jobs_topbottom", K("Home") + K("End")]]],
    ["panels", [["fold", K("Ctrl") + K("B")], ["close", K("Esc")], ["overlay", K("?")]]],
    ["zones", [["nudge", K("←") + K("↑") + K("→") + K("↓") + plus("app.help.plus") + K("Shift")], ["save", K("Ctrl") + K("S")], ["grab", K("Enter")], ["close_editor", K("Esc")], ["overlay", K("?")]]]];
  $("help-body").innerHTML = `<div class="keys">${groups.map(([g, ks]) => `<div><h6>${H("g." + g)}</h6>${ks.map(([l, k]) => `<div class="kr"><span>${H(l)}</span><span>${k}</span></div>`).join("")}</div>`).join("")}</div>
    <p class="faint t-xs" style="margin:16px 0 0">${te("app.help.note")}</p>`;
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
  if (k === "t") { if (window.QM) QM.recToTop(ST.sel); return; }   // the selected recording's work runs next
  if (ST.view !== "normal") return;
  // n / p would start the (hidden) player behind the calibration view; in Find clips they step the results
  if (k === "n" || k === "p") { if (findShown()) stepFound(k === "n" ? 1 : -1); else stepEvent(k === "n" ? 1 : -1); return; }
  if (k === "y") { verdictKey("kept"); return; }
  if (k === "x") { verdictKey("false"); return; }
  if (k === "u") { if (ST.event && ST.event.status !== "new") verdictKey("new"); return; }
  if (k === "1" || k === "2" || k === "3") { showCtab(CTABS[+k - 1]); return; }
}

boot().catch(err => { setStatus("boot failed: " + err.message); console.error(err); });
