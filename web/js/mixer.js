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
   column widths and gaps freely. Under the lanes, a Track details disclosure lists the OBS names and roles. */
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
  const ROLE_LABEL = { mic: "mic", voip: "squad", voice: "voice", game: "game", desktop: "desktop", music: "music",
                       silent: "digital silence", dup: "duplicate of another track", other: "other", unknown: "unknown" };

  /* ---------- persisted state ---------- */
  const load = () => { try { return JSON.parse(localStorage.getItem(LS_KEY) || "{}") || {}; } catch (e) { return {}; } };
  const save = (o) => { try { localStorage.setItem(LS_KEY, JSON.stringify(o)); } catch (e) { } };
  let saved = load();
  function stateFor(t) {
    const s = saved[t.idx] || {};
    return { db: s.db != null ? s.db : 0, mute: s.mute != null ? s.mute : !t.in_mix, solo: !!s.solo };
  }
  function remember(lane) { saved[lane.t.idx] = { db: lane.db, mute: lane.mute, solo: lane.solo }; save(saved); }
  function masterSaved() {
    const s = saved.master || {};
    return { db: s.db != null ? s.db : 0, mute: !!s.mute, dim: false, dimDb: DIM_STEPS.includes(s.dimDb) ? s.dimDb : -15 };
  }
  const M = Object.assign(masterSaved(), { el: null, canvas: null, level: [0, 0], hold: [0, 0], holdT: [0, 0] });
  function rememberMaster() { saved.master = { db: M.db, mute: M.mute, dimDb: M.dimDb }; save(saved); }

  const dbGain = (db) => db <= FADER_MIN ? 0 : Math.pow(10, db / 20);
  const dbText = (db) => db <= FADER_MIN ? "-\u221e" : (db > 0 ? "+" : "") + db.toFixed(1);
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
      d.title = `DIM: listen ${-M.dimDb} dB quieter without changing the mix (monitoring only, like Resolve)\nright-click: change the amount (${DIM_STEPS.map(x => -x).join(" / ")} dB)`;
      M.el.querySelector(".db").textContent = dbText(M.db) + (M.dim ? `  DIM ${M.dimDb}` : "");
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
  const faderHTML = (db, what) => `<input type="range" class="fader" min="${FADER_MIN}" max="${FADER_MAX}" step="0.1" value="${db}" style="--p:${fillOf(db)}"
      aria-label="${what}" title="${what} (dB) - drag, mouse wheel 0.5 dB (shift 0.1 dB), double-click: 0 dB, bottom stop: -inf">`;

  /* ---------- A0 Master (built once, always on top) ---------- */
  function buildMaster() {
    const el = document.createElement("div");
    el.className = "lane master";
    el.innerHTML = `<div class="lctl"><div class="lrow"><span class="lnum">A0</span><span class="lname">Master</span>
          <button class="sm ms m" title="mute the master">M</button><button class="sm ms d">DIM</button><span class="db"></span></div>
        <div class="lrow fad">${faderHTML(M.db, "master level")}</div>
        <div class="meters" title="master peak level, L / R (before DIM) - click: reset the peak hold"><span class="meter"><i></i><b></b></span><span class="meter"><i></i><b></b></span></div></div>
      <div class="wf"><canvas class="wave" title="the mix as set above (estimated from the track peaks) - red = over 0 dBFS. click: seek"></canvas></div>`;
    $("mx-master").appendChild(el);
    M.el = el; M.canvas = el.querySelector("canvas");
    el.querySelector(".ms.m").onclick = () => { M.mute = !M.mute; rememberMaster(); applyMaster(); };
    const d = el.querySelector(".ms.d");
    d.onclick = () => { M.dim = !M.dim; applyMaster(); };
    d.oncontextmenu = (e) => {
      e.preventDefault(); e.stopPropagation();
      M.dimDb = DIM_STEPS[(DIM_STEPS.indexOf(M.dimDb) + 1) % DIM_STEPS.length];
      rememberMaster(); applyMaster(); toast(`DIM amount ${M.dimDb} dB`);
    };
    wireFader(el.querySelector(".fader"), () => M.db, (x) => { M.db = x; applyMaster(); }, rememberMaster);
    M.canvas.onclick = (e) => seekAt(M.canvas, e);
    el.querySelector(".meters").onclick = () => {
      M.hold = [0, 0]; M.holdT = [0, 0];
      el.querySelectorAll(".meter b").forEach(b => { b.style.opacity = 0; });
    };
    applyMaster();
  }

  /* ---------- building the track lanes ---------- */
  function teardown() {
    for (const l of S.lanes) {
      try { if (l.audio) { l.audio.pause(); l.audio.removeAttribute("src"); l.audio.load(); } } catch (e) { }
      try { if (l.src) l.src.disconnect(); if (l.gain) l.gain.disconnect(); if (l.an) l.an.disconnect(); } catch (e) { }
    }
    S.lanes = []; S.ready = false; S.settled = false; S.man = null; S.key = null;
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
    if (live()) setState("tracks live", "live");
    else if (S.settled && !S.ready) setState("no playable tracks (playing the clip's own mix)", "warn");
    else if (S.ctx && S.ctx.state === "suspended") setState("click anywhere to enable the track mix (playing the clip's own mix)", "warn");
    else if (!S.ready) setState("loading tracks... (playing the clip's own mix)", "");
    else setState("playing the clip's own mix", "warn");
  }

  async function request(url, mode, isCurrent) {
    // isCurrent(): does the player still show the window these tracks are for?
    const my = ++S.token;
    teardown();
    // a clip that finishes rendering while the Calibration page is open must not show the panel there;
    // closeCalibration shows it again once tracks are loaded
    if (!(typeof ST !== "undefined" && ST.view !== "normal")) $("mixer").hidden = false;
    $("mx-lanes").innerHTML = `<div class="empty" style="padding:14px">loading audio tracks...</div>`;
    setState("loading tracks...", "");
    let man;
    try { man = await api(url); }
    catch (err) {
      if (my !== S.token) return;
      $("mx-lanes").innerHTML = `<div class="empty" style="padding:14px">${esc(err.message)}${mode === "full" ? `<br><br><button class="sm" id="mx-rebuild">Rebuild proxy with tracks</button>` : ""}</div>`;
      setState("", "");
      const b = $("mx-rebuild"); if (b) b.onclick = () => { if (window.PL && PL.state.rec) post(`/api/recordings/${PL.state.rec.id}/proxy`).then(() => { toast("proxy rebuild queued"); b.outerHTML = `<span class="badge">queued</span>`; }).catch(e => toast(e.message, 5000)); };
      return;
    }
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
    $("mx-span").textContent = mode === "full" ? `Whole recording ${tc(man.t0)} \u2013 ${tc(man.t1)}` : `Clip window ${tc(man.t0)} \u2013 ${tc(man.t1)}`;
    const ctx = ensureCtx();
    const holder = $("mx-lanes"); holder.innerHTML = "";
    const pending = [];
    for (const t of man.tracks) {
      const st = stateFor(t);
      const silent = t.role === "silent" || t.peak_db <= -90;
      const lane = { t, db: st.db, mute: st.mute, solo: st.solo, audio: null, src: null, gain: null, an: null, el_lane: null, canvas: null, level: 0 };
      const el = document.createElement("div");
      el.className = `lane role-${t.role}${silent ? " silent" : ""}`;
      const col = (t.role === "silent" || t.role === "dup") ? "var(--dim)" : (ROLE_COLOR[t.role] || "var(--dim)");
      const tip = `OBS track ${t.idx + 1}: ${t.name || "(no name)"}\nrole: ${ROLE_LABEL[t.role] || t.role}`
        + (t.normalized ? "\nlifted like in the clip mix" : "") + (t.in_mix ? "\nin the clip mix" : "\nnot in the clip mix");
      el.innerHTML = `<div class="lctl"><div class="lrow"><span class="lnum" style="color:${col}" title="${esc(tip)}">A${t.idx + 1}</span>
          <button class="sm ms m" title="mute (wins over solo)">M</button><button class="sm ms s" title="solo (ctrl / shift + click: solo only this one)">S</button>
          <span class="db"></span></div>
        <div class="lrow fad">${faderHTML(st.db, "track volume")}</div>
        <div class="meters" title="level after the fader"><span class="meter"><i></i></span></div></div>
        <div class="wf"><canvas class="wave" title="${silent ? "digital silence" : "click: seek"}"></canvas></div>`;
      holder.appendChild(el);
      lane.el_lane = el; lane.canvas = el.querySelector("canvas");
      el.querySelector(".ms.m").onclick = () => { lane.mute = !lane.mute; remember(lane); applyGains(); drawAll(); };
      el.querySelector(".ms.s").onclick = (e) => {
        if (e.ctrlKey || e.shiftKey) for (const o of S.lanes) if (o !== lane) { o.solo = false; remember(o); }
        lane.solo = !lane.solo; remember(lane); applyGains(); drawAll();
      };
      wireFader(el.querySelector(".fader"), () => lane.db, (x) => { lane.db = x; applyGains(); }, () => { remember(lane); drawLane(lane); });
      lane.canvas.onclick = (e) => seekAt(lane.canvas, e);
      if (!silent && ctx) {
        const a = new Audio();
        a.preload = "auto"; a.crossOrigin = "anonymous"; a.src = t.url;
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
        l.audio = null; l.el_lane.classList.add("silent"); l.canvas.title = "this track could not be loaded";
      }
      S.settled = true;
      S.ready = S.lanes.some(l => l.audio);
      applyAudioRouting(); syncNow(true); paintState();
    });
    paintState();
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
    const rows = man.tracks.map(t => `<tr><td class="mono">A${t.idx + 1}</td><td>${esc(t.name || "(no name)")}</td>
        <td><span class="badge${t.role === "mic" || t.role === "voip" || t.role === "voice" ? " acc" : ""}">${esc(ROLE_LABEL[t.role] || t.role)}</span></td>
        <td>${t.in_mix ? "yes" : "no"}${t.normalized ? " (lifted)" : ""}</td>
        <td class="num">${t.peak_db != null && isFinite(t.peak_db) ? t.peak_db.toFixed(1) + " dB" : "-"}</td></tr>`).join("");
    d.innerHTML = `<summary>${icon("chev", "chev")}Track details <span class="faint">OBS names, roles, clip mix, peaks</span></summary>
      <div class="mxdet-b"><table class="t"><thead><tr><th>Track</th><th>OBS name</th><th>Role</th><th>In the clip mix</th><th>Peak</th></tr></thead><tbody>${rows}</tbody></table>
      <p class="faint t-xs">Default mix = the clip mix: game + mic + squad on; desktop, music and silent tracks muted. Mute wins over solo.</p></div>`;
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
  { const r = $("mx-reset"); if (r && r.title) { r.dataset.tip = r.title; r.removeAttribute("title"); } }

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
