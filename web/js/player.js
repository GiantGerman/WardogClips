/* Clip player -- the main view of the page (the evidence crops live in the Event tab, app.js).
   Header: Clip | Full recording, a short title (window + height; the render details in its tooltip), Send to Resolve
   (app.js). Under the video: the scrub bar (in / out shading, the event, the other events, played part, hover time),
   a compact transport (frame step, play, In / Out timecodes in #pl-in-t / #pl-out-t, the time) and the clip tools
   popover #pl-tools (replay, loop, reset, wider, mark here, quality; app.js opens / closes it).
   - Clip mode: the selected event's window (in / out + handles) is rendered on demand by the
     server (media/clips.py, cached under the data dir) and plays from the in point, i.e. the
     pre-roll. Loop plays in -> out; set in / out store the clip bounds on the event.
   - Full recording mode: the whole-recording proxy (a `proxy` job builds it).
   - The timeline strip under the stage shows every event of the recording; click a tick to
     select it, click anywhere else to look at that moment.
   Times inside the player are always absolute seconds into the recording ("abs"). */
"use strict";

const PL = (() => {
  const v = $("pl-video");
  const LS = (k, d) => { try { const x = localStorage.getItem("wclips." + k); return x == null ? d : JSON.parse(x); } catch (e) { return d; } };
  const LSset = (k, x) => { try { localStorage.setItem("wclips." + k, JSON.stringify(x)); } catch (e) { } };
  const S = {
    rec: null, mode: "clip", kind: null,       // kind: event | free | full | null
    ev: null, url: null, w0: 0, w1: 0, inT: null, outT: null, t: null,
    loop: LS("loop", true), h: LS("clipH", 720), token: 0, loading: false, pendingSeek: null, autoplay: false,
    raf: 0, over: null, drag: false,
    loadedEv: null,     // the event whose media is in the <video> (in / out may only be set against it)
    prevA: null,        // last painted playhead: loop fires on crossing the out point, never on a seek past it
  };
  const FRAME = 1 / 60;
  // which encoder rendered the clip (media/encoder.py: x264 while OBS holds the GPU's NVENC engine)
  const encLabel = (r) => r.encoder ? ` (${r.encoder === "libx264" ? "x264" : "NVENC"})` : "";

  const fmt = (s, dec = 2) => {
    if (s == null || isNaN(s)) return "-";
    s = Math.max(0, s);
    const f = Math.pow(10, dec); s = Math.round(s * f) / f;            // 59.996 -> 1:00.00, never 0:60.00
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s - h * 3600 - m * 60;
    const ss = sec.toFixed(dec).padStart(dec ? 3 + dec : 2, "0");
    return h ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
  };
  const fmt1 = (s) => fmt(s, 1);                                     // the transport: 0:56.5
  const dur = () => (S.rec && S.rec.duration_s) || 0;
  const abs = () => (S.kind === "full" ? v.currentTime : S.w0 + v.currentTime);
  const span = () => (S.kind === "full" ? [0, dur()] : [S.w0, S.w1]);
  const hasProxy = () => !!(S.rec && S.rec.proxy_path && S.rec.stages && S.rec.stages.proxy && S.rec.stages.proxy.state === "done");
  const preRoll = () => (ST.settings && ST.settings.highlights ? ST.settings.highlights.pre_roll_s : 8);
  const postRoll = () => (ST.settings && ST.settings.highlights ? ST.settings.highlights.post_roll_s : 4);
  // the player is hidden while the zones editor or the calibration view is open: a click there (Events tab,
  // clip search, a transcript line) may load a clip, but it must never start playing (with sound) unseen
  const onView = () => typeof ST === "undefined" || ST.view === "normal";

  /* ---------- overlay ---------- */
  function overlay(html) {
    const o = $("pl-over");
    if (S.over) { clearInterval(S.over); S.over = null; }
    if (!html) { o.hidden = true; return; }
    o.hidden = false; o.innerHTML = html;
  }
  // a centred card on the dimmed video: a bold title, a line of text, optional actions
  const card = (title, text, acts) => `<div class="empty plcard"><b>${title}</b>${text || ""}${acts ? `<div class="acts">${acts}</div>` : ""}</div>`;
  function busy(msg) {
    const t0 = performance.now();
    overlay(`<div class="plbusy"><div class="spin"></div><div id="pl-busy">${esc(msg)}</div><small class="faint" id="pl-busy-t"></small></div>`);
    S.over = setInterval(() => { const el = $("pl-busy-t"); if (el) el.textContent = ((performance.now() - t0) / 1000).toFixed(0) + " s"; }, 500);
  }

  // the header title: what is in the player (window + height); the render details go in its tooltip
  function title(text, tip) {
    const el = $("pl-title"); el.textContent = text;
    if (tip) el.dataset.tip = tip; else delete el.dataset.tip;
  }
  const renderTip = (r) => `${r.height}p ${r.cached ? "from the clip cache" : `rendered in ${r.render_s} s${encLabel(r)}`}`;
  const errActs = `<button type="button" class="btn sm pri" id="pl-retry">Try again</button><button type="button" class="btn sm ghost" id="pl-enc">Encoding settings</button>`;
  function wireErr(retry) {
    const b = $("pl-retry"); if (b) b.onclick = retry;
    const s = $("pl-enc"); if (s) s.onclick = () => { if (typeof openSettings === "function") openSettings("encoding"); };
  }

  /* ---------- loading ---------- */
  // every path that takes over the <video> bumps the token, so a slower render still in flight
  // cannot replace what the user switched to meanwhile
  function setSrc(url, seekAbs, autoplay) {
    S.token++; S.loading = false;
    autoplay = !!autoplay && onView();
    S.autoplay = autoplay;
    if (S.url === url && v.readyState >= 1) { seek(seekAbs); if (autoplay) v.play().catch(() => { }); overlay(null); return; }
    S.url = url; S.pendingSeek = seekAbs;
    v.src = url; v.load();
    loadStems();
  }

  // the track panel (mixer.js) follows whatever the <video> now shows: the clip's window, or the whole recording
  function loadStems() {
    if (!window.MX || !S.rec) return;
    const rid = S.rec.id;
    if (S.kind === "full") { MX.loadFull(rid, () => S.kind === "full" && S.rec && S.rec.id === rid); return; }
    const w0 = S.w0, w1 = S.w1;
    MX.loadWindow(rid, w0, w1, () => !!(S.rec && S.rec.id === rid && S.kind && S.kind !== "full" && S.w0 === w0 && S.w1 === w1));
  }

  async function playEvent(e, opts = {}) {
    if (!e) return;
    const autoplay = opts.autoplay !== false;
    S.ev = e; S.t = e.t_s;
    S.inT = e.clip_in != null ? e.clip_in : Math.max(0, e.t_s - preRoll());
    S.outT = e.clip_out != null ? e.clip_out : Math.min(dur() || 1e9, e.t_s + postRoll());
    if (S.mode === "full" && hasProxy()) {
      S.kind = "full"; S.loadedEv = e.id;
      setSrc(`/api/recordings/${e.rec_id}/proxy`, S.inT, autoplay);
      modeButtons(); paint(); return;
    }
    const tok = ++S.token; S.loading = true; S.loadedEv = null; modeButtons();
    busy(`rendering the clip (${S.h}p)...`);
    title(`${KIND_NAME[e.kind] || e.kind} at ${tc(e.t_s)} \u00b7 rendering ${S.h}p`);
    try {
      const r = await api(`/api/events/${e.id}/clip?h=${S.h}&handles=${opts.handles || 5}`);
      if (tok !== S.token) return;
      S.kind = "event"; S.w0 = r.t0; S.w1 = r.t1; S.inT = r.in; S.outT = r.out; S.t = r.t; S.loadedEv = e.id;
      title(`${tc(r.t0)} \u2013 ${tc(r.t1)} \u00b7 ${r.height}p`, `${KIND_NAME[e.kind] || e.kind} at ${tc(e.t_s)}: ${renderTip(r)}`);
      setSrc(r.url, S.inT, autoplay);
    } catch (err) {
      if (tok !== S.token) return;
      S.kind = null; S.url = null; v.removeAttribute("src"); v.load();
      if (window.MX) MX.clear(null);
      overlay(card("Could not render this clip", `<small class="plerr">${esc(err.message)}</small>`, errActs));
      wireErr(() => playEvent(e, opts));
    } finally { if (tok === S.token) S.loading = false; modeButtons(); paint(); }
  }

  async function playAt(recId, t, opts = {}) {
    if (!S.rec || S.rec.id !== recId) return;
    t = Math.max(0, Math.min(t, dur()));
    const autoplay = opts.autoplay !== false;
    if (S.kind === "full" || (S.mode === "full" && hasProxy())) {
      S.kind = "full"; setSrc(`/api/recordings/${recId}/proxy`, t, autoplay); paint(); return;
    }
    if (S.url && S.kind && t >= S.w0 && t <= S.w1 - 0.2) { S.token++; S.loading = false; overlay(null); seek(t); if (autoplay && onView()) v.play().catch(() => { }); return; }
    const tok = ++S.token; S.loading = true;
    busy(`rendering ${tc(t)} (${S.h}p)...`);
    try {
      const r = await api(`/api/recordings/${recId}/clip?t0=${Math.max(0, Math.floor(t - 5))}&t1=${Math.ceil(t + 15)}&h=${S.h}`);
      if (tok !== S.token) return;
      S.kind = "free"; S.ev = null; S.inT = null; S.outT = null; S.t = null; S.w0 = r.t0; S.w1 = r.t1; S.loadedEv = null;
      title(`${tc(r.t0)} \u2013 ${tc(r.t1)} \u00b7 ${r.height}p`, `around ${tc(t)}: ${renderTip(r)}`);
      setSrc(r.url, t, autoplay);
    } catch (err) {
      if (tok !== S.token) return;
      overlay(card(`Could not render ${tc(t)}`, `<small class="plerr">${esc(err.message)}</small>`, errActs));
      wireErr(() => playAt(recId, t, opts));
    } finally { if (tok === S.token) S.loading = false; modeButtons(); paint(); }
  }

  function seek(a) {
    const [s0, s1] = span();
    a = Math.max(s0, Math.min(a, s1 - 0.02));
    v.currentTime = S.kind === "full" ? a : a - S.w0;
    S.prevA = a;
    paint();
  }

  // in / out are only stored against the event whose media is actually loaded
  const canEdit = () => !!(S.ev && S.loadedEv === S.ev.id && S.url && S.kind && !S.loading);
  // In / Out button: the timecode goes into its .tc span (empty = hidden by CSS); .on = set by hand on this event
  function setTc(id, text, on) {
    const t = $(id + "-t"); if (t && t.textContent !== text) t.textContent = text;
    $(id).classList.toggle("on", !!on);
  }

  /* ---------- the selected event went away / was replaced (re-detect, re-transcribe, delete) ---------- */
  function clearEvent() {
    S.ev = null; S.inT = S.outT = S.t = null; S.loadedEv = null;
    if (S.kind === "event") S.kind = "free";
    modeButtons(); paint(); renderTimeline();
  }
  function syncEvent(oldId, fresh) {
    if (!S.ev || S.ev.id !== oldId) return;
    S.ev = fresh; S.t = fresh.t_s;
    if (S.loadedEv === oldId) S.loadedEv = fresh.id;
    S.inT = fresh.clip_in != null ? fresh.clip_in : Math.max(0, fresh.t_s - preRoll());
    S.outT = fresh.clip_out != null ? fresh.clip_out : Math.min(dur() || 1e9, fresh.t_s + postRoll());
    modeButtons(); paint();
  }

  /* ---------- recording switch ---------- */
  function setRecording(rec) {
    const changed = !S.rec || S.rec.id !== rec.id;
    S.rec = rec;
    if (changed) {
      S.token++; S.loading = false; S.kind = null; S.ev = null; S.url = null; S.inT = S.outT = S.t = null;
      S.w0 = S.w1 = 0; S.loadedEv = null; S.prevA = null;
      v.removeAttribute("src"); v.load();
      if (window.MX) MX.clear(null);
      title("no clip", rec.name);
      if (S.mode === "full") showFull(); else overlay(card("Pick an event", "Choose one in Events (or press N): its clip plays here from the pre-roll.<small>Or click the timeline below to look at any moment of this recording.</small>"));
    } else if (S.mode === "full" && hasProxy() && S.kind !== "full") {
      // a proxy just became available; a clip playing because there was none is left alone otherwise
      showFull();
    }
    modeButtons(); renderTimeline();
  }

  function showFull() {
    if (!S.rec) return;
    S.token++; S.loading = false;
    if (hasProxy()) {
      const at = S.kind && S.url ? abs() : (S.inT != null ? S.inT : 0);
      S.kind = "full"; S.url = null; overlay(null);
      setSrc(`/api/recordings/${S.rec.id}/proxy`, at, false);
      title(`Proxy ${ST.settings ? ST.settings.proxies.height : 536}p \u00b7 whole recording`, `${S.rec.name}: the whole-recording proxy`);
      return;
    }
    // no proxy: drop the clip too, so the next event / timeline click loads afresh (setSrc's same-URL
    // shortcut would otherwise keep the clip playing with the track panel hidden)
    S.kind = null; S.url = null; v.removeAttribute("src"); v.load();
    if (window.MX) MX.clear(null);
    title("No proxy yet", `${S.rec.name}: no whole-recording proxy yet`);
    const jp = ST.jobprog[S.rec.id];
    const queued = (S.rec.jobs || []).some(j => j.stage === "proxy" && (j.state === "queued" || j.state === "running"));
    const ph = ST.settings ? ST.settings.proxies.height : 536;
    const building = !!(jp && jp.stage === "proxy");
    overlay(card("No proxy for this recording yet",
      `A ${ph}p proxy is a copy of the whole ${fmtDurShort(dur())} file for scrubbing it end to end (several times realtime).`
      + `<div class="plprog"${building ? "" : " hidden"}><span class="badge acc" id="pl-pxmsg">${building ? `${esc(jp.message || "building")} ${pct(jp.progress)}` : ""}</span><div class="pg"><i id="pl-pxbar" style="width:${building ? Math.round((jp.progress || 0) * 100) : 0}%"></i></div></div>`,
      (building ? "" : queued ? `<span class="badge" id="pl-pxq">queued</span>` : `<button type="button" class="btn sm pri" id="pl-mkproxy">Build proxy</button>`)
      + `<button type="button" class="btn sm" id="pl-allproxy" data-tip="Queue proxies for every recording with events">Proxies for all with events</button>`));
    const b = $("pl-mkproxy"); if (b) b.onclick = () => post(`/api/recordings/${S.rec.id}/proxy`).then(() => { toast("proxy queued"); b.outerHTML = `<span class="badge" id="pl-pxq">queued</span>`; }).catch(err => toast(err.message, 5000));
    $("pl-allproxy").onclick = () => post("/api/proxies/build-all").then(r => toast(`queued ${r.queued} proxies (${r.candidates} recordings with events lack one)`)).catch(err => toast(err.message, 5000));
    if (S.over) clearInterval(S.over);
    S.over = setInterval(() => {
      const j = ST.jobprog[S.rec && S.rec.id];
      if (!(j && j.stage === "proxy" && S.mode === "full" && !hasProxy())) return;
      const box = $("pl-over").querySelector(".plprog"); if (box) box.hidden = false;
      const q = $("pl-pxq"); if (q) q.remove();
      const mk = $("pl-mkproxy"); if (mk) mk.remove();
      const m = $("pl-pxmsg"); if (m) m.textContent = `${j.message || "building"} ${pct(j.progress)}`;
      const bar = $("pl-pxbar"); if (bar) bar.style.width = Math.round((j.progress || 0) * 100) + "%";
    }, 1000);
  }

  function setMode(m) {
    if (S.mode === m) return;
    S.mode = m; LSset("mode", m); modeButtons();
    const at = S.kind && S.url ? abs() : null;
    if (m === "full") { v.pause(); showFull(); }
    else {
      S.kind = null; S.url = null; v.removeAttribute("src"); v.load();
      if (window.MX) MX.clear(null);
      if (S.ev) playEvent(S.ev, { autoplay: false });
      else if (at != null && S.rec) playAt(S.rec.id, at, { autoplay: false });
      else overlay(card("Pick an event", "Choose one in Events (or press N), or click the timeline below."));
    }
  }
  function modeButtons() {
    $("pl-modes").querySelectorAll("button").forEach(b => b.classList.toggle("on", b.dataset.mode === S.mode));
    const full = $("pl-modes").querySelector('[data-mode="full"]');
    full.classList.toggle("noproxy", !hasProxy());
    full.dataset.tip = hasProxy() ? "The whole recording off its proxy" : "The whole recording: no proxy yet (build one from here)";
    ["pl-in", "pl-out", "pl-reset", "pl-wider"].forEach(id => $(id).disabled = !canEdit());
    $("pl-loop").classList.toggle("on", !!S.loop);
  }

  /* ---------- clip bounds ---------- */
  async function setBound(which, val) {
    if (!canEdit()) { toast("wait for this event's clip to load"); return; }
    const body = which === "reset" ? { clip_in: null, clip_out: null } : { [which === "in" ? "clip_in" : "clip_out"]: Math.round(val * 1000) / 1000 };
    if (which === "in" && S.outT != null && val >= S.outT) { toast("in must be before out"); return; }
    if (which === "out" && S.inT != null && val <= S.inT) { toast("out must be after in"); return; }
    try {
      const u = await post(`/api/events/${S.ev.id}/clip`, body);
      Object.assign(S.ev, u);
      const i = ST.events.list.findIndex(x => x.id === u.id); if (i >= 0) ST.events.list[i] = Object.assign(ST.events.list[i], u);
      if (ST.event && ST.event.id === u.id) Object.assign(ST.event, u);
      S.inT = u.clip_in != null ? u.clip_in : Math.max(0, u.t_s - preRoll());
      S.outT = u.clip_out != null ? u.clip_out : Math.min(dur() || 1e9, u.t_s + postRoll());
      toast(which === "reset" ? "clip back to the pre / post roll" : `${which} = ${fmt(val)} (clip ${(S.outT - S.inT).toFixed(1)} s)`);
      if (S.kind === "event" && (S.inT < S.w0 || S.outT > S.w1)) playEvent(S.ev, { autoplay: !v.paused });
      paint(); renderTimeline();
    } catch (err) { toast(err.message, 5000); }
  }

  async function markHere() {
    if (!S.rec || !S.kind) { toast("nothing playing"); return; }
    const t = abs(); v.pause();
    try {
      const e = await post(`/api/recordings/${S.rec.id}/events`, { t_s: Math.round(t * 1000) / 1000, note: "" });
      toast(`manual mark at ${fmt(t)}`);
      await loadEvents(S.rec.id);
      ST.event = ST.events.list.find(x => x.id === e.id) || e;
      S.ev = ST.event; S.t = e.t_s; S.loadedEv = e.id;     // the loaded media holds the mark (it is the playhead)
      S.inT = Math.max(0, e.t_s - preRoll()); S.outT = Math.min(dur() || 1e9, e.t_s + postRoll());
      if (S.kind === "free") S.kind = "event";
      renderStage(); renderDetail(); if (ST.tab === "events") renderEvents(); modeButtons(); paint();
    } catch (err) { toast(err.message, 5000); }
  }

  /* ---------- painting ---------- */
  function paint() {
    const [s0, s1] = span(); const w = Math.max(0.001, s1 - s0);
    const pc = (x) => ((x - s0) / w * 100).toFixed(3) + "%";
    const io = $("pl-io"), mk = $("pl-mark"), hd = $("pl-head");
    const live = !!S.kind && !!S.url;
    if (live && S.inT != null && S.outT != null) { io.hidden = false; io.style.left = pc(S.inT); io.style.width = ((S.outT - S.inT) / w * 100).toFixed(3) + "%"; } else io.hidden = true;
    if (live && S.t != null) { mk.hidden = false; mk.style.left = pc(S.t); mk.style.background = (S.ev && KIND_COLORS[S.ev.kind]) || "var(--acc)"; } else mk.hidden = true;
    const a = live ? abs() : null;
    if (a != null) { hd.hidden = false; hd.style.left = pc(a); } else hd.hidden = true;
    // other events inside the visible span
    const bar = $("pl-bar"); bar.querySelectorAll(".evt").forEach(x => x.remove());
    if (live && S.rec && ST.events.rec_id === S.rec.id) {
      for (const e of ST.events.list) {
        if (e.t_s < s0 || e.t_s > s1 || (S.ev && e.id === S.ev.id) || e.status === "false") continue;
        const d = document.createElement("div"); d.className = "evt"; d.style.left = pc(e.t_s); d.style.background = KIND_COLORS[e.kind] || "var(--dim)"; d.title = `${KIND_NAME[e.kind] || e.kind} ${tc(e.t_s)}`;
        bar.insertBefore(d, hd);                                    // under the playhead
      }
    }
    const pl = $("pl-play"); const ic = v.paused ? "play" : "pause";
    if (pl.dataset.ic !== ic) { pl.dataset.ic = ic; pl.innerHTML = icon(ic); pl.setAttribute("aria-label", v.paused ? "Play" : "Pause"); }
    const pip = $("pl-pip"); if (pip) pip.hidden = !(live && S.loop && S.kind !== "free" && S.kind !== "full");
    const hov = $("pl-hov"); if (hov && !live) hov.hidden = true;
    const played = bar.querySelector(".played");
    if (played) { if (a != null) { played.hidden = false; played.style.width = pc(a); } else played.hidden = true; }
    // In / Out as timecodes (lit when set by hand on this event)
    const bounds = live && S.kind !== "free" && S.inT != null && S.outT != null;
    setTc("pl-in", bounds ? fmt1(S.inT) : "", bounds && S.ev && S.ev.clip_in != null);
    setTc("pl-out", bounds ? fmt1(S.outT) : "", bounds && S.ev && S.ev.clip_out != null);
    // time: playhead / end of what is loaded; the recording position, offset from the event and clip length in its tooltip
    const tm = $("pl-time");
    const txt = a != null ? fmt1(a) : "-", end = a != null ? ` / ${fmt1(S.kind === "full" ? dur() : s1)}` : "";
    if (tm.dataset.v !== txt + end) { tm.dataset.v = txt + end; tm.innerHTML = `<b>${txt}</b><span class="faint">${end}</span>`; }
    const rel = a != null && S.t != null ? `${a - S.t >= 0 ? "+" : ""}${(a - S.t).toFixed(2)} s from the event` : "";
    const clipLen = bounds ? `clip ${fmt(S.inT)} \u2013 ${fmt(S.outT)} = ${(S.outT - S.inT).toFixed(1)} s` : "";
    const tip = [a != null ? `${fmt(a)} of ${fmt(dur())}` : "", rel, clipLen].filter(Boolean).join(" \u00b7 ");
    if (tip) tm.dataset.tip = tip; else delete tm.dataset.tip;
    paintTimelineHead(a);
    if (window.MX) MX.paint(a, S.kind === "free" ? null : S.inT, S.kind === "free" ? null : S.outT);
  }

  function tick() {
    S.raf = 0;
    if (S.kind && S.kind !== "free" && !v.paused) {
      const a = abs();
      // loop only when playback runs across the out point; a seek / timeline click past it is left alone
      if (S.loop && S.inT != null && S.outT != null && S.prevA != null && S.prevA < S.outT && a >= S.outT) seek(S.inT);
      else S.prevA = a;
    }
    paint();
    if (!v.paused) S.raf = requestAnimationFrame(tick);
  }

  /* ---------- timeline strip ---------- */
  function renderTimeline() {
    const tl = $("tl"), tr = $("tl-track");
    const rec = ST.detail;
    if (!rec || ST.view !== "normal" || !rec.duration_s) { tl.hidden = true; return; }
    tl.hidden = false;
    const D = rec.duration_s;
    const evs = ST.events.rec_id === rec.id ? ST.events.list : [];
    let html = "";
    // faint grid lines at the same steps as the time scale under the strip (app.js renderTlScale)
    const gstep = [15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400].find(x => D / x <= 12) || 28800;
    for (let x = gstep; x < D - gstep * 0.4; x += gstep) html += `<span class="gl" style="left:${(x / D * 100).toFixed(3)}%"></span>`;
    if (S.rec && S.rec.id === rec.id && S.kind && S.kind !== "full" && S.url) html += `<div class="win" style="left:${(S.w0 / D * 100).toFixed(3)}%;width:${((S.w1 - S.w0) / D * 100).toFixed(3)}%"></div>`;
    for (const e of evs) {
      if (ST.chips[e.kind] === false) continue;
      const tip = `${KIND_NAME[e.kind] || e.kind} \u00b7 ${tc(e.t_s)} \u00b7 score ${e.score.toFixed(2)}${e.status !== "new" ? " \u00b7 " + (e.status === "false" ? "false positive" : e.status) : ""}`;
      html += `<div class="tk ${e.status === "false" ? "false" : ""} ${ST.event && ST.event.id === e.id ? "cur" : ""}" data-id="${e.id}" style="left:${(e.t_s / D * 100).toFixed(3)}%;background:${KIND_COLORS[e.kind] || "var(--dim)"};--kc:${KIND_COLORS[e.kind] || "var(--dim)"}" data-tip="${esc(tip)}"></div>`;
    }
    html += `<div class="ph" id="tl-ph" hidden></div><div class="hover" id="tl-hover" hidden></div>`;
    tr.innerHTML = html;
    // the line above the strip: "Whole recording  9:58  * 1 kill  * 5 voice ... (hover time)"; kinds the filter hides are dimmed
    const counts = {}; for (const e of evs) if (e.status !== "false") counts[e.kind] = (counts[e.kind] || 0) + 1;
    const kinds = Object.keys(KIND_COLORS).filter(k => counts[k]).map(k =>
      `<span class="tlk${ST.chips[k] === false ? " off" : ""}"><i style="background:${KIND_COLORS[k]}"></i>${counts[k]} ${KIND_LABEL[k] || k}</span>`).join("");
    const none = rec.stages && rec.stages.detect ? "no events" : "not detected yet";
    $("tl-info").innerHTML = `<b>Whole recording</b><span class="num">${fmtDurShort(D)}</span>${kinds || `<span class="faint">${none}</span>`}`
      + (hasProxyRec(rec) ? "" : `<span class="faint" data-tip="Full recording mode needs a proxy (build it from the player)">no proxy</span>`)
      + `<span class="spacer"></span><span id="tl-at" class="num"></span>`;
    tr.querySelectorAll(".tk").forEach(k => k.onclick = (ev) => {
      ev.stopPropagation();
      // Find keeps its results on screen (N / P step them); anything else shows the recording's events
      if (ST.tab !== "events" && ST.tab !== "clips") showTab("events");
      selectEvent(+k.dataset.id);
    });
    paintTimelineHead(S.kind && S.url ? abs() : null);
  }
  const hasProxyRec = (rec) => !!(rec.proxy_path && rec.stages && rec.stages.proxy && rec.stages.proxy.state === "done");
  function paintTimelineHead(a) {
    const ph = $("tl-ph"); if (!ph || !ST.detail || !S.rec || ST.detail.id !== S.rec.id) return;
    if (a == null) { ph.hidden = true; return; }
    ph.hidden = false; ph.style.left = (a / (ST.detail.duration_s || 1) * 100).toFixed(3) + "%";
  }

  /* ---------- wiring ---------- */
  function barSeek(ev) {
    if (!S.kind || !S.url) return;
    const r = $("pl-bar").getBoundingClientRect(); const [s0, s1] = span();
    seek(s0 + Math.max(0, Math.min(1, (ev.clientX - r.left) / r.width)) * (s1 - s0));
  }
  function step(d) { if (!S.kind) return; v.pause(); seek(abs() + d); }
  function toggle() { if (!S.kind || !S.url) { if (ST.event) playEvent(ST.event); return; } if (v.paused && onView()) v.play().catch(() => { }); else v.pause(); }
  function replay() { if (!S.kind) return; seek(S.inT != null ? S.inT : span()[0]); if (onView()) v.play().catch(() => { }); }

  // parts of the player the static markup does not carry: the played part + hover time on the scrub bar and the
  // "Loop in -> out" badge on the video (positioned inline so they never push the layout, whatever the CSS)
  function extras() {
    const bar = $("pl-bar");
    if (!bar.querySelector(".played")) {
      const p = document.createElement("div"); p.className = "played"; p.hidden = true;
      bar.insertBefore(p, bar.querySelector(".trk").nextSibling);
    }
    if (!$("pl-hov")) {
      const h = document.createElement("div"); h.className = "hov"; h.id = "pl-hov"; h.hidden = true;
      h.style.position = "absolute"; h.innerHTML = "<span></span>"; bar.appendChild(h);
    }
    bar.removeAttribute("title");
    if (!$("pl-pip")) {
      const pip = document.createElement("div"); pip.id = "pl-pip"; pip.className = "plpip"; pip.hidden = true;
      pip.style.cssText = "position:absolute;left:12px;top:12px;pointer-events:none";
      pip.innerHTML = `<span class="badge acc">${icon("loop")}Loop in \u2192 out</span>`;
      $("pl-box").appendChild(pip);
    }
    // tooltips through the shell's [data-tip] instead of the browser's title
    $("pl-modes").querySelectorAll("button[title]").forEach(b => { b.dataset.tip = b.title; b.removeAttribute("title"); });
  }
  function barHover(ev) {
    const h = $("pl-hov"); if (!h) return;
    if (!S.kind || !S.url) { h.hidden = true; return; }
    const r = $("pl-bar").getBoundingClientRect(); const [s0, s1] = span();
    const f = Math.max(0, Math.min(1, (ev.clientX - r.left) / r.width));
    h.hidden = false; h.style.left = (f * 100).toFixed(2) + "%"; h.firstChild.textContent = fmt1(s0 + f * (s1 - s0));
  }

  function wire() {
    extras();
    const sel = $("pl-h"); sel.value = String(S.h);
    sel.onchange = () => { S.h = +sel.value; LSset("clipH", S.h); if (S.kind === "event" && S.ev) playEvent(S.ev, { autoplay: !v.paused }); else if (S.kind === "free" && S.rec) { const a = abs(); S.url = null; S.w0 = S.w1 = 0; playAt(S.rec.id, a, { autoplay: !v.paused }); } };
    $("pl-modes").querySelectorAll("button").forEach(b => b.onclick = () => setMode(b.dataset.mode));
    $("pl-play").onclick = toggle;
    $("pl-back").onclick = () => step(-FRAME);
    $("pl-fwd").onclick = () => step(FRAME);
    $("pl-replay").onclick = replay;
    $("pl-loop").onclick = () => { S.loop = !S.loop; LSset("loop", S.loop); modeButtons(); paint(); };
    $("pl-in").onclick = () => S.kind && setBound("in", abs());
    $("pl-out").onclick = () => S.kind && setBound("out", abs());
    $("pl-reset").onclick = () => setBound("reset");
    $("pl-wider").onclick = () => { if (S.ev && S.mode === "clip") playEvent(S.ev, { autoplay: !v.paused, handles: 20 }); };
    $("pl-markhere").onclick = markHere;
    const bar = $("pl-bar");
    bar.onmousedown = (e) => { S.drag = true; v.pause(); barSeek(e); };
    bar.onmousemove = barHover;
    bar.onmouseleave = () => { const h = $("pl-hov"); if (h && !S.drag) h.hidden = true; };
    window.addEventListener("mousemove", (e) => { if (S.drag) barSeek(e); });
    window.addEventListener("mouseup", () => { S.drag = false; });
    v.addEventListener("click", toggle);
    v.addEventListener("loadedmetadata", () => {
      overlay(null);
      if (S.pendingSeek != null) { seek(S.pendingSeek); S.pendingSeek = null; }
      if (S.autoplay && onView()) v.play().catch(() => { });   // the view may have changed while it loaded
      modeButtons(); paint(); renderTimeline();
    });
    v.addEventListener("play", () => { if (!S.raf) S.raf = requestAnimationFrame(tick); paint(); });
    v.addEventListener("pause", paint);
    v.addEventListener("seeked", paint);
    v.addEventListener("ended", () => { if (S.loop && S.inT != null && S.kind !== "free") replay(); paint(); });
    v.addEventListener("error", () => {
      if (!S.url) return;
      // the clip cache is size-capped (and Settings > Clear clip cache empties it): an expired clip renders
      // again, at the same moment. A clip that fails twice within a few seconds is shown as an error instead
      // of being re-rendered in a loop.
      const url = S.url; const at = S.kind && S.kind !== "full" ? S.w0 + (v.currentTime || 0) : null;
      S.url = null;
      const now = performance.now(); const again = S.lastErr && S.lastErr.url === url && now - S.lastErr.t < 5000;
      S.lastErr = { url, t: now };
      if (again && S.kind !== "full") { overlay(`<div class="empty">This clip could not be played.<br><small>${esc(url)}</small></div>`); return; }
      if (S.kind === "event" && S.ev) playEvent(S.ev, { autoplay: S.autoplay });
      else if (S.kind === "free" && S.rec) { S.w0 = S.w1 = 0; playAt(S.rec.id, at, { autoplay: S.autoplay }); }
      else if (S.kind === "full") overlay(`<div class="empty">The proxy could not be played.<br><small>${esc(url)}</small></div>`);
    });
    // timeline: click anywhere = look at that moment
    const tr = $("tl-track");
    tr.onclick = (e) => {
      if (!ST.detail || !ST.detail.duration_s) return;
      const r = tr.getBoundingClientRect(); const t = (e.clientX - r.left) / r.width * ST.detail.duration_s;
      playAt(ST.detail.id, t);
    };
    tr.onmousemove = (e) => {
      if (!ST.detail || !ST.detail.duration_s) return;
      const r = tr.getBoundingClientRect(); const f = (e.clientX - r.left) / r.width;
      const h = $("tl-hover"); if (h) { h.hidden = false; h.style.left = (f * 100).toFixed(2) + "%"; }
      const at = $("tl-at"); if (at) at.textContent = tc(f * ST.detail.duration_s);
    };
    tr.onmouseleave = () => { const h = $("tl-hover"); if (h) h.hidden = true; const at = $("tl-at"); if (at) at.textContent = ""; };
    document.addEventListener("keydown", (e) => {
      const tag = e.target.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || e.target.isContentEditable || e.ctrlKey || e.altKey || e.metaKey) return;
      if (ST.view !== "normal" || !$("drawer").hidden || document.querySelector("dialog[open]")) return;
      const k = e.key;
      if (k === " ") { e.preventDefault(); toggle(); }
      else if (k === ",") step(-FRAME);
      else if (k === ".") step(FRAME);
      else if (k === "ArrowLeft" && S.kind) { e.preventDefault(); seek(abs() - (e.shiftKey ? 5 : 1)); }
      else if (k === "ArrowRight" && S.kind) { e.preventDefault(); seek(abs() + (e.shiftKey ? 5 : 1)); }
      else if (k === "i" && S.ev) setBound("in", abs());
      else if (k === "o" && S.ev) setBound("out", abs());
      else if (k === "r") replay();
      else if (k === "l") $("pl-loop").click();
      else if (k === "m") markHere();
    });
    S.mode = LS("mode", "clip") === "full" ? "full" : "clip";
    modeButtons();
  }

  wire();
  return {
    playEvent, playAt, setRecording, renderTimeline, clearEvent, syncEvent,
    seekAbs: (recId, t) => playAt(recId, t),
    seekTo: (a) => { if (S.kind && S.url) seek(a); },
    repaint: () => paint(),
    pause: () => v.pause(),
    isOn: (eventId) => !!(S.ev && S.ev.id === eventId && S.kind && S.url),
    state: S,
  };
})();
window.PL = PL;
