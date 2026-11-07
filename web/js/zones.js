/* Zones editor: per-streamer detection zones (reward ticker, kill feed, money band, respawn centre)
   drawn as boxes over a sample frame of that streamer's footage. A full-page view in #main like the
   Calibration view. The boxes live in NORMALIZED frame coordinates (0..1, what core/zones.py stores):
   the overlay is positioned in % of a frame element sized to the fitted image, so it stays right when
   the window resizes, and a template drawn on a 1080p frame fits the same streamer's 1440p VOD.
   Drag / resize math reads the image's rendered box (getBoundingClientRect) at pointerdown.
   The other main-area elements are hidden with a class on #main (zones.css), not their `hidden` flag:
   the player / mixer keep toggling theirs while this view is open, so on close they are simply right.
   Backend: server/routes/zones.py. */
"use strict";

window.ZN = (() => {
  const ZONES = {
    ticker: { color: "#ff5a52", label: "ticker", tip: "reward ticker (KILL +$1,500, ASSIST ...): the lines under the money readout" },
    money: { color: "#ffd54a", label: "money", tip: "the money readout band at the top of the ticker; its bottom edge splits the readout from the ticker lines", opt: true },
    feed: { color: "#4ca7ef", label: "kill feed", tip: "the kill feed (rows with the [60 m] distance tag)" },
    center: { color: "#49c463", label: "center", tip: "where RESPAWNING shows (the death check reads this box)", opt: true },
  };
  const PAINT = ["center", "feed", "ticker", "money"];      // money is drawn inside (on top of) the ticker
  const HANDLES = ["nw", "n", "ne", "e", "se", "s", "sw", "w"];
  const MIN_PX = 12, MIN_MONEY_H = 6;                      // smallest box, in sample pixels
  const ARROWS = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };

  const S = {
    open: false, built: false, token: 0,
    streamers: [], cur: null,              // the chosen streamer row {provider, channel_key, channel_name, ...}
    templates: [], tpl: null,              // its versions; tpl = the one in the editor (null = new / built-in default)
    seed: "",                              // tpl null + seeded from another row's template (site-wide / pinned): its label
    recs: null, streamRecs: [],            // probed recordings (all) / of the chosen streamer
    originRec: null, forRec: null,         // the recording the view was opened from + /api/zones/for-recording
    z: null,                               // {ticker, feed, money, center: [x,y,w,h] | null, feed_row_h: n | null}
    meta: { name: "", eff: 0, note: "" },
    saved: "",                             // snapshot when loaded / saved -> the unsaved-changes state
    frame: { kind: "none", src: "", w: 0, h: 0, rec_id: null, t: null },   // w / h = footage resolution of the frame
    defaults: null, defKey: "",
    sel: null, drag: null, busy: false,
  };

  /* ---------------- small helpers ---------------- */
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const r6 = (v) => Math.round(v * 1e6) / 1e6;
  const enc = encodeURIComponent;
  const hasST = () => typeof ST !== "undefined" && ST;
  const view = () => $("zones-view");
  const isBox = (b) => Array.isArray(b) && b.length === 4 && b.every(v => typeof v === "number" && isFinite(v)) && b[2] > 0 && b[3] > 0;
  const vodProvider = (name) => ((name || "").match(/^VOD_([A-Za-z]+)_\d{4}-/) || [])[1] || null;
  const sameKey = (a, b) => String(a || "").toLowerCase() === String(b || "").toLowerCase();
  const asList = (r, key) => Array.isArray(r) ? r : (r && Array.isArray(r[key]) ? r[key] : []);
  // the shell's inline SVG icons (core.js); nothing when it is not there
  const ic = (n) => typeof icon === "function" ? icon(n) : "";
  const provCls = (p) => "prov p-" + String(p || "").toLowerCase().replace(/[^a-z0-9]/g, "");

  function recStartOf(r) {
    if (!r) return null;
    if (!r.name_utc) return r.mtime || null;
    return r.kind === "replay" && r.duration_s ? r.name_utc - r.duration_s : r.name_utc;
  }
  function tc(s) {
    s = Math.max(0, Math.round(s || 0)); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
    const p = (n) => String(n).padStart(2, "0");
    return h ? `${h}:${p(m)}:${p(x)}` : `${m}:${p(x)}`;
  }
  function parseT(v) {
    v = String(v || "").trim(); if (!v) return null;
    if (/^\d+(\.\d+)?$/.test(v)) return +v;
    const parts = v.split(":").map(Number);
    if (parts.some(n => isNaN(n))) return null;
    return parts.reduce((a, n) => a * 60 + n, 0);
  }
  function toLocalInput(utc) {
    if (!utc) return "";
    const d = new Date(utc * 1000); const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
  }
  function fromLocalInput(v) { if (!v) return 0; const t = new Date(v).getTime(); return isNaN(t) ? 0 : Math.floor(t / 1000); }
  function effLabel(utc) { return utc ? "from " + fmtDate(utc).slice(0, 10) : "from the start"; }
  function streamerName(s) { return s ? (s.channel_name || (s.channel_key === "*" ? `all ${s.provider}` : s.channel_key)) : ""; }

  // built-in Season 2 proportions (vision/hud.py) -- only used when /api/zones/default does not answer
  function localDefaults(W, H) {
    const s = H / 1440;
    const tw = Math.min(1, 540 * s / W), fw = Math.min(1, 620 * s / W), cw = Math.min(1, 1400 * s / W), ch = 440 / 1440;
    return { ticker: [1 - tw, 0, tw, 160 / 1440], feed: [0, 560 / 1440, fw, 300 / 1440],
      money: [1 - tw, 0, tw, 70 / 1440], center: [(1 - cw) / 2, (1 - ch) / 2, cw, ch], feed_row_h: 26 / 1440 };
  }
  function cleanZones(z) {
    if (typeof z === "string") { try { z = JSON.parse(z); } catch (e) { z = {}; } }
    z = z || {};
    const box = (b) => isBox(b) ? b.map(Number) : null;
    const rh = typeof z.feed_row_h === "number" && z.feed_row_h > 0 ? z.feed_row_h : null;
    return { ticker: box(z.ticker), feed: box(z.feed), money: box(z.money), center: box(z.center), feed_row_h: rh };
  }
  function payload() {
    const z = S.z || {}; const b = (v) => v ? v.map(r6) : null;
    // a row is never taller than the feed box (core/zones.validate refuses that)
    const rh = z.feed_row_h ? (z.feed ? Math.min(z.feed_row_h, z.feed[3]) : z.feed_row_h) : null;
    return { ticker: b(z.ticker), feed: b(z.feed), money: b(z.money), center: b(z.center), feed_row_h: rh ? r6(rh) : null };
  }
  // the page-side mirror of core/zones.validate (MIN_SIDE 0.005, EPS 1e-4): -> an error text or null
  const MIN_SIDE = 0.005, EPS = 1e-4;
  function checkZones(p) {
    for (const k of ["ticker", "feed", "money", "center"]) {
      const b = p[k];
      if (!b) { if (k === "ticker" || k === "feed") return `the ${ZONES[k].label} box is required`; continue; }
      if (!isBox(b)) return `${ZONES[k].label}: not a box`;
      if (!(b[2] > MIN_SIDE && b[3] > MIN_SIDE)) return `${ZONES[k].label}: too small`;
      if (b[0] < -EPS || b[1] < -EPS || b[0] + b[2] > 1 + EPS || b[1] + b[3] > 1 + EPS) return `${ZONES[k].label}: outside the frame`;
    }
    const m = p.money, t = p.ticker;
    if (m && (m[0] < t[0] - EPS || m[1] < t[1] - EPS || m[0] + m[2] > t[0] + t[2] + EPS || m[1] + m[3] > t[1] + t[3] + EPS))
      return "money: must lie inside the ticker box";
    if (p.feed_row_h != null && !(p.feed_row_h > 0 && p.feed_row_h <= p.feed[3] + EPS)) return "feed row: must be > 0 and no taller than the feed box";
    return null;
  }
  // keep feed_row_h between 4 px and the feed box's height (after a feed resize / nudge / number edit)
  function clampRow() {
    if (!S.z || !S.z.feed || !S.z.feed_row_h) return;
    const fh = S.z.feed[3];
    S.z.feed_row_h = clamp(S.z.feed_row_h, Math.min(4 / res().h, fh), fh);
  }
  // does a template belong to this streamer row (a site-wide '*' template or a pinned one of another row does not)
  const ownedBy = (t, row) => !!(t && row && String(t.provider || "").toLowerCase() === String(row.provider || "").toLowerCase() && sameKey(t.channel_key, row.channel_key));
  const seedLabel = (t) => t.channel_key === "*" ? `the ${t.provider}-wide template` : `"${t.name || "#" + t.id}" of ${t.provider} ${t.channel_name || t.channel_key}`;
  const snap = () => JSON.stringify({ z: payload(), m: S.meta });
  const isDirty = () => !!(S.open && S.z && snap() !== S.saved);

  function res() {
    if (S.frame.w > 0 && S.frame.h > 0) return { w: S.frame.w, h: S.frame.h };
    const img = $("zn-img");
    if (img && img.naturalWidth && !img.hidden) return { w: img.naturalWidth, h: img.naturalHeight };
    return S.cur && S.cur.provider === "OBS" ? { w: 3440, h: 1440 } : { w: 1920, h: 1080 };
  }

  /* ---------------- modal (version date, confirms) ---------------- */
  function ask(title, bodyHtml, buttons, onOpen) {
    let d = $("dlg-zn");
    if (!d) {
      d = document.createElement("dialog"); d.id = "dlg-zn"; d.className = "dlg zn-dlg";
      d.innerHTML = `<form method="dialog"><h3></h3><div class="zn-dlgbody"></div><div class="dlgacts"><span class="spacer"></span></div></form>`;
      document.body.appendChild(d);
    }
    d.querySelector("h3").textContent = title;
    d.querySelector(".zn-dlgbody").innerHTML = bodyHtml;
    d.querySelector(".dlgacts").innerHTML = `<span class="spacer"></span>` + buttons.map(([v, label, cls]) => `<button value="${esc(v)}" class="sm ${cls || ""}">${esc(label)}</button>`).join("");
    return new Promise((resolve) => {
      // Escape closes a <dialog> WITHOUT setting returnValue: reset it on every open so a stale answer never counts
      d.returnValue = "";
      d.addEventListener("close", () => resolve(d.returnValue || null), { once: true });
      try { d.showModal(); } catch (e) { resolve(null); return; }
      if (onOpen) onOpen(d);
    });
  }
  async function guard() {
    if (!isDirty()) return true;
    const v = await ask("Unsaved zone changes", `<div class="hint">The boxes of <b>${esc(S.meta.name || streamerName(S.cur))}</b> were changed and not saved.</div>`,
      [["cancel", "Keep editing"], ["discard", "Discard"], ["save", "Save", "acc"]]);
    if (v === "save") { await save(); return !isDirty(); }
    return v === "discard";
  }

  /* ---------------- DOM ---------------- */
  function build() {
    if (S.built && view()) return;
    let v = view();
    if (!v) { v = document.createElement("section"); v.id = "zones-view"; v.hidden = true; $("main").appendChild(v); }
    v.classList.add("zn");
    // the zone toggles: a swatch + the zone's name; the optional zones carry a "default" switch beside them
    const chip = (k) => `<button type="button" class="zn-chip" data-z="${k}" data-tip="${esc(ZONES[k].tip)}"><i style="background:${ZONES[k].color}"></i>${esc(ZONES[k].label)}</button>`;
    const defSw = (k, tip) => `<label class="zn-def" data-tip="${esc(tip)}"><input type="checkbox" class="toggle" data-def="${k}">default</label>`;
    v.innerHTML = `
      <aside class="zn-side">
        <div class="phd"><h2>Detection zones</h2><span class="sp"></span>
          <button type="button" class="ib sm" id="zn-more-lib" data-tip="Import / export templates">${ic("more")}</button>
          <input type="file" id="zn-file" accept=".json,application/json" hidden></div>
        <div class="zn-shead">Streamers<span class="cnt" id="zn-nstr"></span></div>
        <div class="zn-list" id="zn-streamers"><div class="empty">loading...</div></div>
        <div class="zn-shead">Versions<span class="sp"></span>
          <button type="button" class="btn sm ghost" id="zn-new" data-tip="A new template for this streamer, starting from the built-in boxes">${ic("plus")}New</button></div>
        <div class="zn-list zn-tpls" id="zn-tpls"></div>
        <div class="zn-sfoot">Boxes are stored per streamer, dated: a recording uses the newest version that started before it.</div>
      </aside>
      <div class="zn-ed">
        <div class="zn-head">
          <div class="zn-ttl">
            <div class="zn-l1"><span class="prov" id="zn-prov" hidden></span><h2 id="zn-title">Detection zones</h2><span class="badge warn" id="zn-dirty" hidden>unsaved</span></div>
            <div class="zn-src" id="zn-src"></div>
          </div>
          <span class="sp"></span>
          <div class="zn-acts">
            <button type="button" class="btn sm" id="zn-pin" hidden></button>
            <button type="button" class="btn sm" id="zn-redet" data-tip="Queue detect again for every recording of this streamer (uses the SAVED zones)">${ic("refresh")}Re-detect</button>
            <button type="button" class="btn sm" id="zn-ver" data-tip="Keep this version as it was saved and store these boxes as a NEW version from a date on (the stream layout changed)">Save as new version...</button>
            <button type="button" class="btn sm pri" id="zn-save" data-tip="Save this version" data-k="Ctrl S">${ic("check")}Save</button>
            <button type="button" class="ib" id="zn-more" data-tip="More: reset, delete, export">${ic("more")}</button>
            <button type="button" class="ib" id="zn-close" data-tip="Back to the player" data-k="Esc">${ic("x")}</button>
          </div>
        </div>
        <div class="zn-meta">
          <label class="zn-f"><span>Name</span><input type="text" class="inp sm" id="zn-name" placeholder="template name" autocomplete="off"></label>
          <label class="zn-f" data-tip="This version applies to recordings that START at / after this moment; empty = from the start (the earliest version also covers older recordings)"><span>Applies from</span><input type="datetime-local" class="inp sm" id="zn-eff" step="60"></label>
          <label class="zn-f zn-grow"><span>Note</span><input type="text" class="inp sm" id="zn-note" placeholder="e.g. new overlay since the 1.4 patch" autocomplete="off"></label>
        </div>
        <div class="zn-fbar">
          <span class="zn-lab">Frame</span>
          <select class="inp sm" id="zn-rec" data-tip="The recording the sample frame is grabbed from"></select>
          <input type="text" id="zn-t" class="inp sm mono" size="8" data-tip="h:mm:ss, m:ss or seconds (Enter = grab)">
          <input type="range" id="zn-tr" min="0" max="60" step="1" value="0" data-tip="Drag, release = grab that moment">
          <button type="button" class="btn sm" id="zn-grab" data-tip="Grab the frame at that moment of that recording" data-k="Enter">Grab</button>
          <button type="button" class="btn sm ghost" id="zn-tplframe" data-tip="Show the template's stored sample frame again" hidden>Sample</button>
        </div>
        <div class="zn-canvas" id="zn-canvas">
          <div class="zn-frame" id="zn-frame">
            <img id="zn-img" alt="" draggable="false" hidden>
            <div class="zn-fmsg" id="zn-fmsg"></div>
            <div class="zn-ghost" data-g="center" hidden></div><div class="zn-ghost" data-g="money" hidden></div>
            ${PAINT.map(k => `<div class="zn-box" data-z="${k}" style="--zc:${ZONES[k].color}" tabindex="-1" hidden><span class="zn-lbl"></span>${HANDLES.map(h => `<i class="zn-h zn-${h}" data-h="${h}"></i>`).join("")}</div>`).join("")}
          </div>
        </div>
        <div class="zn-tools">
          <div class="zn-grp">
            ${chip("ticker")}${chip("feed")}
            <span class="zn-pair">${chip("money")}${defSw("money", "Use the built-in band: the top 70/160 of the ticker")}</span>
            <span class="zn-pair">${chip("center")}${defSw("center", "Use the built-in 1400x440 (at 1440p) centre box")}</span>
          </div>
          <span class="zn-sep"></span>
          <div class="zn-grp">
            <label class="zn-f" data-tip="One kill-feed row's height (the dashed lines in the feed box)"><span>Feed row</span><input type="number" class="inp sm mono" id="zn-rowh" min="4" step="1"><span class="faint">px</span></label>
            <label class="zn-def" data-tip="Built-in: feed height x 26/300"><input type="checkbox" class="toggle" id="zn-rowdef">default</label>
          </div>
          <span class="zn-sep"></span>
          <span class="zn-num" id="zn-num" data-tip="The selected box in sample pixels (arrows nudge 1 px, shift 10 px)">
            <b id="zn-numk">-</b><span>x</span><input type="number" class="inp sm num" data-n="0"><span>y</span><input type="number" class="inp sm num" data-n="1"><span>w</span><input type="number" class="inp sm num" data-n="2"><span>h</span><input type="number" class="inp sm num" data-n="3"></span>
        </div>
        <div class="zn-foot"><span class="zn-hint" id="zn-hint"></span><span class="sp"></span>
          <span class="faint t-xs zn-keys"><span class="kbd">&larr;</span><span class="kbd">&rarr;</span> nudge 1 px, <span class="kbd">Shift</span> 10 px &middot; <span class="kbd">Ctrl S</span> save &middot; <span class="kbd">Esc</span> close</span></div>
      </div>`;
    wire();
    S.built = true;
  }

  function wire() {
    $("zn-close").onclick = () => close();
    $("zn-new").onclick = async () => { if (!S.cur || S.busy || !(await guard())) return; await chooseTemplate(null, { fresh: true }, S.token); };
    $("zn-save").onclick = () => save();
    $("zn-ver").onclick = () => saveVersion();
    $("zn-pin").onclick = () => pin();
    $("zn-redet").onclick = () => redetect();
    $("zn-more").onclick = (e) => { e.stopPropagation(); menu($("zn-more"), editorMenu()); };
    $("zn-more-lib").onclick = (e) => { e.stopPropagation(); menu($("zn-more-lib"), libraryMenu()); };
    $("zn-file").onchange = () => importFile();
    $("zn-name").oninput = () => { S.meta.name = $("zn-name").value; updateDirty(); };
    $("zn-note").oninput = () => { S.meta.note = $("zn-note").value; updateDirty(); };
    $("zn-eff").onchange = () => { S.meta.eff = fromLocalInput($("zn-eff").value); updateDirty(); };
    $("zn-rec").onchange = () => { const r = recById(+$("zn-rec").value); if (r) { setTimeCtl(r, suggestT(r)); grab(); } };
    $("zn-t").onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); grab(); } };
    $("zn-tr").oninput = () => { $("zn-t").value = tc(+$("zn-tr").value); };
    $("zn-tr").onchange = () => grab();
    $("zn-grab").onclick = () => grab();
    $("zn-tplframe").onclick = () => { if (S.tpl) showTemplateFrame(S.tpl); };
    view().querySelectorAll(".zn-chip").forEach(c => c.onclick = () => selectBox(c.dataset.z));
    view().querySelectorAll("input[data-def]").forEach(cb => cb.onchange = () => toggleDefault(cb.dataset.def, cb.checked));
    $("zn-rowdef").onchange = () => {
      if (!S.z) return;
      S.z.feed_row_h = $("zn-rowdef").checked ? null : rowDefault();
      syncCtl(); layout(); updateDirty();
    };
    $("zn-rowh").onchange = () => {
      const px = +$("zn-rowh").value; if (!S.z || !S.z.feed || !(px > 0)) { syncCtl(); return; }
      const h = res().h, maxPx = S.z.feed[3] * h;
      if (px > maxPx + 0.5) toast(`a feed row is at most the feed box's height (${Math.floor(maxPx)} px)`);
      S.z.feed_row_h = px / h; clampRow(); syncCtl(); layout(); updateDirty();
    };
    $("zn-num").querySelectorAll("input[data-n]").forEach(inp => inp.onchange = () => setFromNumbers());
    view().querySelectorAll(".zn-box").forEach(el => {
      el.addEventListener("pointerdown", onDown);
      el.addEventListener("pointermove", onMove);
      el.addEventListener("pointerup", onUp);
      el.addEventListener("pointercancel", onUp);
      el.addEventListener("lostpointercapture", onUp);
    });
    $("zn-frame").addEventListener("pointerdown", (e) => { if (e.target === $("zn-frame") || e.target === $("zn-img")) selectBox(null); });
    const img = $("zn-img");
    img.addEventListener("load", onImgLoad);
    img.addEventListener("error", onImgError);
    if (window.ResizeObserver) new ResizeObserver(() => { if (S.open) fit(); }).observe($("zn-canvas"));
    else window.addEventListener("resize", () => { if (S.open) fit(); });
  }

  // the "..." menus: the shell's popover menu (app.js openMenu), read at open time so the states are current
  function editorMenu() {
    return [
      { ic: "reset", label: "Reset to default", sub: "the built-in Season 2 boxes for this frame size", act: () => resetDefault(), disabled: !S.z },
      { ic: "download", label: "Export streamer", sub: "this streamer's templates, with their sample frames", act: () => exportTemplates(true), disabled: !S.templates.length },
      "-",
      { ic: "trash", label: "Delete version", sub: S.tpl ? `"${S.tpl.name || "#" + S.tpl.id}"` : "nothing saved to delete", act: () => deleteVersion(), danger: true, disabled: !S.tpl },
    ];
  }
  function libraryMenu() {
    return [
      { ic: "folder", label: "Import...", sub: "templates from a .json file exported here (or on another PC)", act: () => $("zn-file").click() },
      { ic: "download", label: "Export all", sub: "every template of every streamer, with their sample frames", act: () => exportTemplates(false) },
      { ic: "download", label: "Export this streamer", sub: "its templates, with their sample frames", act: () => exportTemplates(true), disabled: !S.templates.length },
    ];
  }
  function menu(anchor, items) {
    if (S.busy) { toast("wait for the save to finish"); return; }
    if (typeof openMenu === "function") openMenu(anchor, items);
    else toast("the menu did not load (app.js)");
  }

  /* ---------------- open / close ---------------- */
  async function open(opts = {}) {
    opts = opts || {};
    build();
    if (!S.open) {
      // the calibration view keeps #stage hidden by its flag: close it properly first
      if (hasST() && ST.view === "calibration" && typeof closeCalibration === "function") { try { closeCalibration(); } catch (e) { /* its view is replaced anyway */ } }
      if (window.PL && typeof PL.pause === "function") { try { PL.pause(); } catch (e) { /* no clip loaded */ } }
      S.open = true; S.recs = null; S.cur = null; S.tpl = null; S.z = null; S.saved = "";
      $("main").classList.add("zn-open"); view().hidden = false; $("main").scrollTop = 0;
      if (hasST()) ST.view = "zones";
      window.addEventListener("keydown", onKey, true);
      window.addEventListener("beforeunload", onUnload);
      if (window.PL && typeof PL.renderTimeline === "function") { try { PL.renderTimeline(); } catch (e) { /* hides #tl off the normal view */ } }
      S.templates = []; S.streamRecs = []; S.forRec = null; S.originRec = null;
      showFrame("none", "", 0, 0, null, null);                         // no stale frame from the last visit
      renderTemplates(); renderEditor();
    } else if (!(await guard())) return;
    const tok = ++S.token;
    S.originRec = opts.rec_id ? +opts.rec_id : null; S.forRec = null;
    await loadStreamers();
    if (tok !== S.token) return;
    if (S.originRec) {
      try { S.forRec = await api(`/api/zones/for-recording/${S.originRec}`); } catch (err) { toast("zones of this recording: " + err.message, 4000); }
      if (tok !== S.token) return;
      const f = S.forRec;
      if (f && f.provider) {
        const row = findStreamer(f.provider, f.channel_key) || addStreamer({ provider: f.provider, channel_key: f.channel_key || "*", channel_name: f.channel_name || "" });
        return chooseStreamer(row, { recId: S.originRec, tplObj: f.template || null, useDefault: !f.template });
      }
      // no answer about its zones: still open this recording's streamer, from the row itself
      await ensureRecs();
      if (tok !== S.token) return;
      const r = (S.recs || []).find(x => x.id === S.originRec);
      if (r) {
        const prov = r.kind === "vod" ? vodProvider(r.name) : "OBS", key = r.kind === "vod" ? (r.channel_key || "*") : "*";
        if (prov) return chooseStreamer(findStreamer(prov, key) || addStreamer({ provider: prov, channel_key: key, channel_name: r.channel || "" }), { recId: r.id });
      }
    }
    if (opts.provider) {
      const row = findStreamer(opts.provider, opts.channel_key || "*") || addStreamer({ provider: opts.provider, channel_key: opts.channel_key || "*", channel_name: "" });
      return chooseStreamer(row, {});
    }
    if (S.streamers.length) return chooseStreamer(S.streamers[0], {});
    renderAll();
  }

  async function close(force) {
    if (!S.open) return;
    if (!force && !(await guard())) return;
    S.open = false; S.drag = null; S.token++;
    view().hidden = true; $("main").classList.remove("zn-open");
    window.removeEventListener("keydown", onKey, true);
    window.removeEventListener("beforeunload", onUnload);
    if (hasST() && ST.view === "zones") ST.view = "normal";
    // as closeCalibration (app.js): mixer.js does not unhide #mixer while ST.view !== 'normal', so a clip
    // loaded while this view was open left the panel hidden -- show it again when there is a manifest
    if (window.MX && MX.state && MX.state.man && $("mixer")) { $("mixer").hidden = false; }
    if (window.MX && typeof MX.redraw === "function") { try { MX.redraw(); } catch (e) { /* no stems loaded */ } }
    if (typeof renderStage === "function") renderStage();
    if (typeof renderDetail === "function") renderDetail();
  }

  function onUnload(e) { if (isDirty()) { e.preventDefault(); e.returnValue = ""; } }

  function onKey(e) {
    if (!S.open) return;
    if (document.querySelector("dialog[open]")) return;                 // a modal owns the keyboard
    const dr = $("drawer"); if (dr && !dr.hidden) return;                 // app.js closes the settings drawer
    const t = e.target || {};
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName || "") || t.isContentEditable;
    if (e.key === "Escape") {
      const cm = $("ctx"); if (cm && !cm.hidden) return;                  // the context menu closes first
      const pp = $("pop"); if (pp && !pp.hidden) return;                  // so does a "..." menu / popover (app.js)
      e.preventDefault(); e.stopPropagation();
      if (typing && view().contains(t)) { t.blur(); return; }
      close();
      return;
    }
    if ((e.ctrlKey || e.metaKey) && !e.altKey && (e.key === "s" || e.key === "S")) {
      e.preventDefault(); e.stopPropagation();
      // a number / date field being typed into commits on change, which only fires on blur / Enter:
      // blur it first (that fires its change handler synchronously) so the save carries what is shown
      if (typing && view().contains(t) && typeof t.blur === "function") t.blur();
      save(); return;
    }
    if (typing || e.ctrlKey || e.altKey || e.metaKey) return;
    if (ARROWS[e.key]) {
      e.preventDefault(); e.stopPropagation();
      if (S.sel && S.z && S.z[S.sel]) nudge(S.sel, ARROWS[e.key], e.shiftKey ? 10 : 1);
      return;
    }
    if (e.key === "/") return;                                           // the search box stays reachable
    if (e.key === "?" && typeof openHelp === "function") { e.preventDefault(); e.stopPropagation(); openHelp(); return; }
    e.stopPropagation();                                                 // list / player shortcuts must not act behind the view
  }

  /* ---------------- data ---------------- */
  async function loadStreamers() {
    let list = [];
    try { list = asList(await api("/api/zones/streamers"), "streamers"); } catch (err) { toast("streamers: " + err.message, 4000); }
    list = list.filter(s => s && s.provider);
    list.sort((a, b) => (a.provider === "OBS" ? 0 : 1) - (b.provider === "OBS" ? 0 : 1));
    if (!list.some(s => s.provider === "OBS")) list.unshift({ provider: "OBS", channel_key: "*", channel_name: "My OBS recordings" });
    S.streamers = list;
    if (S.cur) S.cur = findStreamer(S.cur.provider, S.cur.channel_key) || addStreamer(S.cur);
    renderStreamers();
  }
  function findStreamer(provider, key) { return S.streamers.find(s => s.provider === provider && sameKey(s.channel_key, key)) || null; }
  function addStreamer(row) { S.streamers.push(row); return row; }

  async function loadTemplates(row) {
    try { return asList(await api(`/api/zones/templates?provider=${enc(row.provider)}&channel_key=${enc(row.channel_key)}`), "templates").filter(t => t && t.id != null); }
    catch (err) { if (!/not found/i.test(err.message)) toast("templates: " + err.message, 4000); return []; }
  }
  async function ensureRecs() {
    if (S.recs) return;
    try { S.recs = asList(await api("/api/recordings?limit=5000"), "recordings").filter(r => !r.missing && r.status === "probed" && r.width && r.height); }
    catch (err) { toast("recordings: " + err.message, 4000); S.recs = []; }
  }
  function recsOf(row) {
    const all = S.recs || [];
    if (row.provider === "OBS") return all.filter(r => r.kind !== "vod");
    return all.filter(r => r.kind === "vod" && vodProvider(r.name) === row.provider && (row.channel_key === "*" || sameKey(r.channel_key, row.channel_key)));
  }
  function recById(id) { return S.streamRecs.find(r => r.id === id) || (S.recs || []).find(r => r.id === id) || null; }
  async function loadDefaults(w, h) {
    const key = `${w}x${h}`;
    if (S.defaults && S.defKey === key) return S.defaults;
    let z = null;
    try { z = cleanZones((await api(`/api/zones/default?width=${w}&height=${h}`)).zones); } catch (err) { z = null; }
    const loc = localDefaults(w, h);
    if (!z || !z.ticker || !z.feed) z = cleanZones(loc);
    if (!z.center) z.center = loc.center;
    S.defaults = z; S.defKey = key;
    return z;
  }
  function defaultZones() {
    const d = S.defaults || cleanZones(localDefaults(res().w, res().h));
    return { ticker: d.ticker.slice(), feed: d.feed.slice(), money: null, center: null, feed_row_h: null };
  }

  /* ---------------- choosing ---------------- */
  async function chooseStreamer(row, want = {}) {
    const tok = ++S.token;
    S.cur = row; S.sel = null;
    renderStreamers();
    $("zn-tpls").innerHTML = `<div class="empty">loading...</div>`;
    const [list] = await Promise.all([loadTemplates(row), ensureRecs()]);
    if (tok !== S.token) return;
    S.templates = list;
    S.streamRecs = recsOf(row);
    if (want.recId && !S.streamRecs.some(r => r.id === want.recId)) {
      const r = (S.recs || []).find(x => x.id === want.recId); if (r) S.streamRecs.unshift(r);
    }
    let tpl = null;
    if (want.tplObj) tpl = S.templates.find(t => t.id === want.tplObj.id) || want.tplObj;
    else if (want.tplId != null) tpl = S.templates.find(t => t.id === want.tplId) || null;
    else if (!want.useDefault) tpl = S.templates[0] || null;            // newest version
    await chooseTemplate(tpl, want, tok);
  }

  async function chooseTemplate(tpl, want = {}, tok = S.token) {
    // a template of another row (the site-wide '*' one a VOD falls back to, or a pinned one of another streamer)
    // is never edited from this streamer's editor -- Save would PUT that other row's template. Start a NEW
    // unsaved template for this streamer, seeded with its boxes, instead.
    let seed = null;
    if (tpl && S.cur && !ownedBy(tpl, S.cur)) { seed = tpl; tpl = null; }
    S.tpl = tpl; S.sel = null; S.seed = seed ? seedLabel(seed) : "";
    S.meta = tpl ? { name: tpl.name || "", eff: tpl.effective_from || 0, note: tpl.note || "" }
      : { name: streamerName(S.cur), eff: 0, note: "" };
    const rec = (want.recId && recById(want.recId)) || (tpl && tpl.sample_rec_id && recById(tpl.sample_rec_id)) || S.streamRecs[0] || null;
    fillRecSelect(rec);
    if (tpl && tpl.sample_url && !want.recId) showTemplateFrame(tpl);
    else if (rec) showFrame("rec", frameUrl(rec.id, suggestT(rec)), rec.width, rec.height, rec.id, suggestT(rec));
    else showFrame("none", "", tpl && tpl.sample_w || 0, tpl && tpl.sample_h || 0, null, null);
    const r = res();
    await loadDefaults(r.w, r.h);
    if (tok !== S.token) return;
    const src = tpl || seed;
    const z = src ? cleanZones(src.zones) : null;
    if (z && (!z.ticker || !z.feed)) { const d = defaultZones(); z.ticker = z.ticker || d.ticker; z.feed = z.feed || d.feed; }
    S.z = z || defaultZones();
    if (S.z.money) S.z.money = containMoney(S.z.money);
    clampRow();
    S.saved = tpl ? snap() : ((want.fresh || seed) ? "" : snap());     // "New" / a seeded copy is unsaved from the start
    renderAll();
    if (seed) toast(`${streamerName(S.cur)} has no template of its own: starting from ${S.seed} - Save creates one for this streamer`, 6000);
  }

  function suggestT(rec) {
    const dur = rec.duration_s || 0;
    let t;
    if (hasST() && ST.event && ST.event.rec_id === rec.id && ST.event.t_s != null) t = ST.event.t_s + 0.3;
    else if (S.tpl && S.tpl.sample_rec_id === rec.id && S.tpl.sample_t != null) t = S.tpl.sample_t;
    else t = dur ? Math.round(dur * 0.3) : 60;
    return dur ? clamp(t, 0, Math.max(0, dur - 1)) : Math.max(0, t);
  }
  const frameUrl = (id, t) => `/api/recordings/${id}/frame?t=${(+t).toFixed(2)}&w=1920`;

  /* ---------------- frame ---------------- */
  function fillRecSelect(rec) {
    const sel = $("zn-rec");
    // the newest 400 + always the recording shown / opened from / holding the template's sample
    const list = S.streamRecs.slice(0, 400);
    const origin = S.originRec && S.streamRecs.find(r => r.id === S.originRec);
    for (const id of [rec && rec.id, origin && origin.id, S.tpl && S.tpl.sample_rec_id]) {
      if (id && !list.some(r => r.id === id)) { const r = recById(id); if (r) list.push(r); }
    }
    if (!list.length) {
      sel.innerHTML = `<option value="">no probed recordings of this streamer</option>`; sel.disabled = true;
      $("zn-t").value = ""; $("zn-tr").disabled = true; $("zn-grab").disabled = true;
      return;
    }
    sel.disabled = false; $("zn-tr").disabled = false; $("zn-grab").disabled = false;
    sel.innerHTML = list.map(r => `<option value="${r.id}">${esc(fmtDate(recStartOf(r)))}  ${esc(r.kind === "vod" && r.title ? r.title : r.name)}  (${r.width}x${r.height}, ${esc(fmtDur(r.duration_s))})</option>`).join("");
    if (rec) { sel.value = String(rec.id); setTimeCtl(rec, suggestT(rec)); }
  }
  function setTimeCtl(rec, t) {
    const tr = $("zn-tr");
    tr.max = String(Math.max(1, Math.floor(rec.duration_s || 60)));
    tr.value = String(Math.round(t || 0));
    $("zn-t").value = tc(t);
  }
  async function grab() {
    const rec = recById(+$("zn-rec").value);
    if (!rec) { toast("pick a recording of this streamer first"); return; }
    let t = parseT($("zn-t").value);
    if (t == null) { toast("time: h:mm:ss, m:ss or seconds"); return; }
    if (rec.duration_s) t = clamp(t, 0, Math.max(0, rec.duration_s - 0.5));
    setTimeCtl(rec, t);
    const tpl0 = S.tpl, fresh = !S.tpl && !isDirty();
    showFrame("rec", frameUrl(rec.id, t), rec.width, rec.height, rec.id, t);
    // an untouched built-in default follows the frame's aspect (the ticker width is normalized to the width)
    const tok = S.token;
    await loadDefaults(rec.width, rec.height);
    if (tok !== S.token || S.tpl !== tpl0) return;
    if (fresh && S.z) { S.z = defaultZones(); S.saved = snap(); }
    renderEditor();
  }
  function showTemplateFrame(tpl) {
    const u = tpl.sample_url + (tpl.sample_url.includes("?") ? "&" : "?") + "v=" + Math.round(tpl.updated_utc || 0);
    showFrame("tpl", u, tpl.sample_w || 0, tpl.sample_h || 0, tpl.sample_rec_id || null, tpl.sample_t);
    const r = tpl.sample_rec_id && recById(tpl.sample_rec_id);
    if (r) { $("zn-rec").value = String(r.id); setTimeCtl(r, tpl.sample_t || 0); }
  }
  function showFrame(kind, src, w, h, recId, t) {
    S.frame = { kind, src, w: w || 0, h: h || 0, rec_id: recId, t };
    const img = $("zn-img"), msg = $("zn-fmsg");
    if (!src) {
      img.hidden = true; img.removeAttribute("src");
      msg.textContent = S.cur ? "No sample frame: this streamer has no probed recording yet. The boxes can still be set on this blank frame." : "Pick a streamer on the left.";
      msg.hidden = false;
    } else {
      msg.textContent = "grabbing frame..."; msg.hidden = false; $("zn-frame").classList.add("loading");
      if (img.getAttribute("src") !== src) img.src = src;
      else if (img.complete) { if (img.naturalWidth) onImgLoad(); else onImgError(); }   // same frame again: no new load event
      // else: that load is still in flight and fires its own event
    }
    fit(); layout(); renderFrameNote();
  }
  function onImgLoad() {
    const img = $("zn-img");
    if (!S.frame.src || !img.naturalWidth) return;
    if (!S.frame.w || !S.frame.h) { S.frame.w = img.naturalWidth; S.frame.h = img.naturalHeight; }
    img.hidden = false; $("zn-fmsg").hidden = true; $("zn-frame").classList.remove("loading");
    fit(); layout(); syncCtl(); renderFrameNote();
  }
  function onImgError() {
    if (!S.frame.src) return;
    const img = $("zn-img"); img.hidden = true; $("zn-frame").classList.remove("loading");
    $("zn-fmsg").textContent = S.frame.kind === "tpl" ? "the template's sample frame is missing - grab a frame of a recording"
      : `could not grab a frame at ${tc(S.frame.t)} of recording #${S.frame.rec_id} (file missing or past the end?)`;
    $("zn-fmsg").hidden = false;
    fit(); layout();
  }
  function fit() {
    const c = $("zn-canvas"), f = $("zn-frame"), img = $("zn-img");
    if (!c || !f) return;
    const useImg = !img.hidden && img.naturalWidth;
    const nw = useImg ? img.naturalWidth : res().w, nh = useImg ? img.naturalHeight : res().h;
    const aw = c.clientWidth - 2, ah = c.clientHeight - 2;
    if (aw <= 0 || ah <= 0 || !nw || !nh) return;
    const s = Math.min(aw / nw, ah / nh);
    f.style.width = Math.max(1, Math.floor(nw * s)) + "px";
    f.style.height = Math.max(1, Math.floor(nh * s)) + "px";
  }
  function frameRect() {
    const img = $("zn-img");
    const el = !img.hidden && img.naturalWidth ? img : $("zn-frame");
    return el.getBoundingClientRect();
  }

  /* ---------------- boxes ---------------- */
  function boxEl(k) { return view().querySelector(`.zn-box[data-z="${k}"]`); }
  function place(el, b) {
    el.style.left = (b[0] * 100) + "%"; el.style.top = (b[1] * 100) + "%";
    el.style.width = (b[2] * 100) + "%"; el.style.height = (b[3] * 100) + "%";
  }
  function ghostMoney() { const t = S.z && S.z.ticker; return t ? [t[0], t[1], t[2], t[3] * 70 / 160] : null; }
  function ghostCenter() { return (S.defaults && S.defaults.center) || cleanZones(localDefaults(res().w, res().h)).center; }
  function rowDefault() { return S.z && S.z.feed ? S.z.feed[3] * 26 / 300 : 26 / 1440; }

  function layout() {
    if (!view()) return;
    const r = res();
    for (const k of PAINT) {
      const el = boxEl(k); const b = S.z && S.z[k];
      if (!el) continue;
      el.hidden = !b;
      if (!b) continue;
      place(el, b);
      el.classList.toggle("sel", S.sel === k);
      el.classList.toggle("lblin", b[1] < 0.05);               // no room above: the chip goes below the box
      el.classList.toggle("lblr", b[0] + b[2] > 0.75);          // right side: the chip hugs the right edge
      el.querySelector(".zn-lbl").textContent = `${ZONES[k].label} ${Math.round(b[2] * r.w)}x${Math.round(b[3] * r.h)}`;
      if (k === "feed") {
        const rh = S.z.feed_row_h || rowDefault();
        const pctH = clamp(rh / b[3] * 100, 0.5, 100);
        el.style.setProperty("--rowh", pctH + "%");
        el.classList.toggle("rowdef", !S.z.feed_row_h);
      }
    }
    const gm = view().querySelector('.zn-ghost[data-g="money"]'), gc = view().querySelector('.zn-ghost[data-g="center"]');
    const m = S.z && !S.z.money ? ghostMoney() : null, c = S.z && !S.z.center ? ghostCenter() : null;
    gm.hidden = !m; if (m) place(gm, m);
    gc.hidden = !c; if (c) place(gc, c);
    syncNumbers();
  }

  function selectBox(k) {
    if (k && S.z && !S.z[k] && ZONES[k] && ZONES[k].opt) toggleDefault(k, false);   // picking a defaulted zone = start editing it
    S.sel = k && S.z && S.z[k] ? k : null;
    view().querySelectorAll(".zn-chip").forEach(c => c.classList.toggle("on", c.dataset.z === S.sel));
    layout();
  }
  function toggleDefault(k, useDefault) {
    if (!S.z) return;
    if (useDefault) { S.z[k] = null; if (S.sel === k) S.sel = null; }
    else if (!S.z[k]) S.z[k] = k === "money" ? containMoney(ghostMoney() || [0, 0, 0.1, 0.03]) : ghostCenter().slice();
    syncCtl(); layout(); updateDirty();
  }

  function bounds(k) { return k === "money" && S.z.ticker ? S.z.ticker : [0, 0, 1, 1]; }
  // 0.006 > core/zones.MIN_SIDE (0.005): every box the page makes passes validate(); money is also capped by the
  // ticker it must lie in (a loaded ticker may be narrower than the page's own minimum, still > 0.005)
  function minWH(k) {
    const r = res();
    let mw = Math.max(0.006, MIN_PX / r.w), mh = Math.max(0.006, (k === "money" ? MIN_MONEY_H : MIN_PX) / r.h);
    if (k === "money" && S.z && S.z.ticker) { mw = Math.min(mw, S.z.ticker[2]); mh = Math.min(mh, S.z.ticker[3]); }
    return [mw, mh];
  }
  function moveBox(k, b0, dx, dy) {
    const [bx, by, bw, bh] = bounds(k);
    const w = Math.min(b0[2], bw), h = Math.min(b0[3], bh);
    return [clamp(b0[0] + dx, bx, bx + bw - w), clamp(b0[1] + dy, by, by + bh - h), w, h];
  }
  function resizeBox(k, b0, hd, dx, dy) {
    const [bx, by, bw, bh] = bounds(k); const [mw, mh] = minWH(k);
    let l = b0[0], t = b0[1], r = b0[0] + b0[2], b = b0[1] + b0[3];
    if (hd.includes("w")) l = clamp(l + dx, bx, r - mw);
    if (hd.includes("e")) r = clamp(r + dx, l + mw, bx + bw);
    if (hd.includes("n")) t = clamp(t + dy, by, b - mh);
    if (hd.includes("s")) b = clamp(b + dy, t + mh, by + bh);
    return [l, t, Math.max(mw, r - l), Math.max(mh, b - t)];
  }
  // the money band must stay inside the ticker: shrink it to fit, then clamp its position
  function containMoney(m) {
    const t = S.z && S.z.ticker; if (!t || !m) return m;
    const w = Math.min(m[2], t[2]), h = Math.min(m[3], t[3]);
    return [clamp(m[0], t[0], t[0] + t[2] - w), clamp(m[1], t[1], t[1] + t[3] - h), w, h];
  }
  // after the ticker moved / resized: money rides along with the ticker's move, then is contained
  function carryMoney(t0, m0) {
    if (!m0 || !S.z.ticker) return;
    const t = S.z.ticker;
    S.z.money = containMoney([m0[0] + (t[0] - t0[0]), m0[1] + (t[1] - t0[1]), m0[2], m0[3]]);
  }

  function onDown(e) {
    if (e.button !== 0 || !S.z) return;
    const el = e.currentTarget, k = el.dataset.z;
    if (!S.z[k]) return;
    e.preventDefault(); e.stopPropagation();
    if (S.sel !== k) selectBox(k);
    const rect = frameRect();
    if (!rect.width || !rect.height) return;
    S.drag = { k, hd: (e.target && e.target.dataset && e.target.dataset.h) || "", id: e.pointerId, x0: e.clientX, y0: e.clientY,
      rw: rect.width, rh: rect.height, b0: S.z[k].slice(), m0: S.z.money ? S.z.money.slice() : null, el, moved: false };
    try { el.setPointerCapture(e.pointerId); } catch (err) { /* capture is a nicety: moves still arrive while over the box */ }
    el.classList.add("drag");
  }
  function onMove(e) {
    const d = S.drag; if (!d || e.pointerId !== d.id || !S.z) return;
    const dx = (e.clientX - d.x0) / d.rw, dy = (e.clientY - d.y0) / d.rh;
    if (!d.moved && Math.abs(e.clientX - d.x0) < 2 && Math.abs(e.clientY - d.y0) < 2) return;
    d.moved = true;
    S.z[d.k] = d.hd ? resizeBox(d.k, d.b0, d.hd, dx, dy) : moveBox(d.k, d.b0, dx, dy);
    if (d.k === "ticker") carryMoney(d.b0, d.m0);
    if (d.k === "feed") clampRow();
    layout();
  }
  function onUp(e) {
    const d = S.drag; if (!d || (e.pointerId != null && e.pointerId !== d.id)) return;
    S.drag = null;
    d.el.classList.remove("drag");
    try { if (d.el.hasPointerCapture && d.el.hasPointerCapture(d.id)) d.el.releasePointerCapture(d.id); } catch (err) { /* already released */ }
    if (d.moved) { syncCtl(); updateDirty(); }
  }
  function nudge(k, [sx, sy], px) {
    const r = res(); const b0 = S.z[k].slice(), m0 = S.z.money ? S.z.money.slice() : null;
    S.z[k] = moveBox(k, b0, sx * px / r.w, sy * px / r.h);
    if (k === "ticker") carryMoney(b0, m0);
    if (k === "feed") clampRow();
    layout(); syncCtl(); updateDirty();
  }
  function setFromNumbers() {
    const k = S.sel; if (!k || !S.z || !S.z[k]) return;
    const r = res(); const v = [...$("zn-num").querySelectorAll("input[data-n]")].map(i => parseFloat(i.value));
    if (v.some(n => isNaN(n))) { syncNumbers(); return; }
    const [bx, by, bw, bh] = bounds(k); const [mw, mh] = minWH(k);
    const w = clamp(v[2] / r.w, mw, bw), h = clamp(v[3] / r.h, mh, bh);
    const b0 = S.z[k].slice(), m0 = S.z.money ? S.z.money.slice() : null;
    S.z[k] = [clamp(v[0] / r.w, bx, bx + bw - w), clamp(v[1] / r.h, by, by + bh - h), w, h];
    if (k === "ticker") carryMoney(b0, m0);
    if (k === "feed") clampRow();
    layout(); syncCtl(); updateDirty();
  }
  function syncNumbers() {
    const k = S.sel, b = k && S.z && S.z[k], r = res();
    $("zn-numk").textContent = b ? ZONES[k].label : "Select a box to edit its position";
    $("zn-num").classList.toggle("none", !b);
    $("zn-num").querySelectorAll("input[data-n]").forEach(inp => {
      inp.disabled = !b;
      if (document.activeElement === inp) return;
      const n = +inp.dataset.n;
      inp.value = b ? String(Math.round(b[n] * (n % 2 === 0 ? r.w : r.h))) : "";
    });
  }

  /* ---------------- render ---------------- */
  function renderAll() { renderStreamers(); renderTemplates(); renderEditor(); }

  function renderStreamers() {
    const el = $("zn-streamers"); if (!el) return;
    if ($("zn-nstr")) $("zn-nstr").textContent = S.streamers.length ? String(S.streamers.length) : "";
    if (!S.streamers.length) { el.innerHTML = `<div class="empty"><b>No streamers yet</b><small>Import a VOD first: its streamer shows up here.</small></div>`; return; }
    el.innerHTML = S.streamers.map((s, i) => {
      const on = S.cur && s.provider === S.cur.provider && sameKey(s.channel_key, S.cur.channel_key);
      const nt = s.n_templates || 0, nr = s.n_recordings || 0;
      const meta = [nt ? `${nt} version${nt === 1 ? "" : "s"}` : "built-in boxes", nr ? `${nr} rec` : ""].filter(Boolean).join(" · ");
      return `<div class="zn-item ${on ? "on" : ""}" data-i="${i}" data-tip="${esc(s.provider)} ${esc(s.channel_key)}">
        <span class="${provCls(s.provider)}">${esc(s.provider)}</span><span class="zn-nm">${esc(streamerName(s))}</span><span class="zn-ct">${esc(meta)}</span></div>`;
    }).join("");
    el.querySelectorAll(".zn-item").forEach(it => it.onclick = async () => {
      const s = S.streamers[+it.dataset.i]; if (!s || s === S.cur) return;
      // a save / import in flight reloads the current streamer when it answers: no switching meanwhile
      if (S.busy) { toast("wait for the save to finish"); return; }
      if (!(await guard()) || S.busy) return;
      chooseStreamer(s, {});
    });
  }

  function renderTemplates() {
    const el = $("zn-tpls"); if (!el) return;
    if (!S.cur) { el.innerHTML = ""; return; }
    const f = S.forRec, fid = f && f.template ? f.template.id : null;
    let html = S.templates.map(t => {
      const on = S.tpl && S.tpl.id === t.id;
      const used = fid === t.id ? `<span class="badge acc" data-tip="recording #${S.originRec} uses this version (${esc(f.source)})">${f.source === "pinned" ? "pinned here" : "this rec"}</span>` : "";
      return `<div class="zn-item ${on ? "on" : ""}" data-id="${t.id}"><span class="zn-nm">${esc(t.name || "(unnamed)")}</span>${used}<span class="zn-ct">${esc(effLabel(t.effective_from))}</span></div>`;
    }).join("");
    if (S.tpl && !S.templates.some(t => t.id === S.tpl.id)) {
      // a pinned / provider-wide template of another row: shown so it is clear what is being edited
      html = `<div class="zn-item on"><span class="zn-nm">${esc(S.tpl.name || "(unnamed)")}</span><span class="badge">${esc(S.tpl.provider)} ${esc(S.tpl.channel_key)}</span><span class="zn-ct">${esc(effLabel(S.tpl.effective_from))}</span></div>` + html;
    }
    if (!S.tpl) {
      const lab = S.seed ? `new template, starting from ${S.seed} (not saved yet)`
        : S.templates.length ? "new version (not saved yet)" : "default (built-in Season 2 boxes)";
      const tip = S.seed ? `a copy of ${S.seed}'s boxes for this streamer; Save stores it as this streamer's own template (the other one is left as it is)`
        : S.templates.length ? "starts from the built-in boxes; Save stores it" : "no template for this streamer yet: detection uses the built-in boxes; Save stores these as its first template";
      const thisRec = !S.seed && f && fid == null && f.source === "default";   // a seeded copy is not what the rec uses yet
      html = `<div class="zn-item on" data-tip="${esc(tip)}"><span class="zn-nm">${esc(lab)}</span>${thisRec ? `<span class="badge acc">this rec</span>` : ""}</div>` + html;
    }
    el.innerHTML = html;
    el.querySelectorAll(".zn-item[data-id]").forEach(it => it.onclick = async () => {
      const t = S.templates.find(x => x.id === +it.dataset.id); if (!t || t === S.tpl) return;
      if (S.busy) { toast("wait for the save to finish"); return; }
      if (!(await guard()) || S.busy) return;
      chooseTemplate(t, {}, S.token);
    });
  }

  function renderEditor() {
    if (!view()) return;
    const has = !!S.cur;
    $("zn-title").textContent = has ? streamerName(S.cur) : "Pick a streamer";
    const pv = $("zn-prov"); pv.hidden = !has; if (has) { pv.className = provCls(S.cur.provider); pv.textContent = S.cur.provider; }
    $("zn-name").value = S.meta.name || ""; $("zn-note").value = S.meta.note || ""; $("zn-eff").value = toLocalInput(S.meta.eff);
    ["zn-name", "zn-note", "zn-eff", "zn-save", "zn-ver", "zn-redet"].forEach(id => { $(id).disabled = !has; });
    $("zn-tplframe").hidden = !(S.tpl && S.tpl.sample_url);
    const pin = $("zn-pin");
    pin.hidden = !S.originRec;
    if (S.originRec) {
      // pinned to this version -- or to another row's template this editor only seeded a copy from (no version to
      // pin here then, so the button offers the unpin instead)
      const pinnedHere = S.forRec && S.forRec.source === "pinned" && S.forRec.template && (S.tpl ? S.forRec.template.id === S.tpl.id : !!S.seed);
      pin.textContent = pinnedHere ? "Unpin from this recording" : "Pin to this recording";
      pin.dataset.unpin = pinnedHere ? "1" : "";
      pin.disabled = !pinnedHere && !S.tpl;
      pin.dataset.tip = pinnedHere ? `recording #${S.originRec} goes back to its streamer's dated versions`
        : `recording #${S.originRec} always uses this version, whatever its date${S.tpl ? "" : " (save it first)"}`;
    }
    let src = "";
    if (S.originRec && S.forRec) {
      const f = S.forRec;
      src = `rec #${S.originRec} uses ${f.template ? `"${f.template.name || "#" + f.template.id}"` : "the built-in boxes"} (${f.source || "default"})`;
    }
    if (!S.tpl && S.seed) src = (src ? src + " - " : "") + `editing a new template starting from ${S.seed}`;
    $("zn-src").textContent = src;
    syncCtl(); fit(); layout(); renderFrameNote(); updateDirty();
  }

  function syncCtl() {
    if (!view()) return;
    const z = S.z, r = res();
    view().querySelectorAll("input[data-def]").forEach(cb => { cb.disabled = !z; cb.checked = !z || !z[cb.dataset.def]; });
    $("zn-rowdef").disabled = !z; $("zn-rowdef").checked = !z || !z.feed_row_h;
    if (document.activeElement !== $("zn-rowh")) $("zn-rowh").value = z ? String(Math.round((z.feed_row_h || rowDefault()) * r.h)) : "";
    $("zn-rowh").disabled = !z;
    view().querySelectorAll(".zn-chip").forEach(c => { c.classList.toggle("on", c.dataset.z === S.sel); c.classList.toggle("def", !!(z && !z[c.dataset.z])); });
    syncNumbers();
  }

  function renderFrameNote() {
    const h = $("zn-hint"); if (!h) return;
    const r = res(), f = S.frame;
    if (f.kind === "rec") h.textContent = `frame: rec #${f.rec_id} at ${tc(f.t)} (${r.w}x${r.h}) - Save stores it as the sample`;
    else if (f.kind === "tpl") h.textContent = `the template's sample frame (${r.w}x${r.h})`;
    else h.textContent = S.cur ? `blank ${r.w}x${r.h} frame` : "";
  }

  function updateDirty() {
    const d = isDirty();
    if ($("zn-dirty")) $("zn-dirty").hidden = !d;
    if ($("zn-save")) $("zn-save").classList.toggle("pri", d || (!S.tpl && !!S.cur));
  }

  /* ---------------- actions ---------------- */
  function sampleFields() {
    return S.frame.kind === "rec" && S.frame.rec_id ? { sample_rec_id: S.frame.rec_id, sample_t: Math.round((S.frame.t || 0) * 100) / 100 } : {};
  }
  async function busy(fn) {
    if (S.busy) return; S.busy = true;
    view().classList.add("busy");
    try { return await fn(); } catch (err) { toast(err.message || String(err), 5000); } finally { S.busy = false; view().classList.remove("busy"); }
  }
  // after a save: reload the lists and take the saved template as the editor's. tok = S.token when the request
  // went out; any await that comes back after a streamer switch / close / reopen stops here (the old streamer's
  // template must never land in another streamer's editor). sent = snap() when it went out: boxes moved while
  // the request was in flight stay in the editor (and stay unsaved) instead of being overwritten.
  async function reloadAfter(t, tok, sent) {
    const id = t && t.id != null ? t.id : (S.tpl && S.tpl.id);
    await loadStreamers();
    if (tok !== S.token || !S.open) return false;
    if (S.cur) {
      const list = await loadTemplates(S.cur);
      if (tok !== S.token || !S.open) return false;
      S.templates = list;
    }
    let forRec = null;
    if (S.originRec) {
      try { forRec = await api(`/api/zones/for-recording/${S.originRec}`); } catch (err) { /* keeps the old line */ }
      if (tok !== S.token || !S.open) return false;
    }
    if (forRec) S.forRec = forRec;
    const edited = sent != null && snap() !== sent;
    const keepZ = S.z, keepMeta = S.meta;
    const fresh = S.templates.find(x => x.id === id) || (t && t.id != null ? t : null);
    if (fresh) {
      S.tpl = fresh; S.seed = "";
      S.meta = { name: fresh.name || "", eff: fresh.effective_from || 0, note: fresh.note || "" };
      const z = cleanZones(fresh.zones);
      if (z.ticker && z.feed) S.z = z;
    }
    S.saved = snap();
    if (edited) { S.z = keepZ; S.meta = keepMeta; }
    renderAll();
    return true;
  }
  // the server stores the boxes even when the sample frame could not be grabbed: say so
  function sampleNote(t) { return t && t.sample_error ? ` - sample frame NOT stored: ${t.sample_error}` : ""; }
  function checked() {
    const p = payload(), err = checkZones(p);
    if (err) { toast(`not saved: ${err}`, 5000); return null; }
    return p;
  }

  async function save() {
    if (!S.open || !S.cur || !S.z) return;
    if (!S.z.ticker || !S.z.feed) { toast("the ticker and the kill feed boxes are required"); return; }
    const zones = checked(); if (!zones) return;
    await busy(async () => {
      const tok = S.token, sent = snap(), wasTpl = !!S.tpl;
      const body = Object.assign({ name: S.meta.name || streamerName(S.cur), effective_from: S.meta.eff || 0, zones, note: S.meta.note || "" }, sampleFields());
      let t;
      if (S.tpl) t = await put(`/api/zones/templates/${S.tpl.id}`, body);
      else t = await post("/api/zones/templates", Object.assign({ provider: S.cur.provider, channel_key: S.cur.channel_key, channel_name: S.cur.channel_name || "" }, body));
      const note = sampleNote(t);
      toast((wasTpl ? "zones saved - Re-detect applies them to recordings already scanned" : "template created") + note, note ? 8000 : 3500);
      await reloadAfter(t, tok, sent);
    });
  }

  async function saveVersion() {
    if (!S.cur || !S.z) return;
    const rec = S.frame.rec_id ? recById(S.frame.rec_id) : null;
    const def = recStartOf(rec) || Math.floor(Date.now() / 1000);
    const base = S.meta.name || streamerName(S.cur);
    const v = await ask("Save as a new version",
      `<div class="zn-form"><label>Applies from <input type="datetime-local" id="znv-eff" step="60" value="${esc(toLocalInput(def))}"></label>
       <label>Name <input type="text" id="znv-name" value="${esc(base)}" autocomplete="off"></label></div>
       <div class="hint">Recordings that start at / after this moment use the new version; older ones keep ${S.tpl ? `"${esc(S.tpl.name || "the current version")}"` : "the earlier versions"}.
       Default: the start of the frame's recording${rec ? ` (#${rec.id})` : ""}.${S.tpl ? " The current version stays as it was last saved." : ""}</div>`,
      [["cancel", "Cancel"], ["go", "Create version", "acc"]], (d) => { const i = d.querySelector("#znv-eff"); if (i) i.focus(); });
    if (v !== "go") return;
    const eff = fromLocalInput(($("znv-eff") || {}).value);
    const name = (($("znv-name") || {}).value || base).trim();
    if (!S.open || !S.cur || !S.z) return;
    const zones = checked(); if (!zones) return;
    await busy(async () => {
      const tok = S.token, sent = snap();
      let t;
      if (S.tpl) {
        // ONE request: the version is created with these boxes / name / note (the route validates them), so a
        // failure leaves nothing behind -- a create-then-PUT left a stale dated copy when the PUT failed
        t = await post(`/api/zones/templates/${S.tpl.id}/version`,
          Object.assign({ effective_from: eff, name, zones, note: S.meta.note || "" }, sampleFields()));
        if (!t || t.id == null) throw new Error("the server did not return the new version");
      } else {
        t = await post("/api/zones/templates", Object.assign({ provider: S.cur.provider, channel_key: S.cur.channel_key, channel_name: S.cur.channel_name || "",
          name, effective_from: eff, zones, note: S.meta.note || "" }, sampleFields()));
      }
      const note = sampleNote(t);
      toast(`new version ${effLabel(eff)}${note}`, note ? 8000 : 3000);
      await reloadAfter(t, tok, sent);
    });
  }

  async function deleteVersion() {
    if (!S.tpl) return;
    const v = await ask("Delete this version?", `<div class="hint">"${esc(S.tpl.name || "#" + S.tpl.id)}" (${esc(effLabel(S.tpl.effective_from))}) is deleted;
      recordings pinned to it go back to their streamer's versions. Recordings already scanned keep their events until they are detected again.</div>`,
      [["cancel", "Cancel"], ["del", "Delete", "zn-danger"]]);
    if (v !== "del") return;
    await busy(async () => {
      const tok = S.token;
      await del(`/api/zones/templates/${S.tpl.id}`);
      toast("version deleted");
      if (tok !== S.token || !S.open) return;
      S.saved = snap();                                // nothing left to save
      const row = S.cur;
      await loadStreamers();
      if (tok !== S.token || !S.open) return;
      if (S.originRec) {
        let f = null; try { f = await api(`/api/zones/for-recording/${S.originRec}`); } catch (err) { f = null; }
        if (tok !== S.token || !S.open) return;
        S.forRec = f;
      }
      await chooseStreamer(findStreamer(row.provider, row.channel_key) || addStreamer(row), { recId: S.frame.kind === "rec" ? S.frame.rec_id : undefined });
    });
  }

  async function resetDefault() {
    if (!S.z) return;
    const r = res(), tok = S.token;
    await loadDefaults(r.w, r.h);
    if (tok !== S.token || !S.z) return;
    S.z = defaultZones(); S.sel = null;
    syncCtl(); layout(); updateDirty();
    toast("built-in boxes loaded - Save to keep them");
  }

  async function pin() {
    if (!S.originRec) return;
    const unpin = $("zn-pin").dataset.unpin === "1";
    if (!unpin && !S.tpl) return;
    if (!unpin && isDirty()) { toast("save the version first: a pinned recording uses the saved zones"); return; }
    await busy(async () => {
      const tok = S.token, rid = S.originRec, tpl = S.tpl;
      await post(`/api/recordings/${rid}/zone-template`, { template_id: unpin ? null : tpl.id });
      toast(unpin ? `recording #${rid} unpinned` : `recording #${rid} pinned to "${tpl.name || "#" + tpl.id}" - re-detect it to apply`, 3500);
      let f = null; try { f = await api(`/api/zones/for-recording/${rid}`); } catch (err) { /* keep the old line */ }
      if (tok !== S.token || !S.open) return;
      if (f) S.forRec = f;
      renderTemplates(); renderEditor();
    });
  }

  async function redetect() {
    if (!S.cur) return;
    if (isDirty()) { toast("save first: re-detect uses the saved zones", 3500); return; }
    const n = S.cur.n_recordings;
    const v = await ask("Re-detect this streamer", `<div class="hint">Queue detect again for ${n != null ? `the ${n} recording${n === 1 ? "" : "s"}` : "every probed recording"} of
      <b>${esc(streamerName(S.cur))}</b> (${esc(S.cur.provider)}), ahead of the normal queue. Your keep / false-positive verdicts and notes carry over.</div>`,
      [["cancel", "Cancel"], ["go", "Queue detect", "acc"]]);
    if (v !== "go") return;
    await busy(async () => {
      const r = await post("/api/zones/redetect", { provider: S.cur.provider, channel_key: S.cur.channel_key });
      toast(`queued detect for ${r && r.queued != null ? r.queued : "?"} recording${r && r.queued === 1 ? "" : "s"}`, 3500);
      if (typeof loadJobs === "function" && hasST() && ST.tab === "jobs") { try { loadJobs(); } catch (err) { /* the SSE feed refreshes it */ } }
    });
  }

  function exportTemplates(streamerOnly) {
    let url = "/api/zones/export?samples=1";
    if (streamerOnly) {
      if (!S.templates.length) { toast("this streamer has no saved templates"); return; }
      url += "&ids=" + S.templates.map(t => t.id).join(",");
    }
    const a = document.createElement("a");
    a.href = url; a.download = ""; a.rel = "noopener";
    document.body.appendChild(a); a.click(); a.remove();
  }

  async function importFile() {
    const inp = $("zn-file"); const file = inp.files && inp.files[0];
    inp.value = "";                                                    // the same file can be picked again
    if (!file) return;
    let data;
    try { data = JSON.parse(await file.text()); } catch (err) { toast(`${file.name}: not a JSON file`, 5000); return; }
    const list = data && Array.isArray(data.templates) ? data.templates : null;
    if (!list || data.format !== "wardogclips-zone-templates") { toast(`${file.name}: not a WardogClips zone-template export`, 5000); return; }
    const names = [...new Set(list.map(t => `${t.provider || "?"} ${t.channel_name || t.channel_key || "?"}`))];
    const v = await ask("Import zone templates", `<div class="hint"><b>${list.length}</b> template${list.length === 1 ? "" : "s"} in ${esc(file.name)}:
      ${esc(names.slice(0, 8).join(", "))}${names.length > 8 ? ` and ${names.length - 8} more` : ""}.</div>
      <div class="hint">Overwrite existing versions? A version already here (same streamer and date) is replaced with "Overwrite", kept with "Keep mine".</div>`,
      [["cancel", "Cancel"], ["keep", "Keep mine"], ["over", "Overwrite", "acc"]]);
    if (v !== "keep" && v !== "over") return;
    await busy(async () => {
      const tok = S.token;
      const r = await post("/api/zones/import", { data, overwrite: v === "over" }) || {};
      // core/zones.import_: errors = a COUNT, messages = the texts ("#3 (Twitch foo): ticker: too small")
      const msgs = Array.isArray(r.messages) ? r.messages : (Array.isArray(r.errors) ? r.errors : []);
      const nerr = typeof r.errors === "number" ? r.errors : msgs.length;
      const first = msgs.slice(0, 2).map(String).join("; ");
      toast(`imported: ${r.added || 0} added, ${r.updated || 0} updated, ${r.skipped || 0} skipped`
        + (nerr ? `, ${nerr} error${nerr === 1 ? "" : "s"}${first ? `: ${first}${msgs.length > 2 ? ` (+${msgs.length - 2} more in the console)` : ""}` : ""}` : ""),
        nerr ? 10000 : 3500);
      if (nerr) console.warn("[Zones] import errors", nerr, msgs);
      if (tok !== S.token || !S.open) return;
      await loadStreamers();
      if (tok !== S.token || !S.open) return;
      if (!S.cur) { if (S.streamers.length) await chooseStreamer(S.streamers[0], {}); return; }
      const list = await loadTemplates(S.cur);
      if (tok !== S.token || !S.open) return;
      S.templates = list;
      // an untouched editor shows what was imported for this streamer; unsaved edits are left alone
      if (!isDirty()) {
        const keep = S.tpl && S.templates.find(t => t.id === S.tpl.id);
        await chooseTemplate(keep || S.templates[0] || null, { recId: S.frame.kind === "rec" ? S.frame.rec_id : undefined }, tok);
      } else renderTemplates();
    });
  }

  return {
    open, close,
    isOpen: () => S.open,
    isDirty,
  };
})();
