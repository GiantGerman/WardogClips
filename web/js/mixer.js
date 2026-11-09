/* Track panel = the Audio tab of the context area (#cp-audio > #mixer), like Resolve's mixer: A0 Master on top (fader, mute, DIM, stereo
   peak meter, the mix's waveform), then every OBS audio track as a lane A1..AN with its waveform,
   Mute / Solo, a dB fader on its own row across the whole cell and a post-fader meter. The OBS
   track name and role are in the A-number's tooltip.

   The clip / proxy video carries ONE mixed audio track, so per-track control needs the tracks
   separately: the server renders "stems" (one AAC file per OBS track, same window as the clip,
   the mic lifted exactly as in the mix) + waveform peaks (media/stems.py). Each stem plays in an
   <audio> element routed through Web Audio:
       stem -> track gain -> track meter -> master gain -> master meters (L / R)
                                                       \-> DIM -> speakers
   and follows the video's clock; while the stems are live the video itself is muted. Until they
   are ready (or when Web Audio is blocked) the video's own mixed audio plays, so there is never
   silence by accident -- the master fader / mute / DIM then act on the video's volume (which
   cannot go above 0 dB).

   DIM is a monitoring control, as in Resolve: it lowers what you HEAR by a fixed amount (default
   15 dB, right-click to change) without touching the master level or its meter.

   Mute / solo / fader per track number and the master fader / mute / DIM amount persist in
   localStorage across recordings (DIM itself starts off).

   Layout: each lane = a control card (.lctl: A-number, M / S, dB, the fader across the
   bottom, the post-fader meter on its right) beside a waveform card (.wf > canvas). The in / out shading and the
   playhead over the waveforms are placed from the waveform column's measured position, so the CSS may change the
   column widths and gaps freely. Under the lanes, a Track details disclosure lists the OBS names and roles.

   Every visible text comes from the "mixer" catalog; labels and tooltips are set by labelMaster / labelLane and set
   again when the UI language changes (relabel), without rebuilding the lanes or touching playback. */
"use strict";

