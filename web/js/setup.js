/* First-run setup wizard + add-on packs.
   - dialog#dlg-setup: six steps (Welcome, Footage, Your name, Speech, Streams, Start) against GET / POST /api/setup.
     Plain words only on its screens (no tool names); the app picks the technical add-ons from the answers.
     Opened on boot while the server says setup is not done, from the palette and Settings > Add-ons ("Run setup
     again"). On a first run Escape does not close it: only "Skip setup" (which still posts done) or Finish do.
   - Add-on packs (CUDA speech libraries, deno, a newer yt-dlp, Whisper models): one shared state PK fed by
     GET /api/packs, the SSE "packs" events (app.js forwards them to SU.onPackEvent) and, while anything downloads, a
     2 s poll of /api/packs as a fallback (an SSE reconnect can drop events). Three views render it: the wizard's
     progress list, Settings > Add-ons (#ao-list) and the status bar item #sb-dl.
   - Release check: GET /api/updates -> the status bar link #sb-update; Settings > Updates "Check now" forces it.
   Loaded after core.js and BEFORE app.js: it only defines things at load time; app.js globals (ST, openSettings,
   closeDrawer, refreshStatus, loadRecs) are looked up when a function runs. */
"use strict";

// ST is a top-level const in app.js: a global binding but NOT a window property, so test it with typeof
const hasST = () => typeof ST !== "undefined" && !!ST;
const SU = (() => {
  const STEPS = ["Welcome", "Footage", "Your name", "Speech", "Streams", "Start"];
  // display sizes when the server sends none (known without network)
  const MODEL_MB = { "tiny.en": 75, "small.en": 480, "medium.en": 1500, "large-v3": 3100, "large-v3-turbo": 1600 };
  const PK = new Map();                      // pack / model id -> its latest status
  let gpu = null;                            // packs.gpu_info() as last seen
  let curModel = "";                         // the speech model in use (packs.status current_model)
  const S = { data: null, open: false, first: false, step: 0, maxStep: 0, roots: [], checks: {}, aliases: [], squad: [],
    sttEnabled: true, speech: "", vods: false, vodsEdit: false, vodsDir: "", saved: false, started: [], saving: false,
    picking: false, nameWarned: false, skipRoots: false, offline: false, err: "", vodCheck: null };

  /* ---------------- small helpers ---------------- */
  // a typed / pasted path: quotes and trailing slashes off, a bare drive keeps its backslash ("D:\")
  const norm = (p) => { const t = String(p || "").trim().replace(/^"(.*)"$/, "$1").trim(); return /^[A-Za-z]:[\\/]*$/.test(t) ? t.slice(0, 2) + "\\" : t.replace(/[\\/]+$/, ""); };
  const pkey = (p) => norm(p).toLowerCase().replace(/\//g, "\\");
  const mb = (m) => m == null || isNaN(m) ? "" : m >= 1000 ? (m / 1000).toFixed(1) + " GB" : Math.round(m) + " MB";
  const isActive = (p) => !!p && (p.state === "downloading" || p.state === "extracting");
  const isModel = (id) => String(id || "").startsWith("model:");
  const titleOf = (p) => p.title || (p.name ? "Speech model " + p.name : String(p.id || "add-on"));
  // "done" only says the job finished: an up-to-date yt-dlp ends done with nothing installed (note says why)
  const isInstalled = (p) => !!p && !!(p.installed || p.cached);
  const urlId = (id) => String(id).split(":").map(encodeURIComponent).join(":");
  const domId = (id) => "pk-" + String(id).replace(/[^A-Za-z0-9_-]/g, "_");
  const firstRun = () => !!(hasST() && ST.settings && ST.settings.setup && ST.settings.setup.done === false);
  async function req(method, url, body) {
    const r = await fetch(url, { method, headers: _hdrs(!!body), body: body ? JSON.stringify(body) : undefined });
    let j = null; try { j = await r.json(); } catch (e) { }
    if (!r.ok) { const e = new Error((j && j.detail) || r.statusText || ("HTTP " + r.status)); e.status = r.status; throw e; }
    return j || {};
  }

  /* ---------------- pack state (shared by the wizard, Settings > Add-ons and the status bar) ---------------- */
  function ingest(st) {
    if (!st) return;
    for (const p of [].concat(st.packs || [], st.models || [])) merge(p);
    if (st.gpu) gpu = st.gpu;
    if (st.current_model) curModel = st.current_model;
  }
  // merge one status; an install that stops being active (done / failed / cancelled) says so once
  function merge(p) {
    if (!p || !p.id) return;
    const prev = PK.get(p.id); const was = prev && prev.state;
    const cur = Object.assign({}, prev || {}, p); PK.set(p.id, cur);
    if (!prev || !isActive(prev) || isActive(cur)) return;
    if (cur.state === "done") {
      if (cur.id === "cuda") toast("Fast speech on your graphics card is ready: used from the next transcription", 6000);
      else toast(isInstalled(cur) ? `${titleOf(cur)}: ready` : `${titleOf(cur)}: ${cur.note || "nothing to install"}`, 5000);
    } else if (cur.state === "failed") toast(`${titleOf(cur)}: the download stopped. Settings > Add-ons can try again.`, 8000);
    else if (cur.state === "cancelled" && was) toast(`${titleOf(cur)}: stopped (Retry goes on where it stopped)`, 4000);
  }
  let renderT = null;
  function renderAll() {
    // SSE can send ~4 updates a second per pack: one paint per animation frame is plenty
    if (renderT) return;
    renderT = requestAnimationFrame(() => { renderT = null; renderStatusbar(); renderAddons(); if (S.open && S.saved) renderProgress(); });
  }
  let pollT = null;
  function ensurePoll() {
    if (pollT || ![...PK.values()].some(isActive)) return;
    pollT = setInterval(async () => {
      try { ingest(await api("/api/packs")); } catch (e) { }
      renderAll();
      if (![...PK.values()].some(isActive)) { clearInterval(pollT); pollT = null; }
    }, 2000);
  }
  async function refreshPacks() {
    try { const st = await api("/api/packs"); for (const p of [].concat(st.packs || [], st.models || [])) merge(p); if (st.gpu) gpu = st.gpu; if (st.current_model) curModel = st.current_model; }
    catch (e) { return false; }
    renderAll(); ensurePoll(); return true;
  }
  // SSE "packs": the PackStatus itself (bus.publish("packs", **status)) or wrapped as {pack: ...}
  function onPackEvent(d) {
    const p = (d && (d.pack || d.status)) || d;
    if (!p || !p.id) return;
    merge(p); renderAll(); ensurePoll();
  }
  async function install(id) {
    try { const r = await req("POST", `/api/packs/${urlId(id)}/install`); merge(Object.assign({ id }, r && r.id ? r : { state: "downloading", progress: 0 })); }
    catch (e) { toast(`${titleOf(PK.get(id) || { id })} could not start: ${e.message}`, 6000); }
    renderAll(); ensurePoll();
  }
  async function cancel(id) {
    try { await req("POST", `/api/packs/${urlId(id)}/cancel`); } catch (e) { toast(e.message, 5000); }
    refreshPacks();
  }
  async function remove(id) {
    const p = PK.get(id) || { id };
    const where = isModel(id) ? " Other programs on this PC that use the same speech model (Hugging Face cache) lose it too." : "";
    if (!confirm(`Remove ${titleOf(p)}? Its downloaded files are deleted; you can install it again later.${where}`)) return;
    try {
      const r = await req("POST", `/api/packs/${urlId(id)}/remove`);
      toast(r && r.ok === false ? `${titleOf(p)} could not be removed` : `${titleOf(p)} removed`, r && r.ok === false ? 5000 : 3000);
    } catch (e) { toast(e.message || `${titleOf(p)} could not be removed`, 6000); }
    refreshPacks();
  }

  function progText(p) {
    const pc = Math.round((p.progress || 0) * 100);
    const by = p.total ? ` · ${fmtBytes(p.bytes || 0)} of ${fmtBytes(p.total)}` : p.bytes ? ` · ${fmtBytes(p.bytes)}` : "";
    return (p.state === "extracting" ? "installing " : "") + pc + " %" + by;
  }
  // one row (Settings > Add-ons and the wizard's progress list); sig decides between an in-place bar update and a
  // re-render, so a focused button survives the 4-a-second progress events
  function rowSig(p, mode) { return [p.state, isInstalled(p), p.available !== false, !!p.error, mode, p.name && p.name === curModel].join("|"); }
  function packRow(p, mode) {
    if (mode === "wiz") return wizRow(p);
    const act = isActive(p), inst = isInstalled(p) && !act, avail = p.available !== false;
    // already covered on this PC some other way (Node.js for YouTube, the CUDA libraries in the environment)
    const notNeeded = !inst && avail && ((p.id === "cuda" && p.cuda_ready === true) || (p.id === "deno" && p.needed === false));
    const badges = [];
    if (inst) badges.push(`<span class="badge ok">${icon("check")}installed${p.version && !isModel(p.id) ? " " + esc(p.version) : ""}</span>`);
    if (isModel(p.id) && p.name && p.name === curModel) badges.push(`<span class="badge acc">in use</span>`);
    if (p.recommended && !inst && !notNeeded) badges.push(`<span class="badge acc">recommended</span>`);
    if (notNeeded) badges.push(`<span class="badge">not needed on this PC</span>`);
    if (p.state === "failed") badges.push(`<span class="badge bad">failed</span>`);
    if (p.state === "cancelled") badges.push(`<span class="badge warn">cancelled</span>`);
    if (!avail) badges.push(`<span class="badge">not needed on this PC</span>`);
    const desc = [p.desc || "", p.size_mb ? "about " + mb(p.size_mb) : (p.name && MODEL_MB[p.name] ? "about " + mb(MODEL_MB[p.name]) : "")].filter(Boolean).join(" · ");
    let btns = "";
    if (act) btns = `<button type="button" class="btn sm" data-pk="cancel" data-id="${esc(p.id)}">${icon("x")}Cancel</button>`;
    else if (mode !== "wiz") {
      if (inst) btns = `<button type="button" class="btn sm ghost danger" data-pk="remove" data-id="${esc(p.id)}">${icon("trash")}Remove</button>`;
      else if (avail) btns = `<button type="button" class="btn sm${notNeeded ? " ghost" : p.recommended ? " pri" : ""}" data-pk="install" data-id="${esc(p.id)}">${icon("download")}${p.state === "failed" || p.state === "cancelled" ? "Retry" : notNeeded ? "Install anyway" : p.id === "ytdlp" ? "Update" : "Install"}</button>`;
    } else if (p.state === "failed" || p.state === "cancelled") btns = `<button type="button" class="btn sm" data-pk="install" data-id="${esc(p.id)}">${icon("refresh")}Retry</button>`;
    // reason / note are informational even for an available pack ("Node.js is installed -- ..."); not once installed
    const why = !inst ? (p.reason || p.note || "") : (p.note || "");
    const reason = why ? `<div class="pk-note">${esc(why)}</div>` : "";
    const err = p.state === "failed" && p.error ? `<div class="pk-err">${esc(p.error)}</div>` : "";
    const done = mode === "wiz" && inst ? `<div class="pk-note ok">${p.id === "cuda" ? "ready: used from the next transcription" : "ready"}</div>`
      : mode === "wiz" && p.state === "done" && !p.note ? `<div class="pk-note ok">finished</div>` : "";
    const prog = act ? `<div class="pk-prog"><div class="pg"><i style="width:${pct(p.progress)}"></i></div><span class="num" data-pt>${esc(progText(p))}</span></div>` : "";
    return `<div class="pk-row${act ? " act" : ""}${!avail ? " na" : ""}" id="${domId(p.id)}${mode === "wiz" ? "-w" : ""}" data-sig="${esc(rowSig(p, mode))}">
      <div class="pk-main"><div class="pk-t"><b>${esc(titleOf(p))}</b>${badges.join("")}</div>${desc ? `<div class="pk-d">${esc(desc)}</div>` : ""}${prog}${reason}${err}${done}</div>
      <div class="pk-acts">${btns}</div></div>`;
  }
  // write rows into a container, updating active rows in place when their shape did not change
  function paintRows(box, list, mode) {
    const want = list.map(p => ({ p, id: domId(p.id) + (mode === "wiz" ? "-w" : ""), sig: rowSig(p, mode) }));
    const have = Array.from(box.children).map(c => c.id);
    if (have.length === want.length && want.every((w, i) => have[i] === w.id && box.children[i].dataset.sig === w.sig)) {
      for (const w of want) {
        const row = box.querySelector("#" + w.id); if (!row || !isActive(w.p)) continue;
        const bar = row.querySelector(".pg > i"); if (bar) bar.style.width = pct(w.p.progress);
        const t = row.querySelector("[data-pt]"); if (t) t.textContent = progText(w.p);
      }
      return;
    }
    const keep = document.activeElement && box.contains(document.activeElement) ? document.activeElement : null;
    const kid = keep && keep.dataset ? keep.dataset.id + "|" + keep.dataset.pk : null;
    box.innerHTML = list.map(p => packRow(p, mode)).join("");
    if (kid) { const b = Array.from(box.querySelectorAll("[data-pk]")).find(x => x.dataset.id + "|" + x.dataset.pk === kid); if (b) b.focus(); }
  }
  function wireRows(box) {
    if (box.dataset.wired) return; box.dataset.wired = "1";
    box.addEventListener("click", (e) => {
      const b = e.target.closest("[data-pk]"); if (!b) return;
      const id = b.dataset.id, a = b.dataset.pk;
      if (a === "install") install(id); else if (a === "cancel") cancel(id); else if (a === "remove") remove(id);
    });
  }

  /* ---------------- status bar: download progress + "vX available" ---------------- */
  function renderStatusbar() {
    const el = $("sb-dl"); if (!el) return;
    const act = [...PK.values()].filter(isActive);
    if (!act.length) { el.hidden = true; return; }
    const tot = act.reduce((a, p) => a + (p.total || 0), 0), got = act.reduce((a, p) => a + (p.bytes || 0), 0);
    const pr = tot ? got / tot : act.reduce((a, p) => a + (p.progress || 0), 0) / act.length;
    const what = act.length === 1 ? titleOf(act[0]) : `${act.length} add-ons`;
    const verb = act.every(p => p.state === "extracting") ? "Installing" : "Downloading";
    $("sb-dl-t").textContent = `${verb} ${what} ${Math.round(pr * 100)} %`;
    el.querySelector(".pg > i").style.width = pct(pr);
    el.dataset.tip = act.map(p => `${titleOf(p)}: ${progText(p)}`).join("\n") + "\nClick: Settings > Add-ons";
    el.hidden = false;
  }
  let upd = null;
  async function checkUpdates(force) {
    try { upd = await api("/api/updates" + (force ? "?force=1" : "")); }
    catch (e) { if (force) throw e; return null; }
    const a = $("sb-update");
    if (a) {
      const show = !!(upd && upd.newer && upd.latest);
      a.hidden = !show;
      if (show) { a.textContent = `v${String(upd.latest).replace(/^v/i, "")} available`; a.href = upd.url || "#"; a.title = "A newer WardogClips release: open its download page"; }
    }
    return upd;
  }

  /* ---------------- Settings > Add-ons / Updates (app.js openDrawer renders the mounts) ---------------- */
  function renderAddons() {
    const box = $("ao-list"); if (!box) return;
    wireRows(box); const mbox = $("ao-models"); if (mbox) wireRows(mbox);
    const all = [...PK.values()];
    const packs = all.filter(p => !isModel(p.id)), models = all.filter(p => isModel(p.id));
    if (!all.length) { box.innerHTML = `<div class="hint">The add-on list could not be loaded. Close Settings and open it again.</div>`; if (mbox) mbox.innerHTML = ""; }
    else { paintRows(box, packs, "set"); if (mbox) paintRows(mbox, models, "set"); }
    const g = $("ao-gpu");
    if (g) g.innerHTML = gpu ? (gpu.nvidia ? `<span class="sd ok"></span><span>${esc(gpu.name || "NVIDIA graphics card")}${gpu.vram_mb ? ` · ${(gpu.vram_mb / 1024).toFixed(0)} GB` : ""}${gpu.driver ? ` · driver ${esc(gpu.driver)}` : ""}: ${cudaReady() ? "speech turns into text on the graphics card" : "speech can turn into text on the graphics card once the speech engine below is installed"}</span>`
      : `<span class="sd warn"></span><span>No NVIDIA graphics card: speech turns into text on the processor (slower; the Standard speech model is the sensible pick)</span>`) : "";
  }
  // called by openDrawer once its markup is in place
  function mountSettings() {
    renderAddons(); refreshPacks();
    const y = $("ao-ytdlp"); if (y) y.onclick = () => install("ytdlp");
    const rs = $("ao-setup"); if (rs) rs.onclick = () => { if (typeof closeDrawer === "function") closeDrawer(); open({}); };
    const cn = $("up-check");
    if (cn) cn.onclick = async () => {
      const line = $("up-res"); cn.disabled = true; line.textContent = "checking...";
      try {
        const u = await checkUpdates(true);
        if (!u || u.disabled) line.innerHTML = `<span class="muted">No repository set: fill in the field above (owner/name) to check for releases.</span>`;
        else if (u.newer) line.innerHTML = `<span class="sd acc"></span><span>v${esc(String(u.latest).replace(/^v/i, ""))} is available (you have v${esc(u.current || "?")}) · <a href="${esc(u.url || "#")}" target="_blank" rel="noopener">open the release page</a></span>`;
        else line.innerHTML = `<span class="sd ok"></span><span>You have the newest version (v${esc(u.current || (ST.status && ST.status.version) || "?")})${u.checked ? ` · checked ${esc(String(u.checked).replace("T", " ").slice(0, 16))}` : ""}</span>`;
      } catch (e) { line.innerHTML = `<span class="sd bad"></span><span>check failed: ${esc(e.message)}</span>`; }
      finally { cn.disabled = false; }
    };
  }

  /* ---------------- the wizard ----------------
     Plain words for first-time users: no tool names on these screens, one question per step, the app picks the
     technical add-ons itself (the screen only says "about X to download"), no option that does nothing on this PC, no
     raw error text (a failed check is retried quietly). Exact models / removing packs / the downloader update live in
     Settings > Add-ons only. */
  function packsFrom(d) { const p = (d && d.packs) || {}; ingest(p); }
  async function load() {
    S.offline = false;
    try { S.data = await api("/api/setup"); packsFrom(S.data); return; }
    catch (e) { S.offline = true; }
    // no setup service (older server / the route is missing): build what we can from the settings
    const s = (hasST() && ST.settings) || {};
    S.data = { done: !!(s.setup && s.setup.done), detected: {}, recommended: {},
      current: { roots: (s.library || {}).roots || [], aliases: (s.player || {}).aliases || [], squad: (s.player || {}).squad || [],
        vods_dir: (s.vods || {}).dir || "", vods_dir_effective: "", stt_model: (s.stt || {}).model || "medium.en", stt_enabled: (s.stt || {}).enabled !== false } };
    await refreshPacks();
  }
  const det = () => (S.data && S.data.detected) || {};
  const cur = () => (S.data && S.data.current) || {};
  const gpuNow = () => det().gpu || gpu || {};
  const modelOf = (name) => PK.get("model:" + name) || { id: "model:" + name, name, size_mb: MODEL_MB[name], cached: false };
  const sizeOf = (id) => { const p = PK.get(id); return (p && p.size_mb) || (isModel(id) ? MODEL_MB[id.slice(6)] || 0 : 0); };
  const vodDirShown = () => S.vodsDir || cur().vods_dir_effective || "";

  // the speech cards this PC gets: Fast (NVIDIA only) + Standard, or Standard + Light without an NVIDIA card
  function speechChoices() {
    const nv = !!gpuNow().nvidia, ready = cudaReady(), c = PK.get("cuda");
    const rec = (S.data && S.data.recommended && S.data.recommended.stt_model) || "";
    const out = [];
    if (nv) out.push({ key: "fast", name: "Fast", model: rec || "medium.en", cuda: !ready && !!c && c.available !== false && !isInstalled(c),
      text: "Uses your NVIDIA graphics card. Best results." });
    out.push({ key: "standard", name: "Standard", model: "small.en", cuda: false,
      text: nv ? (ready ? "Lighter and a little less accurate." : "Runs on the processor: slower, no graphics card download.") : "Runs on the processor. Good results." });
    if (!nv) out.push({ key: "light", name: "Light", model: "tiny.en", cuda: false, text: "Fastest, less accurate." });
    // a re-run whose model was picked in Settings > Add-ons and is none of the above: offer to keep it
    const cm = cur().stt_model;
    if (!S.first && cm && !out.some(o => o.model === cm)) out.push({ key: "keep", name: "Keep my current choice", model: cm, cuda: false, text: "The speech model picked in Settings > Add-ons." });
    return out;
  }
  // the downloads one card needs (what is already on this PC is left out)
  function choiceIds(ch) {
    const ids = [];
    if (ch.cuda) ids.push("cuda");
    const m = modelOf(ch.model); if (!isInstalled(m)) ids.push("model:" + ch.model);
    return ids;
  }
  const curChoice = () => speechChoices().find(c => c.key === S.speech) || speechChoices()[0];
  // YouTube needs the JS helper when the backend says so (no Node.js / deno on this PC and the pack not installed)
  function denoNeeded() {
    const p = PK.get("deno"); if (!p || p.available === false || isInstalled(p)) return false;
    return p.needed != null ? !!p.needed : (!det().node && !det().deno);
  }

  function initFromData() {
    const d = S.data || {}, c = d.current || {}, dt = d.detected || {};
    S.roots = []; S.checks = {};
    const add = (p, src, on) => { const k = pkey(p); if (!k || S.roots.some(r => r.k === k)) return; S.roots.push({ path: norm(p), k, src, on }); };
    (c.roots || []).forEach(p => add(p, "current", true));
    // OBS's folders are pre-ticked on a first run; a re-run lists them unticked unless they are roots already
    ((dt.obs || {}).rec_paths || []).forEach(p => add(p, "obs", S.first || !(c.roots || []).length));
    S.aliases = (c.aliases || []).slice(); S.squad = (c.squad || []).slice();
    S.sttEnabled = c.stt_enabled !== false;
    // a first run takes the first card (Fast on an NVIDIA PC); a re-run the card of the model in use
    const ch = speechChoices();
    S.speech = (S.first ? ch[0] : (ch.find(x => x.model === c.stt_model) || ch[0])).key;
    S.vodsDir = c.vods_dir || "";
    const dn = PK.get("deno");
    S.vods = !S.first && (!!c.vods_dir || (!!dn && isInstalled(dn)));
    S.vodsEdit = false;
    S.step = 0; S.maxStep = 0; S.saved = false; S.started = []; S.err = ""; S.nameWarned = false; S.skipRoots = false; S.vodCheck = null;
  }

  async function open(opts) {
    const d = $("dlg-setup"); if (!d) return;
    if (d.open) { d.focus(); return; }
    S.first = !!(opts && opts.first) || firstRun();
    d.classList.toggle("first", S.first);
    d.returnValue = "";
    $("su-body").innerHTML = `<div class="su-loading"><div class="spin"></div>Looking at this PC...</div>`;
    $("su-steps").innerHTML = ""; $("su-msg").textContent = "";
    S.open = true; d.showModal();
    await load();
    initFromData();
    const v = (hasST() && ST.status && ST.status.version) || "";
    $("su-ver").textContent = v ? "version " + v : "";
    render();
    $("su-next").focus();
  }
  function close() {
    const d = $("dlg-setup"); S.open = false; S.allowClose = true;
    if (d && d.open) d.close("done");
    S.allowClose = false;
  }

  // ---- steps ----
  const hwl = (cls, t) => `<div class="sline su-hwl"><span class="sd ${cls}"></span><span>${t}</span></div>`;
  function hwLines() {
    const dt = det(), g = gpuNow(), ff = dt.ffmpeg || {}, obs = dt.obs || {}, rv = dt.resolve || {};
    if (S.offline && !Object.keys(dt).length) return "";
    const out = [];
    out.push(g.nvidia ? hwl("ok", `Graphics card: <b>${esc(g.name || "NVIDIA")}</b> — fast transcription available`)
      : hwl("", "No NVIDIA graphics card — transcription runs on the processor (slower)"));
    if (obs.found) out.push(hwl("ok", (obs.rec_paths || []).length ? "OBS found — we'll suggest its recording folder" : "OBS found — you'll pick its recording folder next"));
    else out.push(hwl("", "OBS not found — you'll pick your recordings folder next"));
    if (rv.installed) out.push(hwl("ok", "DaVinci Resolve found — your best moments can go straight into it"));
    // the video tools ship with the app: only worth a line when they are broken
    if (dt.ffmpeg && !ff.ok) out.push(hwl("bad", "<b>Video tools are missing</b> — reinstall WardogClips"));
    return `<div class="su-sub">This PC</div><div class="su-hw">${out.join("")}</div>`;
  }
  function stepWelcome() {
    return `<h2 tabindex="-1">Welcome to WardogClips</h2>
      <p class="lead">WardogClips watches your game recordings for you and finds the moments worth keeping: kills, assists, deaths and the things you and your squad said.</p>
      <p class="lead"><b>We'll ask 4 quick things.</b></p>
      ${hwLines()}
      <p class="hint">WardogClips only reads your videos. It never touches the game or changes your files, and everything stays on this PC. You can change every answer later in Settings.</p>`;
  }
  function rootInfo(c) {
    if (!c || c.pending) return `<span class="faint">checking...</span>`;
    if (c.error) return `<span class="faint">can't check this folder right now</span>`;
    if (!c.exists || !c.is_dir) return `<span class="bad-t">Folder not found</span>`;
    if (!c.n_videos) return `<span class="warn-t">No videos yet</span>`;
    const many = c.capped || c.n_videos >= 2000;
    return `<span class="ok-t">${c.n_videos}${many ? "+" : ""} video${c.n_videos === 1 && !many ? "" : "s"} found</span>`;
  }
  function rootRow(r, i) {
    const src = r.src === "obs" ? `<span class="badge acc">from OBS</span>` : r.src === "current" ? `<span class="badge">in use</span>` : "";
    return `<div class="su-root${r.on ? " on" : ""}"><input type="checkbox" id="su-r${i}" data-ri="${i}" ${r.on ? "checked" : ""} aria-label="use this folder">
      <label for="su-r${i}" class="mono path" title="${esc(r.path)}">${esc(r.path)}</label><span class="meta">${src}<span class="info">${rootInfo(S.checks[r.k])}</span></span>
      <button type="button" class="ib sm" data-rx="${i}" title="Remove from the list" aria-label="remove ${esc(r.path)}">${icon("x")}</button></div>`;
  }
  const noRoots = () => `<div class="su-none">No folder yet: add the folder OBS saves your recordings in.</div>`;
  function stepFolders() {
    return `<h2 tabindex="-1">Where does OBS save your recordings?</h2>
      <p class="lead">Tick the folders that hold your recordings and replays. Folders inside them count too. WardogClips only reads them: nothing is moved or changed.</p>
      <div class="su-roots" id="su-roots">${S.roots.length ? S.roots.map(rootRow).join("") : noRoots()}</div>
      <div class="su-add">
        <button type="button" class="btn" id="su-pick" ${S.picking ? "disabled" : ""}>${icon("folder")}${S.picking ? "Waiting for the folder window..." : "Add folder..."}</button>
        <input class="inp" type="text" id="su-path" placeholder="or paste a folder path and press Enter" autocomplete="off" spellcheck="false" aria-label="folder path">
        <button type="button" class="btn" id="su-path-add">Add</button>
      </div>
      <label class="su-check"><input type="checkbox" id="su-skiproots" ${S.skipRoots ? "checked" : ""}><span>Skip for now: I'll add folders later in Settings</span></label>`;
  }
  function chipBox(id, vals, ph) {
    return `<div class="su-chips" id="${id}" data-chips>${vals.map((v, i) => `<span class="su-tag">${esc(v)}<button type="button" data-cx="${i}" aria-label="remove ${esc(v)}">${icon("x")}</button></span>`).join("")}<input type="text" placeholder="${esc(vals.length ? "add another" : ph)}" autocomplete="off" spellcheck="false" aria-label="${esc(ph)}"></div>`;
  }
  function stepYou() {
    return `<h2 tabindex="-1">What's your in-game name?</h2>
      <p class="lead">So we can tell your kills from everyone else's in the kill feed.</p>
      <div class="su-field"><div class="su-lab"><b>Your in-game name</b><small>Exactly as the kill feed shows it, without a clan tag. Play under more than one? Add them all.</small></div>${chipBox("su-aliases", S.aliases, chipPh("su-aliases"))}</div>
      <div class="su-field"><div class="su-lab"><b>Squad mates (optional)</b><small>The people you usually play with, so their kills count as your team's.</small></div>${chipBox("su-squad", S.squad, chipPh("su-squad"))}</div>
      <p class="hint">Press Enter after each name.</p>`;
  }
  function sizeLine(ids) {
    if (!ids.length) return `<span class="badge ok">${icon("check")}already downloaded</span>`;
    const tot = ids.reduce((a, id) => a + sizeOf(id), 0);
    return tot ? `<span class="num">${ids.includes("cuda") ? "one-time download " : ""}about ${esc(mb(tot))}</span>` : "";
  }
  function stepSpeech() {
    const ch = speechChoices();
    const cards = ch.map((c, i) => `<label class="opt su-card"><input type="radio" name="su-speech" value="${esc(c.key)}" ${c.key === S.speech ? "checked" : ""}>
        <span><b>${esc(c.name)}</b>${i === 0 && c.key !== "keep" ? ` <span class="badge acc">recommended</span>` : ""}<small>${esc(c.text)}</small></span>
        ${sizeLine(choiceIds(c))}</label>`).join("");
    return `<h2 tabindex="-1">Turn speech into searchable text?</h2>
      <p class="lead">WardogClips can write down what you and your squad say, so you can search it later, and it marks the moments you got loud. It all happens on this PC: nothing is uploaded.</p>
      <label class="su-switch"><input type="checkbox" class="toggle" id="su-stt" ${S.sttEnabled ? "checked" : ""}><span><b>Yes, turn speech into text</b><small>Off: no text and no voice moments. Everything else still works.</small></span></label>
      ${S.sttEnabled ? `<div class="su-sub">Pick one</div><div class="su-cards">${cards}</div>
      <p class="hint">Want a specific speech model? Settings &gt; Add-ons lists them all.</p>` : ""}`;
  }
  function vodInfo() {
    const vc = S.vodCheck;
    if (!vc || vc.error) return "";
    if (vc.pending) return `<span class="faint">checking...</span>`;
    const free = vc.free_gb != null ? `<span class="faint"> · ${Math.round(vc.free_gb)} GB free</span>` : "";
    if (!vc.exists) return `<span class="muted">This folder will be created</span>${free}`;
    if (!vc.is_dir) return `<span class="bad-t">That's a file, not a folder: pick another</span>`;
    return `<span class="ok-t">Folder found</span>${free}`;
  }
  function stepVods() {
    const dn = PK.get("deno");
    const yes = S.vods;
    return `<h2 tabindex="-1">Stream recordings too?</h2>
      <p class="lead">Do you also want to download stream recordings (VODs) from YouTube, Twitch, Kick, TikTok or Rumble? WardogClips finds the best moments in them like in your own footage.</p>
      <div class="su-cards">
        <label class="opt su-card"><input type="radio" name="su-vods" value="yes" ${yes ? "checked" : ""}><span><b>Yes</b><small>Paste a link to a stream any time, or follow your favourite streamers.</small></span></label>
        <label class="opt su-card"><input type="radio" name="su-vods" value="no" ${yes ? "" : "checked"}><span><b>Not now</b><small>You can start any time later from the VODs button.</small></span></label>
      </div>
      ${yes ? `<div class="su-vodsbox">
        <div class="su-saveto"><span class="muted">Saved to:</span> <span class="mono path" title="${esc(vodDirShown())}">${esc(vodDirShown() || "your Videos folder")}</span>
          <button type="button" class="btn sm" id="su-vods-pick" ${S.picking ? "disabled" : ""}>${icon("folder")}${S.picking ? "Waiting..." : "Change..."}</button></div>
        ${S.vodsEdit ? `<div class="su-add"><input class="inp" type="text" id="su-vods" value="${esc(S.vodsDir)}" placeholder="${esc(cur().vods_dir_effective || "type a folder path")}" autocomplete="off" spellcheck="false" aria-label="folder for stream recordings"></div>` : ""}
        <div class="su-line" id="su-vods-line">${vodInfo()}</div>
        ${denoNeeded() ? `<div class="sline"><span class="sd"></span><span>YouTube needs a small helper (${esc(mb((dn && dn.size_mb) || 41))}) — it downloads with the rest.</span></div>` : ""}
      </div>` : ""}`;
  }
  // whisper can use the GPU: the backend's cuda_ready (pack installed, or the libraries already there some other way)
  function cudaReady() {
    const c = PK.get("cuda"), g = gpuNow();
    if (!c) return false;
    if (typeof c.cuda_ready === "boolean") return c.cuda_ready;
    if (!g.nvidia) return false;
    return isInstalled(c) || /already/i.test(c.reason || "");
  }
  function installList() {
    const out = [];
    if (S.sttEnabled) { const c = curChoice(); if (c) out.push(...choiceIds(c)); }
    if (S.vods && denoNeeded()) out.push("deno");
    return out.filter(id => { const p = PK.get(id); return !p || p.available !== false; });
  }
  const WIZ_NAMES = { cuda: "Speech engine for your graphics card", deno: "YouTube helper" };
  const wizName = (id) => WIZ_NAMES[id] || (isModel(id) ? "Speech model" : titleOf(PK.get(id) || { id }));
  function stepFinish() {
    if (S.saved) return finishProgress();
    const roots = S.roots.filter(r => r.on);
    const inst = installList();
    const total = inst.reduce((a, id) => a + sizeOf(id), 0);
    const li = (k, v) => `<div class="su-sum"><span>${esc(k)}</span><div>${v}</div></div>`;
    const ch = curChoice();
    return `<h2 tabindex="-1">Ready to start</h2>
      <p class="lead">Here is what you picked. ${roots.length ? "Press Start and WardogClips begins looking through your footage." : "Press Start to save it."}</p>
      <div class="su-summary">
        ${li("Footage", roots.length ? `${roots.length} folder${roots.length === 1 ? "" : "s"}${roots.map(r => `<div class="mono faint">${esc(r.path)}</div>`).join("")}` : `<span class="warn-t">not yet: add a folder later in Settings</span>`)}
        ${li("Your name", S.aliases.length ? S.aliases.map(esc).join(", ") : `<span class="warn-t">not set: your kills can't be told from everyone else's</span>`)}
        ${S.squad.length ? li("Squad", S.squad.map(esc).join(", ")) : ""}
        ${li("Speech", S.sttEnabled ? esc(ch ? (ch.key === "keep" ? "your current choice" : ch.name) : "on") : `<span class="faint">off</span>`)}
        ${li("Stream VODs", S.vods ? "yes" : `<span class="faint">not now</span>`)}
        ${li("Download", inst.length ? `about ${esc(mb(total))} <span class="faint">· ${inst.map(wizName).map(esc).join(", ")}</span>` : `<span class="faint">nothing to download</span>`)}
      </div>
      ${inst.length ? `<p class="hint">You can keep using WardogClips while this downloads.</p>` : ""}`;
  }
  function finishProgress() {
    return `<h2 tabindex="-1">You're all set</h2>
      <p class="lead">${S.roots.some(r => r.on) ? "WardogClips is looking through your footage now: recordings show up in the Library as they are found." : "Add the folder OBS saves to in Settings &gt; Library when you are ready."}</p>
      ${S.started.length ? `<div class="su-sub">Downloading</div><div class="su-prog" id="su-prog"></div><p class="hint">You can keep using WardogClips while this downloads. The bar at the bottom of the window shows how far it is.</p>` : ""}`;
  }
  function renderProgress() {
    const box = $("su-prog"); if (!box) return; wireRows(box);
    paintRows(box, S.started.map(id => PK.get(id) || { id, state: "downloading", progress: 0 }), "wiz");
  }
  // the wizard's progress row: a plain name, the bar, "Ready" -- never the raw error
  function wizRow(p) {
    const act = isActive(p), ok = isInstalled(p) && !act;
    const failed = p.state === "failed", stopped = p.state === "cancelled";
    let status = "";
    if (ok || (p.state === "done" && !failed)) status = `<div class="pk-note ok">Ready</div>`;
    else if (failed) status = `<div class="pk-note warn-t">The download stopped. Check your internet connection, then press Retry.</div>`;
    else if (stopped) status = `<div class="pk-note">Stopped. Press Retry to go on where it stopped.</div>`;
    const prog = act ? `<div class="pk-prog"><div class="pg"><i style="width:${pct(p.progress)}"></i></div><span class="num" data-pt>${esc(progText(p))}</span></div>` : "";
    const btn = act ? `<button type="button" class="btn sm" data-pk="cancel" data-id="${esc(p.id)}">${icon("x")}Cancel</button>`
      : failed || stopped ? `<button type="button" class="btn sm" data-pk="install" data-id="${esc(p.id)}">${icon("refresh")}Retry</button>` : "";
    return `<div class="pk-row${act ? " act" : ""}" id="${domId(p.id)}-w" data-sig="${esc(rowSig(p, "wiz"))}">
      <div class="pk-main"><div class="pk-t"><b>${esc(wizName(p.id))}</b>${sizeOf(p.id) ? `<span class="faint">about ${esc(mb(sizeOf(p.id)))}</span>` : ""}</div>${prog}${status}</div>
      <div class="pk-acts">${btn}</div></div>`;
  }

  // ---- validation per step (Next): "" = fine ----
  function validate(step) {
    if (step === 1) {
      const t = $("su-path"); if (t && t.value.trim()) addRoot(t.value, "typed");
      const on = S.roots.filter(r => r.on);
      if (!on.length && !S.skipRoots) return "Add at least one folder, or tick “Skip for now”.";
      const bad = on.filter(r => { const c = S.checks[r.k]; return c && !c.pending && !c.error && (!c.exists || !c.is_dir); });
      if (bad.length) return `Folder not found: ${bad.map(r => r.path).join(", ")}. Untick or remove it.`;
    }
    if (step === 2) {
      commitChips();
      if (!S.aliases.length && !S.nameWarned) { S.nameWarned = true; return "No name yet: without it we can't tell your kills from your deaths. Press Next again to go on anyway."; }
    }
    if (step === 4) {
      readVodInput();
      const vc = S.vodCheck;
      if (S.vods && vc && !vc.pending && !vc.error && vc.exists && !vc.is_dir) return "That's a file, not a folder: pick another folder for stream recordings.";
    }
    return "";
  }
  function readVodInput() { const v = $("su-vods"); if (v) S.vodsDir = v.value.trim(); }

  // ---- render + wire ----
  function render() {
    const body = $("su-body"); if (!body) return;
    $("su-steps").innerHTML = STEPS.map((n, i) => `<li class="${i === S.step ? "on" : i < S.step || (S.saved && i <= S.maxStep) ? "done" : ""}">
      <button type="button" data-st="${i}" ${i <= S.maxStep && !S.saved ? "" : "disabled"} ${i === S.step ? 'aria-current="step"' : ""}><span class="n">${i < S.step || S.saved ? icon("check") : i + 1}</span><span>${esc(n)}</span></button></li>`).join("");
    body.innerHTML = [stepWelcome, stepFolders, stepYou, stepSpeech, stepVods, stepFinish][S.step]();
    const last = S.step === STEPS.length - 1;
    $("su-back").hidden = S.step === 0 || S.saved;
    $("su-next").innerHTML = S.saved ? `Open WardogClips` : last ? `${icon("check")}Start` : S.step === 0 ? `Let's go${icon("chev")}` : `Next${icon("chev")}`;
    $("su-next").disabled = S.saving;
    $("su-skip").hidden = S.saved;
    $("su-skip").textContent = S.first ? "Skip setup" : "Cancel";
    $("su-msg").textContent = S.err; $("su-msg").classList.toggle("bad", !!S.err);
    wireStep();
    if (S.saved) renderProgress();
  }
  function focusStep() { const h = $("su-body").querySelector("h2"); if (h) h.focus({ preventScroll: true }); $("su-body").scrollTop = 0; }
  function go(n) {
    if (n === S.step) return;
    if (n > S.step) { for (let i = S.step; i < n; i++) { const e = validate(i); if (e) { S.err = e; if (i !== S.step) { S.step = i; } render(); return; } } }
    else if (S.step === 4) readVodInput();                  // keep what was typed when going back
    else if (S.step === 2) commitChips();
    S.err = ""; S.step = n; S.maxStep = Math.max(S.maxStep, n); render(); focusStep();
  }
  async function next() {
    if (S.saved) { close(); return; }
    if (S.step < STEPS.length - 1) { go(S.step + 1); return; }
    await finish();
  }
  async function finish() {
    if (S.saving) return;
    S.saving = true; S.err = ""; render();
    const install = installList();
    const ch = curChoice();
    const body = { roots: S.roots.filter(r => r.on).map(r => r.path), aliases: S.aliases, squad: S.squad,
      stt_enabled: S.sttEnabled, install, done: true };
    if (S.sttEnabled && ch && ch.model) body.stt_model = ch.model;
    if (S.vods) body.vods_dir = S.vodsDir;                 // "Not now" leaves the VOD settings as they are
    try {
      let r;
      try { r = await req("POST", "/api/setup", body); }
      catch (e) {
        if (e.status !== 404 && e.status !== 405) throw e;
        // no setup route on this server: save the same choices through the plain settings API (no installs)
        const patch = { library: { roots: body.roots }, player: { aliases: body.aliases, squad: body.squad }, stt: { enabled: body.stt_enabled }, setup: { done: true } };
        if (body.stt_model) patch.stt.model = body.stt_model;
        if ("vods_dir" in body) patch.vods = { dir: body.vods_dir };
        r = await req("POST", "/api/settings", patch);
        r.started = []; if (install.length) toast("Saved. The downloads can be started from Settings > Add-ons.", 6000);
      }
      afterSaved(r);
      S.started = Array.isArray(r.started) ? r.started.map(x => typeof x === "string" ? x : x && x.id).filter(Boolean) : install;
      for (const id of S.started) if (!PK.get(id) || !isActive(PK.get(id))) merge(Object.assign({ id }, PK.get(id) || {}, { state: "downloading", progress: 0 }));
      S.saved = true; S.maxStep = STEPS.length - 1;
      refreshPacks();
    } catch (e) { S.err = e.status ? "Your choices could not be saved. Press Start to try again." : "WardogClips is not answering. Is it still running? Press Start to try again."; }
    finally { S.saving = false; render(); if (S.saved) $("su-next").focus(); }
  }
  function afterSaved(r) {
    if (hasST()) {
      if (r && r.settings) ST.settings = Object.assign({}, ST.settings || {}, r.settings);
      if (ST.settings) { ST.settings.setup = Object.assign({}, ST.settings.setup || {}, { done: true }); }
    }
    if (typeof refreshStatus === "function") refreshStatus().then(() => { if (typeof loadRecs === "function") loadRecs(); });
  }
  async function skip() {
    if (!S.first) { close(); return; }
    // a first run's Skip still marks setup as done, so the wizard does not come back on every start
    try { const r = await req("POST", "/api/setup", { done: true }).catch(async (e) => { if (e.status !== 404 && e.status !== 405) throw e; return req("POST", "/api/settings", { setup: { done: true } }); }); afterSaved(r); }
    catch (e) { toast("Could not save that. Try again in a moment.", 5000); return; }
    close(); toast("Setup skipped. Settings > Add-ons has “Run setup again”.", 5000);
  }

  // ---- folders ----
  function addRoot(p, src) {
    const path = norm(p), k = pkey(p); if (!k) return;
    const have = S.roots.find(r => r.k === k);
    if (have) { have.on = true; } else { S.roots.push({ path, k, src, on: true }); }
    S.skipRoots = false; checkRoot(k, path);
  }
  // check-folder, retried quietly once (a busy server, a sleeping HDD): the page never shows the raw error
  async function checkFolder(path) {
    for (let i = 0; i < 2; i++) {
      try { return await api("/api/system/check-folder?path=" + encodeURIComponent(path)); }
      catch (e) { if (i === 0) await new Promise(r => setTimeout(r, 1500)); }
    }
    return { error: true };
  }
  async function checkRoot(k, path) {
    if (S.checks[k] && !S.checks[k].error) { if (S.step === 1) paintRoots(); return; }
    S.checks[k] = { pending: true }; if (S.step === 1) paintRoots();
    S.checks[k] = await checkFolder(path);
    if (S.open && S.step === 1) paintRoots();
  }
  function paintRoots() {
    const box = $("su-roots"); if (!box) return;
    const f = document.activeElement; const fi = f && box.contains(f) ? (f.dataset.ri || f.dataset.rx) : null; const fk = f && f.dataset.ri != null ? "ri" : "rx";
    box.innerHTML = S.roots.length ? S.roots.map(rootRow).join("") : noRoots();
    if (fi != null) { const el = box.querySelector(`[data-${fk}="${fi}"]`); if (el) el.focus(); }
  }
  // the native folder window; {path} or {failed: true} when it could not open (typed paths still work)
  async function pick(initial, title) {
    if (S.picking) return {};
    S.picking = true; render();
    try { const r = await req("POST", "/api/system/pick-folder", { initial: initial || "", title: title || "Choose a folder" }); return { path: r && r.path ? r.path : null }; }
    catch (e) {
      if (e.status === 409) toast("A folder window is already open: it may be behind this one.", 5000);
      else if (e.status !== 403) toast("The folder window did not open. You can type the path instead.", 5000);
      return { failed: true };
    }
    finally { S.picking = false; }
  }

  // ---- chips ----
  function splitNames(s) { return String(s || "").split(/[;,\n]+/).map(x => x.trim()).filter(Boolean); }
  function addNames(list, s) { for (const n of splitNames(s)) if (!list.some(x => x.toLowerCase() === n.toLowerCase())) list.push(n); }
  function commitChips() {
    for (const [id, list] of [["su-aliases", S.aliases], ["su-squad", S.squad]]) { const box = $(id); const inp = box && box.querySelector("input"); if (inp && inp.value.trim()) { addNames(list, inp.value); inp.value = ""; } }
  }
  function chipPh(id) { return id === "su-aliases" ? "type your name and press Enter" : "type a name and press Enter"; }
  function wireChips(id, list) {
    const box = $(id); if (!box) return;
    const inp = box.querySelector("input");
    const repaint = () => { const v = inp.value; box.outerHTML = chipBox(id, list, chipPh(id)); wireChips(id, list); const ni = $(id).querySelector("input"); ni.value = v; ni.focus(); if (id === "su-aliases") S.nameWarned = false; };
    box.onclick = (e) => { const x = e.target.closest("[data-cx]"); if (x) { list.splice(+x.dataset.cx, 1); repaint(); return; } inp.focus(); };
    inp.onkeydown = (e) => {
      if ((e.key === "Enter" || e.key === ",") && inp.value.trim()) { e.preventDefault(); addNames(list, inp.value); inp.value = ""; repaint(); }
      else if (e.key === "Enter") { e.preventDefault(); }
      else if (e.key === "Backspace" && !inp.value && list.length) { e.preventDefault(); list.pop(); repaint(); }
    };
    inp.onpaste = (e) => {
      const t = (e.clipboardData || window.clipboardData).getData("text");
      if (!/[;,\n]/.test(t)) return;
      e.preventDefault(); addNames(list, inp.value + t); inp.value = ""; repaint();
    };
    // leaving the field keeps what was typed (as a chip); the box is redrawn without stealing the focus back
    inp.onblur = () => { if (inp.value.trim()) { addNames(list, inp.value); inp.value = ""; box.outerHTML = chipBox(id, list, chipPh(id)); wireChips(id, list); } };
  }

  async function checkVods() {
    const path = vodDirShown();
    if (!path) { S.vodCheck = null; paintVodLine(); return; }
    S.vodCheck = { pending: true }; paintVodLine();
    S.vodCheck = await checkFolder(path);
    paintVodLine();
  }
  function wireStep() {
    $("su-steps").querySelectorAll("[data-st]").forEach(b => b.onclick = () => go(+b.dataset.st));
    if (S.step === 1) {
      S.roots.forEach(r => { if (!S.checks[r.k]) checkRoot(r.k, r.path); });
      const box = $("su-roots");
      box.onchange = (e) => { const i = e.target.dataset.ri; if (i == null) return; S.roots[+i].on = e.target.checked; e.target.closest(".su-root").classList.toggle("on", e.target.checked); if (e.target.checked) S.skipRoots = false; S.err = ""; $("su-msg").textContent = ""; };
      box.onclick = (e) => { const x = e.target.closest("[data-rx]"); if (!x) return; S.roots.splice(+x.dataset.rx, 1); paintRoots(); };
      const t = $("su-path");
      const addTyped = () => { if (!t.value.trim()) { t.focus(); return; } addRoot(t.value, "typed"); t.value = ""; S.err = ""; $("su-msg").textContent = ""; $("su-skiproots").checked = false; t.focus(); };
      t.onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); addTyped(); } };
      $("su-path-add").onclick = addTyped;
      $("su-pick").onclick = async () => {
        const first = S.roots.find(r => r.on) || S.roots[0];
        const r = await pick(first ? first.path : "", "Choose the folder OBS saves your recordings in");
        if (S.step !== 1) return;
        render();
        if (r.path) { addRoot(r.path, "picked"); S.err = ""; $("su-msg").textContent = ""; $("su-skiproots").checked = false; }
        const b = r.failed ? $("su-path") : $("su-pick"); if (b) b.focus();
      };
      $("su-skiproots").onchange = (e) => { S.skipRoots = e.target.checked; S.err = ""; $("su-msg").textContent = ""; };
    }
    if (S.step === 2) { wireChips("su-aliases", S.aliases); wireChips("su-squad", S.squad); }
    if (S.step === 3) {
      $("su-stt").onchange = (e) => { S.sttEnabled = e.target.checked; render(); $("su-stt").focus(); };
      $("su-body").querySelectorAll("input[name=su-speech]").forEach(r => r.onchange = () => { S.speech = r.value; });
    }
    if (S.step === 4) {
      $("su-body").querySelectorAll("input[name=su-vods]").forEach(r => r.onchange = () => {
        readVodInput(); S.vods = r.value === "yes"; S.err = ""; render();
        const n = $("su-body").querySelector(`input[name=su-vods][value="${r.value}"]`); if (n) n.focus();
        if (S.vods && !S.vodCheck) checkVods();
      });
      const v = $("su-vods");
      if (v) { v.onchange = () => { readVodInput(); S.vodCheck = null; paintSaveTo(); checkVods(); }; v.onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); v.onchange(); } }; }
      const pb = $("su-vods-pick");
      if (pb) pb.onclick = async () => {
        readVodInput();
        const r = await pick(vodDirShown(), "Choose where stream recordings are saved");
        if (S.step !== 4) return;
        if (r.path) { S.vodsDir = norm(r.path); S.vodCheck = null; }
        if (r.failed) S.vodsEdit = true;           // no folder window (another PC / it failed): a path field instead
        render();
        if (r.path) checkVods();
        const f = r.failed ? $("su-vods") : $("su-vods-pick"); if (f) f.focus();
      };
      if (S.vods && !S.vodCheck) checkVods();
    }
  }
  function paintSaveTo() { const p = $("su-body").querySelector(".su-saveto .path"); if (p) { p.textContent = vodDirShown() || "your Videos folder"; p.title = vodDirShown(); } }
  function paintVodLine() { if (S.step !== 4 || !$("su-vods-line")) return; $("su-vods-line").innerHTML = vodInfo(); }

  // ---- dialog plumbing (once) ----
  function wireDialog() {
    const d = $("dlg-setup"); if (!d || d.dataset.wired) return; d.dataset.wired = "1";
    $("su-next").onclick = next;
    $("su-back").onclick = () => go(Math.max(0, S.step - 1));
    $("su-skip").onclick = skip;
    // Escape: a first run is not dismissed by accident (keydown preventDefault stops the cancel in Chromium;
    // "cancel" covers the rest); a re-run closes like any dialog
    d.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && S.first && !S.saved) { e.preventDefault(); e.stopPropagation(); toast("finish the steps, or use “Skip setup” at the bottom", 3000); }
      // Alt+arrows step through the wizard from anywhere inside it
      else if (e.altKey && (e.key === "ArrowRight" || e.key === "ArrowLeft") && !S.saved) { e.preventDefault(); if (e.key === "ArrowRight") next(); else go(Math.max(0, S.step - 1)); }
    });
    d.addEventListener("cancel", (e) => { if (S.first && !S.saved) e.preventDefault(); });
    // last line of defence: a first-run wizard closed by anything but Finish / Skip (a browser that ignores the
    // cancel guard) opens again on the step it was on
    d.addEventListener("close", () => {
      if (S.open && S.first && !S.saved && !S.allowClose) { setTimeout(() => { if (!d.open) { d.showModal(); render(); } }, 0); return; }
      S.open = false;
    });
  }

  async function boot() {
    wireDialog();
    const sb = $("sb-dl"); if (sb) sb.onclick = () => { if (typeof openSettings === "function") openSettings("Add-ons"); };
    let done = true;
    const st = hasST() && ST.status, s = hasST() && ST.settings;
    if (st && typeof st.setup_done === "boolean") done = st.setup_done;
    else if (s && s.setup && typeof s.setup.done === "boolean") done = s.setup.done;
    refreshPacks();
    checkUpdates(false);
    setInterval(() => { if (!document.hidden) checkUpdates(false); }, 6 * 3600 * 1000);
    if (!done) await open({ first: true });
    return done;
  }

  return { boot, open, close, onPackEvent, refreshPacks, mountSettings, checkUpdates, install, cancel, remove,
    onSetupEvent(d) { if (hasST() && ST.settings && d && typeof d.done === "boolean") ST.settings.setup = Object.assign({}, ST.settings.setup || {}, { done: d.done }); },
    get state() { return S; }, get packs() { return PK; } };
})();
// app.js tests window.SU (optional module): expose the const there too
window.SU = SU;