const MX = (() => {
  const v = $("pl-video");
  const FADER_MIN = -40.5;                   // the bottom stop = -inf
  const FADER_MAX = 12;
  const DRIFT = 0.08;                        // s: re-seat a stem that wandered further than this
  const LS_KEY = "wclips.mixer";
  const DIM_STEPS = [-10, -15, -20, -30];
  const S = { man: null, key: null, lanes: [], ctx: null, ready: false, token: 0, offset: 0, mode: "window",
              span: [0, 0], lastSync: 0, anySolo: false, bus: null, dimNode: null, anL: null, anR: null };
  const ROLE_COLOR = { game: "#49c463", mic: "#4ca7ef", voip: "#b48cff", voice: "#4ca7ef", desktop: "#8b98a8",
                       music: "#8b98a8", other: "#ffd54a", unknown: "#8b98a8", silent: "#3a4452", dup: "#3a4452" };
  // a track role as text (the role itself is an API value; an unknown one is shown as it is)
  const roleLabel = (r) => I18N.has("mixer.role." + r) ? t("mixer.role." + r) : String(r ?? "");

  /* ---------- persisted state ---------- */
  const load = () => { try { return JSON.parse(localStorage.getItem(LS_KEY) || "{}") || {}; } catch (e) { return {}; } };
  const save = (o) => { try { localStorage.setItem(LS_KEY, JSON.stringify(o)); } catch (e) { } };
  let saved = load();
  function stateFor(tr) {
    const s = saved[tr.idx] || {};
    return { db: s.db != null ? s.db : 0, mute: s.mute != null ? s.mute : !tr.in_mix, solo: !!s.solo };
  }
  function remember(lane) { saved[lane.t.idx] = { db: lane.db, mute: lane.mute, solo: lane.solo }; save(saved); }
  function masterSaved() {
    const s = saved.master || {};
    return { db: s.db != null ? s.db : 0, mute: !!s.mute, dim: false, dimDb: DIM_STEPS.includes(s.dimDb) ? s.dimDb : -15 };
  }
  const M = Object.assign(masterSaved(), { el: null, canvas: null, level: [0, 0], hold: [0, 0], holdT: [0, 0] });
  function rememberMaster() { saved.master = { db: M.db, mute: M.mute, dimDb: M.dimDb }; save(saved); }

  const dbGain = (db) => db <= FADER_MIN ? 0 : Math.pow(10, db / 20);
  const dbText = (db) => db <= FADER_MIN ? "-\u221e" : fmtNum(db, { minimumFractionDigits: 1, maximumFractionDigits: 1, signDisplay: "exceptZero" });
  // the fader's filled part (the CSS paints its track from --p)
  const fillOf = (db) => (Math.max(0, Math.min(1, (Math.max(db, -40) + 40) / (FADER_MAX + 40))) * 100).toFixed(1) + "%";
  const setFill = (f, db) => { if (f) f.style.setProperty("--p", fillOf(db)); };
  const masterGain = () => M.mute ? 0 : dbGain(M.db);        // the master bus (what the meter shows)
  const dimGain = () => M.dim ? Math.pow(10, M.dimDb / 20) : 1; // monitoring only

  /* ---------- Web Audio ---------- */
  function ensureCtx() {
    if (S.ctx) return S.ctx;
    const C = window.AudioContext || window.webkitAudioContext;
    if (!C) return null;
    const ctx = S.ctx = new C({ latencyHint: "interactive" });
    // master bus: every track's meter output sums here
    S.bus = ctx.createGain();
    S.dimNode = ctx.createGain();
    const split = ctx.createChannelSplitter(2);
    S.anL = ctx.createAnalyser(); S.anR = ctx.createAnalyser();
    S.anL.fftSize = S.anR.fftSize = 1024;
    S.bus.connect(split); split.connect(S.anL, 0); split.connect(S.anR, 1);
    S.bus.connect(S.dimNode); S.dimNode.connect(ctx.destination);
    applyMaster();
    return ctx;
  }
  // browsers start an AudioContext suspended until the user interacts with the page
  ["pointerdown", "keydown"].forEach(ev => document.addEventListener(ev, () => {
    if (S.ctx && S.ctx.state === "suspended") S.ctx.resume().then(() => { if (S.ready) { applyAudioRouting(); syncNow(true); } paintState(); });
  }, true));

  // as in Resolve: mute always wins; with any solo lit only the soloed (unmuted) tracks play
  const gainOf = (lane) => {
    const base = dbGain(lane.db);
    if (lane.mute) return 0;
    if (S.anySolo) return lane.solo ? base : 0;
    return base;
  };
  function applyGains() {
    S.anySolo = S.lanes.some(l => l.solo);
    for (const l of S.lanes) {
      const g = gainOf(l);
      if (l.gain) l.gain.gain.setTargetAtTime(g, S.ctx ? S.ctx.currentTime : 0, 0.015);
      l.el_lane.classList.toggle("off", g === 0);
      l.el_lane.classList.toggle("implicit", S.anySolo && !l.solo && !l.mute);
      setFill(l.el_lane.querySelector(".fader"), l.db);
      l.el_lane.querySelector(".ms.m").classList.toggle("on", l.mute);
      l.el_lane.querySelector(".ms.s").classList.toggle("on", l.solo);
      l.el_lane.querySelector(".db").textContent = dbText(l.db);
    }
    drawMaster();
  }

  // the video's own (mixed) audio plays unless every stem is ready and Web Audio is running
  const live = () => !!(S.ready && S.ctx && S.ctx.state === "running" && S.lanes.some(l => l.audio));
  function applyAudioRouting() {
    if (live()) v.muted = true;
    else {
      // the clip's own mix: the master + DIM act on the video element (no gain above 0 dB there)
      const g = masterGain() * dimGain();
      v.muted = g === 0;
      v.volume = Math.max(0, Math.min(1, g));
      for (const l of S.lanes) if (l.audio && !l.audio.paused) l.audio.pause();
    }
  }
  function applyMaster() {
    if (S.ctx) {
      const now = S.ctx.currentTime;
      S.bus.gain.setTargetAtTime(masterGain(), now, 0.015);
      S.dimNode.gain.setTargetAtTime(dimGain(), now, 0.015);
    }
    applyAudioRouting();
    if (M.el) {
      M.el.querySelector(".ms.m").classList.toggle("on", M.mute);
      const d = M.el.querySelector(".ms.d");
      d.classList.toggle("on", M.dim);
      d.title = t("mixer.master.dim_title", { db: fmtNum(-M.dimDb), steps: DIM_STEPS.map(x => fmtNum(-x)).join(" / ") });
      M.el.querySelector(".db").textContent = M.dim ? t("mixer.master.db_dim", { db: dbText(M.db), dim: fmtNum(M.dimDb) }) : dbText(M.db);
      M.el.classList.toggle("dimmed", M.dim);
      M.el.classList.toggle("off", masterGain() === 0);
      M.el.querySelector(".fader").value = M.db;
      setFill(M.el.querySelector(".fader"), M.db);
    }
    drawMaster();
  }

  /* ---------- faders: drag, wheel (0.5 dB, shift 0.1 dB), double-click = 0 dB ---------- */
  function wireFader(f, get, set, commit) {
    f.oninput = () => { set(parseFloat(f.value)); setFill(f, get()); };
    f.onchange = () => commit();
    f.ondblclick = () => { f.value = 0; set(0); setFill(f, 0); commit(); };
    f.addEventListener("wheel", (e) => {
      // Chrome / Edge / Firefox on Windows deliver shift+wheel as a horizontal scroll (deltaX, deltaY = 0)
      const d = e.deltaY || e.deltaX;
      if (!d) return;
      e.preventDefault();
      const step = e.shiftKey ? 0.1 : 0.5;
      let x = get(); if (x <= FADER_MIN) x = -40;
      x = Math.round((x + (d < 0 ? step : -step)) * 10) / 10;
      x = x < -40 ? FADER_MIN : Math.min(FADER_MAX, x);
      f.value = x; set(x); setFill(f, x); commit();
    }, { passive: false });
  }
  // what: the catalog key of the fader's name (mixer.fader.master / mixer.fader.track); labelFader sets its texts
  const faderHTML = (db) => `<input type="range" class="fader" min="${FADER_MIN}" max="${FADER_MAX}" step="0.1" value="${db}" style="--p:${fillOf(db)}">`;
  function labelFader(f, whatKey) {
    if (!f) return;
    const what = t(whatKey);
    f.setAttribute("aria-label", what);
    f.title = t("mixer.fader.title", { what });
  }

  /* ---------- A0 Master (built once, always on top) ---------- */
  function buildMaster() {
    const el = document.createElement("div");
    el.className = "lane master";
    el.innerHTML = `<div class="lctl"><div class="lrow"><span class="lnum">A0</span><span class="lname"></span>
          <button class="sm ms m"></button><button class="sm ms d"></button><span class="db"></span></div>
        <div class="lrow fad">${faderHTML(M.db)}</div>
        <div class="meters"><span class="meter"><i></i><b></b></span><span class="meter"><i></i><b></b></span></div></div>
      <div class="wf"><canvas class="wave"></canvas></div>`;
    $("mx-master").appendChild(el);
    M.el = el; M.canvas = el.querySelector("canvas");
    labelMaster();
    el.querySelector(".ms.m").onclick = () => { M.mute = !M.mute; rememberMaster(); applyMaster(); };
    const d = el.querySelector(".ms.d");
    d.onclick = () => { M.dim = !M.dim; applyMaster(); };
    d.oncontextmenu = (e) => {
      e.preventDefault(); e.stopPropagation();
      M.dimDb = DIM_STEPS[(DIM_STEPS.indexOf(M.dimDb) + 1) % DIM_STEPS.length];
      rememberMaster(); applyMaster(); toast(t("mixer.master.dim_toast", { db: fmtNum(M.dimDb) }));
    };
    wireFader(el.querySelector(".fader"), () => M.db, (x) => { M.db = x; applyMaster(); }, rememberMaster);
    M.canvas.onclick = (e) => seekAt(M.canvas, e);
    el.querySelector(".meters").onclick = () => {
      M.hold = [0, 0]; M.holdT = [0, 0];
      el.querySelectorAll(".meter b").forEach(b => { b.style.opacity = 0; });
    };
    applyMaster();
  }
  // the master strip's texts (the DIM tooltip and the level text are set by applyMaster)
  function labelMaster() {
    const el = M.el; if (!el) return;
    el.querySelector(".lname").textContent = t("mixer.master.name");
    const m = el.querySelector(".ms.m"); m.textContent = t("mixer.master.mute_btn"); m.title = t("mixer.master.mute_title");
    el.querySelector(".ms.d").textContent = t("mixer.master.dim_btn");
    labelFader(el.querySelector(".fader"), "mixer.fader.master");
    el.querySelector(".meters").title = t("mixer.master.meters_title");
    M.canvas.title = t("mixer.master.wave_title");
  }

  /* ---------- building the track lanes ---------- */
  function teardown() {
    for (const l of S.lanes) {
      try { if (l.audio) { l.audio.pause(); l.audio.removeAttribute("src"); l.audio.load(); } } catch (e) { }
      try { if (l.src) l.src.disconnect(); if (l.gain) l.gain.disconnect(); if (l.an) l.an.disconnect(); } catch (e) { }
    }
    S.lanes = []; S.ready = false; S.settled = false; S.man = null; S.key = null; S.msgFn = null;
    $("mx-lanes").innerHTML = "";
    details(null);
    $("mx-io").hidden = true; $("mx-ph").hidden = true;     // the previous window's in / out and playhead
    applyAudioRouting();                                    // back to the video's own mix (master still applies)
    drawMaster();
  }

  function clear(msg) {
    S.token++;
    teardown();
    const box = $("mixer");
    if (msg === null) { box.hidden = true; return; }
    box.hidden = false;
    $("mx-span").textContent = "";
    $("mx-lanes").innerHTML = msg ? `<div class="empty" style="padding:14px">${msg}</div>` : "";
    $("mx-ph").hidden = true; $("mx-io").hidden = true;
    setState(msg ? "" : "-", "");
  }

  // the panel's state line, as a badge: live = green, a fallback to the clip's own mix = amber
  function setState(text, cls) {
    const el = $("mx-state"); el.textContent = text;
    el.className = "mxstate" + (cls ? " " + cls : "") + (text && text !== "-" ? " badge" + (cls === "live" ? " ok" : cls === "warn" ? " warn" : "") : "");
  }
  function paintState() {
    if (!S.man) return;
    if (live()) setState(t("mixer.state.live"), "live");
    else if (S.settled && !S.ready) setState(t("mixer.state.no_tracks"), "warn");
    else if (S.ctx && S.ctx.state === "suspended") setState(t("mixer.state.suspended"), "warn");
    else if (!S.ready) setState(t("mixer.state.loading_fallback"), "");
    else setState(t("mixer.state.fallback"), "warn");
  }

  async function request(url, mode, isCurrent) {
    // isCurrent(): does the player still show the window these tracks are for?
    const my = ++S.token;
    teardown();
    // a clip that finishes rendering while the Calibration page is open must not show the panel there;
    // closeCalibration shows it again once tracks are loaded
    if (!(typeof ST !== "undefined" && ST.view !== "normal")) $("mixer").hidden = false;
    // the lanes' message while there are no lanes: kept as a function, so a language switch draws it again
    S.msgFn = () => {
      $("mx-lanes").innerHTML = `<div class="empty" style="padding:14px">${te("mixer.lanes.loading")}</div>`;
      setState(t("mixer.state.loading"), "");
    };
    S.msgFn();
    let man;
    try { man = await api(url); }
    catch (err) {
      if (my !== S.token) return;
      let rebuilt = false;
      S.msgFn = () => {
        const btn = mode !== "full" ? "" : rebuilt ? `<br><br><span class="badge">${te("mixer.rebuild.queued")}</span>`
          : `<br><br><button class="sm" id="mx-rebuild">${te("mixer.rebuild.button")}</button>`;
        $("mx-lanes").innerHTML = `<div class="empty" style="padding:14px">${esc(err.message)}${btn}</div>`;
        setState("", "");
        const b = $("mx-rebuild"); if (b) b.onclick = () => { if (window.PL && PL.state.rec) post(`/api/recordings/${PL.state.rec.id}/proxy`).then(() => { toast(t("mixer.rebuild.queued_toast")); rebuilt = true; if (b.isConnected) b.outerHTML = `<span class="badge">${te("mixer.rebuild.queued")}</span>`; }).catch(e => toast(e.message, 5000)); };
      };
      S.msgFn();
      return;
    }
    S.msgFn = null;
    if (my !== S.token || (isCurrent && !isCurrent())) return;
    build(man, mode);
  }

  function seekAt(canvas, e) {
    if (!S.man) return;
    const r = canvas.getBoundingClientRect(); const fr = (e.clientX - r.left) / r.width;
    if (window.PL) PL.seekTo(S.span[0] + fr * (S.span[1] - S.span[0]));
  }

  function build(man, mode) {
    S.man = man; S.key = man.key; S.mode = mode;
    S.span = [man.t0, man.t1];
    // the stem's t=0 is man.t0; the video's local t=0 is the clip window start (or 0 for a proxy)
    S.offset = mode === "full" ? 0 : (man.t0 - (window.PL ? PL.state.w0 : man.t0));
    labelSpan();
    const ctx = ensureCtx();
    const holder = $("mx-lanes"); holder.innerHTML = "";
    const pending = [];
    for (const tr of man.tracks) {
      const st = stateFor(tr);
      const silent = tr.role === "silent" || tr.peak_db <= -90;
      const lane = { t: tr, db: st.db, mute: st.mute, solo: st.solo, audio: null, src: null, gain: null, an: null, el_lane: null, canvas: null, level: 0, silent, failed: false };
      const el = document.createElement("div");
      el.className = `lane role-${tr.role}${silent ? " silent" : ""}`;
      const col = (tr.role === "silent" || tr.role === "dup") ? "var(--dim)" : (ROLE_COLOR[tr.role] || "var(--dim)");
      el.innerHTML = `<div class="lctl"><div class="lrow"><span class="lnum" style="color:${col}">A${tr.idx + 1}</span>
          <button class="sm ms m"></button><button class="sm ms s"></button>
          <span class="db"></span></div>
        <div class="lrow fad">${faderHTML(st.db)}</div>
        <div class="meters"><span class="meter"><i></i></span></div></div>
        <div class="wf"><canvas class="wave"></canvas></div>`;
      holder.appendChild(el);
      lane.el_lane = el; lane.canvas = el.querySelector("canvas");
      labelLane(lane);
      el.querySelector(".ms.m").onclick = () => { lane.mute = !lane.mute; remember(lane); applyGains(); drawAll(); };
      el.querySelector(".ms.s").onclick = (e) => {
        if (e.ctrlKey || e.shiftKey) for (const o of S.lanes) if (o !== lane) { o.solo = false; remember(o); }
        lane.solo = !lane.solo; remember(lane); applyGains(); drawAll();
      };
      wireFader(el.querySelector(".fader"), () => lane.db, (x) => { lane.db = x; applyGains(); }, () => { remember(lane); drawLane(lane); });
      lane.canvas.onclick = (e) => seekAt(lane.canvas, e);
      if (!silent && ctx) {
        const a = new Audio();
        a.preload = "auto"; a.crossOrigin = "anonymous"; a.src = tr.url;
        try {
          lane.src = ctx.createMediaElementSource(a);
          lane.gain = ctx.createGain();
          lane.an = ctx.createAnalyser(); lane.an.fftSize = 512;
          lane.src.connect(lane.gain); lane.gain.connect(lane.an); lane.an.connect(S.bus);
          lane.audio = a;
          pending.push(new Promise(res => {
            if (a.readyState >= 3) return res();
            a.addEventListener("canplay", res, { once: true });
            a.addEventListener("error", res, { once: true });
            setTimeout(res, 8000);
          }));
        } catch (e) { lane.audio = null; }
      }
      S.lanes.push(lane);
    }
    details(man);
    applyGains(); drawAll();
    if (window.PL && PL.repaint) PL.repaint();               // playhead / in-out now, even while paused
    const tok = S.token;
    Promise.all(pending).then(() => {
      if (tok !== S.token) return;
      // a stem that failed to load drops out (its lane stays, dimmed); the rest play
      for (const l of S.lanes) if (l.audio && l.audio.error) {
        try { l.src.disconnect(); l.gain.disconnect(); l.an.disconnect(); } catch (e) { }
        l.audio = null; l.failed = true; l.el_lane.classList.add("silent"); labelLane(l);
      }
      S.settled = true;
      S.ready = S.lanes.some(l => l.audio);
      applyAudioRouting(); syncNow(true); paintState();
    });
    paintState();
  }

  // one lane's texts: the A-number's tooltip (OBS name, role, clip mix), M / S, the fader, meter and waveform tips
  function labelLane(lane) {
    const tr = lane.t, el = lane.el_lane; if (!el) return;
    const tip = [t("mixer.lane.tip", { n: tr.idx + 1, name: tr.name || t("mixer.lane.no_name"), role: roleLabel(tr.role) }),
      tr.normalized ? t("mixer.lane.tip_lifted") : "", tr.in_mix ? t("mixer.lane.tip_in_mix") : t("mixer.lane.tip_not_in_mix")].filter(Boolean).join("\n");
    el.querySelector(".lnum").title = tip;
    const m = el.querySelector(".ms.m"); m.textContent = t("mixer.lane.mute_btn"); m.title = t("mixer.lane.mute_title");
    const so = el.querySelector(".ms.s"); so.textContent = t("mixer.lane.solo_btn"); so.title = t("mixer.lane.solo_title");
    labelFader(el.querySelector(".fader"), "mixer.fader.track");
    el.querySelector(".meters").title = t("mixer.lane.meter_title");
    lane.canvas.title = lane.failed ? t("mixer.lane.load_failed") : lane.silent ? t("mixer.role.silent") : t("mixer.lane.wave_title");
  }
  function labelSpan() {
    const man = S.man; if (!man) return;
    $("mx-span").textContent = t(S.mode === "full" ? "mixer.span.full" : "mixer.span.window", { t0: tc(man.t0), t1: tc(man.t1) });
  }

  /* ---------- track details (OBS names, roles, levels) under the lanes, folded ---------- */
  let detOpen = false;
  function details(man) {
    let d = $("mx-det");
    if (!man || !man.tracks || !man.tracks.length) { if (d) d.hidden = true; return; }
    if (!d) {
      d = document.createElement("details"); d.id = "mx-det"; d.className = "disc-d mxdet";
      d.addEventListener("toggle", () => { detOpen = d.open; });
      $("mixer").appendChild(d);
    }
    d.hidden = false; d.open = detOpen;
    const mixKey = (tr) => tr.in_mix ? (tr.normalized ? "mixer.det.yes_lifted" : "mixer.det.yes") : (tr.normalized ? "mixer.det.no_lifted" : "mixer.det.no");
    const rows = man.tracks.map(tr => `<tr><td class="mono">A${tr.idx + 1}</td><td>${tr.name ? esc(tr.name) : te("mixer.lane.no_name")}</td>
        <td><span class="badge${tr.role === "mic" || tr.role === "voip" || tr.role === "voice" ? " acc" : ""}">${esc(roleLabel(tr.role))}</span></td>
        <td>${te(mixKey(tr))}</td>
        <td class="num">${tr.peak_db != null && isFinite(tr.peak_db) ? te("mixer.det.peak_db", { db: fmtNum(tr.peak_db, { minimumFractionDigits: 1, maximumFractionDigits: 1 }) }) : "-"}</td></tr>`).join("");
    d.innerHTML = `<summary>${icon("chev", "chev")}${te("mixer.det.summary")} <span class="faint">${te("mixer.det.summary_sub")}</span></summary>
      <div class="mxdet-b"><table class="t"><thead><tr><th>${te("mixer.det.col_track")}</th><th>${te("mixer.det.col_name")}</th><th>${te("mixer.det.col_role")}</th><th>${te("mixer.det.col_mix")}</th><th>${te("mixer.det.col_peak")}</th></tr></thead><tbody>${rows}</tbody></table>
      <p class="faint t-xs">${te("mixer.det.note")}</p></div>`;
  }

  /* ---------- waveforms ---------- */
  // dB-scaled height (-54 dBFS .. 0): game / Discord sit 20-40 dB under full scale in OBS and
  // would be a flat line on a linear scale
  const sc = (a) => { const m = Math.abs(a); if (m < 0.002) return 0; return Math.sign(a) * Math.max(0, (20 * Math.log10(m) + 54) / 54); };
  function sizeCanvas(c) {
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(10, Math.round(c.clientWidth * dpr)), h = Math.max(10, Math.round(c.clientHeight * dpr));
    if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
    return [w, h, dpr];
  }
  function drawLane(l) {
    // hidden (calibration page, cleared panel): clientWidth is 0 and the bitmap would shrink to 10 px
    const c = l.canvas; if (!c || !c.isConnected || !c.clientWidth) return;
    const [w, h, dpr] = sizeCanvas(c);
    const g = c.getContext("2d"); g.clearRect(0, 0, w, h);
    const lo = l.t.peaks_min || [], hi = l.t.peaks_max || []; const n = hi.length; if (!n) return;
    const mid = h / 2;
    g.fillStyle = "rgba(255,255,255,0.06)"; g.fillRect(0, mid, w, Math.max(1, dpr));
    g.fillStyle = gainOf(l) === 0 ? "#3a4452" : (ROLE_COLOR[l.t.role] || "#8b98a8");
    for (let x = 0; x < w; x++) {
      const a = Math.floor(x / w * n), b = Math.max(a + 1, Math.floor((x + 1) / w * n));
      let mn = 0, mx = 0;
      for (let k = a; k < b && k < n; k++) { if (lo[k] < mn) mn = lo[k]; if (hi[k] > mx) mx = hi[k]; }
      const y0 = mid - sc(mx) * (mid - 1), y1 = mid - sc(mn) * (mid - 1);
      g.fillRect(x, y0, 1, Math.max(1, y1 - y0));
    }
  }
  // the master: each audible track's peaks x its gain, summed, x the master fader. Peaks inside one
  // bucket need not coincide, so this is an upper estimate -- good enough to see level and overs.
  function drawMaster() {
    const c = M.canvas; if (!c || !c.isConnected || !c.clientWidth) return;
    const [w, h, dpr] = sizeCanvas(c);
    const g = c.getContext("2d"); g.clearRect(0, 0, w, h);
    const mid = h / 2;
    g.fillStyle = "rgba(255,255,255,0.06)"; g.fillRect(0, mid, w, Math.max(1, dpr));
    const mg = masterGain();
    const src = S.lanes.map(l => ({ g: gainOf(l) * mg, lo: l.t.peaks_min || [], hi: l.t.peaks_max || [] })).filter(s => s.g > 0 && s.hi.length);
    if (!src.length) return;
    const over = [];
    g.fillStyle = M.dim ? "#7f9bb5" : "#d8dee6";
    for (let x = 0; x < w; x++) {
      let mx = 0, mn = 0;
      for (const s of src) {
        const n = s.hi.length;
        const a = Math.floor(x / w * n), b = Math.max(a + 1, Math.floor((x + 1) / w * n));
        let smx = 0, smn = 0;
        for (let k = a; k < b && k < n; k++) { if (s.lo[k] < smn) smn = s.lo[k]; if (s.hi[k] > smx) smx = s.hi[k]; }
        mx += smx * s.g; mn += smn * s.g;
      }
      if (mx > 1 || mn < -1) over.push(x);
      const y0 = mid - sc(Math.min(1, mx)) * (mid - 1), y1 = mid - sc(Math.max(-1, mn)) * (mid - 1);
      g.fillRect(x, y0, 1, Math.max(1, y1 - y0));
    }
    g.fillStyle = "#ff5a52";
    for (const x of over) { g.fillRect(x, 0, 1, 3 * dpr); g.fillRect(x, h - 3 * dpr, 1, 3 * dpr); }
  }
  function drawAll() { for (const l of S.lanes) drawLane(l); drawMaster(); }
  // redraw + re-place the playhead / in-out whenever the waveform column changes size: a window resize, but also
  // the side panel folding, the context area docking beside the player, or the Audio tab being shown again
  function relayout() { drawAll(); if (window.PL && PL.repaint) PL.repaint(); }
  window.addEventListener("resize", relayout);
  if (window.ResizeObserver) {
    let rq = 0;
    new ResizeObserver(() => { if (!rq) rq = requestAnimationFrame(() => { rq = 0; relayout(); }); }).observe($("mx-body"));
  }

  /* ---------- following the video ---------- */
  const stemTime = () => v.currentTime - S.offset;          // video local time -> stem time
  // the picture is actually moving: not paused, not mid-seek, not starved for data
  const advancing = () => !v.paused && !v.seeking && v.readyState >= 3;
  function syncNow(force) {
    if (!live()) return;
    const t = stemTime();
    const run = advancing();
    for (const l of S.lanes) {
      const a = l.audio; if (!a) continue;
      const dur = isFinite(a.duration) ? a.duration : Infinity;
      if (t < 0 || t >= dur - 0.02) { if (!a.paused) a.pause(); continue; }
      if (force || Math.abs(a.currentTime - t) > DRIFT) { try { a.currentTime = t; } catch (e) { } }
      if (run && a.paused) a.play().catch(() => { });
      if (!run && !a.paused) a.pause();
    }
    S.lastSync = performance.now();
  }
  // The video starts painting ~40 ms after its clock starts, so stems seated at play time lead the
  // picture by that much. One more re-seat once
  // playback has settled brings them to within +/-5 ms, and they stay there.
  let settleT = null;
  function settleSoon() {
    clearTimeout(settleT);
    settleT = setTimeout(() => { if (advancing()) syncNow(true); else if (!v.paused) settleSoon(); }, 450);
  }
  v.addEventListener("play", () => { if (S.ctx && S.ctx.state === "suspended") S.ctx.resume().catch(() => { }); applyAudioRouting(); syncNow(true); paintState(); });
  v.addEventListener("playing", () => { syncNow(true); settleSoon(); });
  v.addEventListener("pause", () => { clearTimeout(settleT); for (const l of S.lanes) if (l.audio && !l.audio.paused) l.audio.pause(); });
  v.addEventListener("waiting", () => { for (const l of S.lanes) if (l.audio && !l.audio.paused) l.audio.pause(); });
  v.addEventListener("seeked", () => { syncNow(true); if (!v.paused) settleSoon(); });
  v.addEventListener("emptied", () => { for (const l of S.lanes) if (l.audio && !l.audio.paused) l.audio.pause(); });

  /* ---------- per-frame paint (called by the player's paint / tick) ---------- */
  function waveCol() {
    const c = M.canvas; if (!c || !c.clientWidth) return null;
    const cr = c.getBoundingClientRect(), br = $("mx-body").getBoundingClientRect();
    return { x: cr.left - br.left, w: cr.width };
  }
  const peakDb = (an, buf) => {
    an.getFloatTimeDomainData(buf);
    let pk = 0; for (let k = 0; k < buf.length; k++) { const x = Math.abs(buf[k]); if (x > pk) pk = x; }
    return pk > 0 ? 20 * Math.log10(pk) : -90;
  };
  const meterFrac = (db) => Math.max(0, Math.min(1, (db + 60) / 60));     // -60 .. 0 dBFS
  function paint(absT, inT, outT) {
    const box = $("mixer"); if (box.hidden || !S.man) return;
    const [s0, s1] = S.span; const w = Math.max(0.001, s1 - s0);
    // the waveform column, measured (every lane's waveform shares its x and width)
    const col = waveCol();
    const ph = $("mx-ph"), io = $("mx-io");
    const frac = (x) => Math.max(0, Math.min(1, (x - s0) / w));
    if (col && absT != null && absT >= s0 && absT <= s1) { ph.hidden = false; ph.style.left = (col.x + col.w * frac(absT)).toFixed(1) + "px"; } else ph.hidden = true;
    if (col && inT != null && outT != null && S.mode !== "full") {
      io.hidden = false; io.style.left = (col.x + col.w * frac(inT)).toFixed(1) + "px";
      io.style.width = (col.w * (Math.max(0, Math.min(outT, s1) - Math.max(inT, s0)) / w)).toFixed(1) + "px";
    } else io.hidden = true;
    // peak meters (fast attack, slow fall) + drift correction a few times a second while playing
    const on = live() && !v.paused;
    if (!on) {
      // paint stops with playback: drop the meters now instead of freezing them at the last level
      for (const l of S.lanes) { l.level = 0; l.el_lane.querySelector(".meter i").style.height = "0%"; }
      M.level = [0, 0]; M.hold = [0, 0];
      if (M.el) M.el.querySelectorAll(".meter").forEach(m => { m.querySelector("i").style.height = "0%"; m.querySelector("b").style.opacity = 0; });
      return;
    }
    const buf = paint.buf || (paint.buf = new Float32Array(512));
    for (const l of S.lanes) {
      const db = (l.an && on) ? peakDb(l.an, buf) : -90;
      l.level = Math.max(meterFrac(db), l.level - 0.03);
      l.el_lane.querySelector(".meter i").style.height = (l.level * 100).toFixed(1) + "%";
    }
    if (M.el) {
      const mbuf = paint.mbuf || (paint.mbuf = new Float32Array(1024));
      const now = performance.now();
      M.el.querySelectorAll(".meter").forEach((m, ch) => {
        const an = ch ? S.anR : S.anL;
        const db = (an && on) ? peakDb(an, mbuf) : -90;
        M.level[ch] = Math.max(meterFrac(db), M.level[ch] - 0.03);
        // peak hold for 1.5 s; red once it touched 0 dBFS
        if (db > -90 && meterFrac(db) >= M.hold[ch]) { M.hold[ch] = meterFrac(db); M.holdT[ch] = now; }
        else if (now - M.holdT[ch] > 1500) M.hold[ch] = Math.max(0, M.hold[ch] - 0.02);
        m.querySelector("i").style.height = (M.level[ch] * 100).toFixed(1) + "%";
        const b = m.querySelector("b");
        b.style.bottom = `calc(${(M.hold[ch] * 100).toFixed(1)}% - 2px)`;
        b.style.opacity = M.hold[ch] > 0.02 ? 1 : 0;
        b.style.background = M.hold[ch] >= 0.999 ? "var(--bad)" : "#fff";
      });
    }
    if (advancing() && performance.now() - S.lastSync > 300) syncNow(false);
  }

  $("mx-reset").onclick = () => {
    const dimDb = M.dimDb;
    saved = {}; save(saved);
    Object.assign(M, { db: 0, mute: false, dim: false, dimDb }); rememberMaster();
    for (const l of S.lanes) {
      const st = stateFor(l.t); l.db = st.db; l.mute = st.mute; l.solo = st.solo;
      l.el_lane.querySelector(".fader").value = st.db; setFill(l.el_lane.querySelector(".fader"), st.db);
    }
    applyMaster(); applyGains(); drawAll();
  };

  buildMaster();
  // the Reset mix button's tooltip through the shell's [data-tip] (index.html gives it as a translated title)
  const resetTip = () => { const r = $("mx-reset"); if (r && r.title) { r.dataset.tip = r.title; r.removeAttribute("title"); } };
  resetTip();

  // a UI language switch: i18n.js has just put the index.html texts back (the Reset mix title included); set this
  // module's texts again in place (no rebuild: the stems keep playing)
  function relabel() {
    resetTip();
    labelMaster(); applyMaster();
    for (const l of S.lanes) labelLane(l);
    labelSpan();
    if (S.man) { paintState(); details(S.man); }
    else if (S.msgFn) S.msgFn();
  }
  I18N.onChange(relabel);

  return {
    // a clip / look-around window: the same t0 / t1 the clip was rendered with
    loadWindow: (recId, t0, t1, isCurrent) => request(`/api/recordings/${recId}/stems?t0=${t0}&t1=${t1}`, "window", isCurrent),
    loadFull: (recId, isCurrent) => request(`/api/recordings/${recId}/stems?full=1`, "full", isCurrent),
    clear, paint, live,
    redraw: () => relayout(),             // after the panel is shown again (waveforms are not drawn while hidden)
    state: S, master: M,
  };
})();
window.MX = MX;
