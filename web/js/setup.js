/* First-run setup wizard + add-on packs.
   - dialog#dlg-setup: six steps (Welcome, Footage, Your name, Speech, Streams, Start) against GET / POST /api/setup.
     Plain words only on its screens (no tool names); the app picks the technical add-ons from the answers.
     Opened on boot while the server says setup is not done, from the palette and Settings > Add-ons ("Run setup
     again"). On a first run Escape does not close it: only "Skip setup" (which still posts done) or Finish do.
   - Add-on packs (CUDA speech libraries, deno, a newer yt-dlp, Whisper models): one shared state PK fed by
     GET /api/packs, the SSE "packs" events (app.js forwards them to SU.onPackEvent) and, while anything downloads, a
     2 s poll of /api/packs as a fallback (an SSE reconnect can drop events). Three views render it: the wizard's
     progress list, Settings > Add-ons (#ao-list) and the status bar item #sb-dl.
   - Release check: GET /api/updates -> updates.js (UPD: the prompt, the status bar link #sb-update, Settings >
     Updates "Check now"); without updates.js the link + Check now work as before.
   - Text: the "setup" catalog (t / te / tHtml at render time); a language switch redraws what is on screen.
   Loaded after core.js and BEFORE app.js: it only defines things at load time; app.js globals (ST, openSettings,
   closeDrawer, refreshStatus, loadRecs) are looked up when a function runs. */
"use strict";

// ST is a top-level const in app.js: a global binding but NOT a window property, so test it with typeof
const hasST = () => typeof ST !== "undefined" && !!ST;
const SU = (() => {
  // the wizard's steps, in order (their names: setup.step.<key>)
  const STEPS = ["welcome", "footage", "name", "speech", "streams", "start"];
  // display sizes when the server sends none (known without network)
  const MODEL_MB = { "tiny.en": 75, "tiny": 75, "small.en": 480, "small": 480, "medium.en": 1500, "medium": 1500, "large-v3": 3100, "large-v3-turbo": 1600 };
  // an English-only model is swapped for its many-languages twin when the speech language is not English
  const MULTI_OF = { "tiny.en": "tiny", "small.en": "small", "medium.en": "medium" };
  const effModel = (m) => (S.sttLang && S.sttLang !== "en" && MULTI_OF[m]) || m;
  const PK = new Map();                      // pack / model id -> its latest status
  let gpu = null;                            // packs.gpu_info() as last seen
  let curModel = "";                         // the speech model in use (packs.status current_model)
  // err: "" or {k: catalog key, p: params} (shown in the footer; re-translated on a language switch)
  const S = { data: null, open: false, first: false, step: 0, maxStep: 0, roots: [], checks: {}, aliases: [], squad: [],
    sttEnabled: true, speech: "", vods: false, vodsEdit: false, vodsDir: "", saved: false, started: [], saving: false,
    picking: false, nameWarned: false, skipRoots: false, offline: false, err: "", vodCheck: null, loading: false };

  /* ---------------- small helpers ---------------- */
  // a typed / pasted path: quotes and trailing slashes off, a bare drive keeps its backslash ("D:\")
  const norm = (p) => { const s = String(p || "").trim().replace(/^"(.*)"$/, "$1").trim(); return /^[A-Za-z]:[\\/]*$/.test(s) ? s.slice(0, 2) + "\\" : s.replace(/[\\/]+$/, ""); };
  const pkey = (p) => norm(p).toLowerCase().replace(/\//g, "\\");
  // a size given in MB, in the UI language's units
  const mb = (m) => m == null || isNaN(m) ? "" : m >= 1000 ? fmtUnit(m / 1000, "gigabyte", 1) : fmtUnit(Math.round(m), "megabyte");
  // a CSS width (never the locale's percent format: "42 %" / "42,5 %" is not valid CSS)
  const cssPct = (x) => (Math.max(0, Math.min(1, Number(x) || 0)) * 100).toFixed(1) + "%";
  const isActive = (p) => !!p && (p.state === "downloading" || p.state === "extracting");
  const isModel = (id) => String(id || "").startsWith("model:");
  // a server text field of a pack ("" when empty): translated when the server sent <field>_i18n
  const sf = (o, f) => (o && o[f] ? srvText(o, f) : "");
  const titleOf = (p) => sf(p, "title") || (p.name ? t("setup.pack.model_title", { name: p.name }) : (p.id ? String(p.id) : t("setup.pack.addon")));
  // "done" only says the job finished: an up-to-date yt-dlp ends done with nothing installed (note says why)
  const isInstalled = (p) => !!p && !!(p.installed || p.cached);
  const urlId = (id) => String(id).split(":").map(encodeURIComponent).join(":");
  const domId = (id) => "pk-" + String(id).replace(/[^A-Za-z0-9_-]/g, "_");
  const firstRun = () => !!(hasST() && ST.settings && ST.settings.setup && ST.settings.setup.done === false);
  async function req(method, url, body) {
    const r = await fetch(url, { method, headers: _hdrs(!!body), body: body ? JSON.stringify(body) : undefined });
    let j = null; try { j = await r.json(); } catch (e) { }
    if (!r.ok) { const e = new Error((j && j.detail && srvText(j, "detail")) || r.statusText || ("HTTP " + r.status)); e.status = r.status; throw e; }
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
    // a new English text without its key must not keep the previous text's translation
    for (const f of ["title", "desc", "reason", "note", "error"]) if (f in p && !((f + "_i18n") in p)) delete cur[f + "_i18n"];
    if (!prev || !isActive(prev) || isActive(cur)) return;
    const title = titleOf(cur);
    if (cur.state === "done") {
      if (cur.id === "cuda") toast(t("setup.toast.cuda_ready"), 6000);
      else toast(isInstalled(cur) ? t("setup.toast.pack_ready", { title }) : cur.note ? t("setup.toast.pack_note", { title, note: sf(cur, "note") }) : t("setup.toast.pack_nothing", { title }), 5000);
    } else if (cur.state === "failed") toast(t("setup.toast.pack_failed", { title }), 8000);
    else if (cur.state === "cancelled" && was) toast(t("setup.toast.pack_stopped", { title }), 4000);
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
    catch (e) { toast(t("setup.toast.install_failed", { title: titleOf(PK.get(id) || { id }), error: e.message }), 6000); }
    renderAll(); ensurePoll();
  }
  async function cancel(id) {
    try { await req("POST", `/api/packs/${urlId(id)}/cancel`); } catch (e) { toast(e.message, 5000); }
    refreshPacks();
  }
  async function remove(id) {
    const p = PK.get(id) || { id };
    const title = titleOf(p);
    if (!confirm(t(isModel(id) ? "setup.confirm.remove_model" : "setup.confirm.remove", { title }))) return;
    try {
      const r = await req("POST", `/api/packs/${urlId(id)}/remove`);
      toast(r && r.ok === false ? t("setup.toast.remove_failed", { title }) : t("setup.toast.removed", { title }), r && r.ok === false ? 5000 : 3000);
    } catch (e) { toast(e.message || t("setup.toast.remove_failed", { title }), 6000); }
    refreshPacks();
  }

  function progText(p) {
    const q = { pct: fmtPct(p.progress || 0), done: fmtBytes(p.bytes || 0), total: p.total ? fmtBytes(p.total) : "" };
    const inst = p.state === "extracting";
    if (p.total) return t(inst ? "setup.progress.installing_of" : "setup.progress.of", q);
    if (p.bytes) return t(inst ? "setup.progress.installing_bytes" : "setup.progress.bytes", q);
    return inst ? t("setup.progress.installing", q) : q.pct;
  }
  // one row (Settings > Add-ons and the wizard's progress list); sig decides between an in-place bar update and a
  // re-render, so a focused button survives the 4-a-second progress events (the language is part of it: a switch
  // redraws the rows)
  function rowSig(p, mode) { return [p.state, isInstalled(p), p.available !== false, !!p.error, mode, p.name && p.name === curModel, I18N.lang].join("|"); }
  function packRow(p, mode) {
    if (mode === "wiz") return wizRow(p);
    const act = isActive(p), inst = isInstalled(p) && !act, avail = p.available !== false;
    // already covered on this PC some other way (Node.js for YouTube, the CUDA libraries in the environment)
    const notNeeded = !inst && avail && ((p.id === "cuda" && p.cuda_ready === true) || (p.id === "deno" && p.needed === false));
    const badges = [];
    if (inst) badges.push(`<span class="badge ok">${icon("check")}${p.version && !isModel(p.id) ? te("setup.badge.installed_version", { version: p.version }) : te("setup.badge.installed")}</span>`);
    if (isModel(p.id) && p.name && p.name === curModel) badges.push(`<span class="badge acc">${te("setup.badge.in_use")}</span>`);
    if (p.recommended && !inst && !notNeeded) badges.push(`<span class="badge acc">${te("setup.badge.recommended")}</span>`);
    if (notNeeded) badges.push(`<span class="badge">${te("setup.badge.not_needed")}</span>`);
    if (p.state === "failed") badges.push(`<span class="badge bad">${te("setup.badge.failed")}</span>`);
    if (p.state === "cancelled") badges.push(`<span class="badge warn">${te("setup.badge.canceled")}</span>`);
    if (!avail) badges.push(`<span class="badge">${te("setup.badge.not_needed")}</span>`);
    const sz = p.size_mb ? mb(p.size_mb) : (p.name && MODEL_MB[p.name] ? mb(MODEL_MB[p.name]) : "");
    const desc = [sf(p, "desc"), sz ? t("setup.pack.about_size", { size: sz }) : ""].filter(Boolean).join(" · ");
    let btns = "";
    if (act) btns = `<button type="button" class="btn sm" data-pk="cancel" data-id="${esc(p.id)}">${icon("x")}${te("setup.btn.cancel")}</button>`;
    else if (mode !== "wiz") {
      if (inst) btns = `<button type="button" class="btn sm ghost danger" data-pk="remove" data-id="${esc(p.id)}">${icon("trash")}${te("setup.btn.remove")}</button>`;
      else if (avail) btns = `<button type="button" class="btn sm${notNeeded ? " ghost" : p.recommended ? " pri" : ""}" data-pk="install" data-id="${esc(p.id)}">${icon("download")}${te(p.state === "failed" || p.state === "cancelled" ? "setup.btn.retry" : notNeeded ? "setup.btn.install_anyway" : p.id === "ytdlp" ? "setup.btn.update" : "setup.btn.install")}</button>`;
    } else if (p.state === "failed" || p.state === "cancelled") btns = `<button type="button" class="btn sm" data-pk="install" data-id="${esc(p.id)}">${icon("refresh")}${te("setup.btn.retry")}</button>`;
    // reason / note are informational even for an available pack ("Node.js is installed -- ..."); not once installed
    const why = !inst ? (sf(p, "reason") || sf(p, "note")) : sf(p, "note");
    const reason = why ? `<div class="pk-note">${esc(why)}</div>` : "";
    const err = p.state === "failed" && p.error ? `<div class="pk-err">${esc(sf(p, "error"))}</div>` : "";
    const done = mode === "wiz" && inst ? `<div class="pk-note ok">${te(p.id === "cuda" ? "setup.pack.ready_cuda" : "setup.pack.ready")}</div>`
      : mode === "wiz" && p.state === "done" && !p.note ? `<div class="pk-note ok">${te("setup.pack.finished")}</div>` : "";
    const prog = act ? `<div class="pk-prog"><div class="pg"><i style="width:${cssPct(p.progress)}"></i></div><span class="num" data-pt>${esc(progText(p))}</span></div>` : "";
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
        const bar = row.querySelector(".pg > i"); if (bar) bar.style.width = cssPct(w.p.progress);
        const pt = row.querySelector("[data-pt]"); if (pt) pt.textContent = progText(w.p);
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
    const inst = act.every(p => p.state === "extracting"), q = { pct: fmtPct(pr) };
    $("sb-dl-t").textContent = act.length === 1
      ? t(inst ? "setup.sb.installing" : "setup.sb.downloading", Object.assign(q, { name: titleOf(act[0]) }))
      : t(inst ? "setup.sb.installing_n" : "setup.sb.downloading_n", Object.assign(q, { count: act.length }));
    el.querySelector(".pg > i").style.width = cssPct(pr);
    el.dataset.tip = act.map(p => t("setup.sb.tip_line", { title: titleOf(p), progress: progText(p) })).join("\n") + "\n" + t("setup.sb.tip_click");
    el.hidden = false;
  }
  let upd = null;
  async function checkUpdates(force) {
    try { upd = await api("/api/updates" + (force ? "?force=1" : "")); }
    catch (e) { if (force) throw e; return null; }
    if (window.UPD) { UPD.onState(upd); return upd; }       // updates.js owns the link + the prompt
    paintUpdLink();
    return upd;
  }
  // the status bar's "vX available" link when updates.js is not loaded
  function paintUpdLink() {
    const a = $("sb-update");
    if (!a || window.UPD) return;
    const show = !!(upd && upd.newer && upd.latest);
    a.hidden = !show;
    if (show) { a.textContent = t("setup.upd.available", { version: String(upd.latest).replace(/^v/i, "") }); a.href = upd.url || "#"; a.title = t("setup.upd.link_title"); }
  }

  /* ---------------- Settings > Add-ons / Updates (app.js openDrawer renders the mounts) ---------------- */
  function renderAddons() {
    const box = $("ao-list"); if (!box) return;
    wireRows(box); const mbox = $("ao-models"); if (mbox) wireRows(mbox);
    const all = [...PK.values()];
    const packs = all.filter(p => !isModel(p.id)), models = all.filter(p => isModel(p.id));
    if (!all.length) { box.innerHTML = `<div class="hint">${te("setup.addons.load_failed")}</div>`; if (mbox) mbox.innerHTML = ""; }
    else { paintRows(box, packs, "set"); if (mbox) paintRows(mbox, models, "set"); }
    const g = $("ao-gpu");
    if (!g) return;
    if (!gpu) { g.innerHTML = ""; return; }
    if (!gpu.nvidia) { g.innerHTML = `<span class="sd warn"></span><span>${te("setup.gpu.none")}</span>`; return; }
    const card = [gpu.name || t("setup.gpu.nvidia_card"), gpu.vram_mb ? fmtUnit(Math.round(gpu.vram_mb / 1024), "gigabyte") : "",
      gpu.driver ? t("setup.gpu.driver", { version: gpu.driver }) : ""].filter(Boolean).join(" · ");
    g.innerHTML = `<span class="sd ok"></span><span>${te(cudaReady() ? "setup.gpu.ready" : "setup.gpu.not_ready", { card })}</span>`;
  }
  // an ISO time from the server ("2026-11-07T19:48:05") in the UI language; the raw text when it does not parse
  function whenText(iso) {
    const d = new Date(String(iso));
    return isNaN(d.getTime()) ? String(iso).replace("T", " ").slice(0, 16) : fmtDateTime(d, "datetime");
  }
  // called by openDrawer once its markup is in place
  function mountSettings() {
    renderAddons(); refreshPacks();
    const y = $("ao-ytdlp"); if (y) y.onclick = () => install("ytdlp");
    const rs = $("ao-setup"); if (rs) rs.onclick = () => { if (typeof closeDrawer === "function") closeDrawer(); open({}); };
    const cn = $("up-check");
    if (cn && window.UPD) UPD.mountSettings();
    else if (cn) cn.onclick = async () => {
      const line = $("up-res"); cn.disabled = true; line.textContent = t("setup.upd.checking");
      try {
        const u = await checkUpdates(true);
        if (!u || u.disabled) line.innerHTML = `<span class="muted">${te("setup.upd.no_repo")}</span>`;
        else if (u.newer) line.innerHTML = `<span class="sd acc"></span><span>${te("setup.upd.newer", { latest: String(u.latest).replace(/^v/i, ""), current: u.current || "?" })} · <a href="${esc(u.url || "#")}" target="_blank" rel="noopener">${te("setup.upd.open_page")}</a></span>`;
        else line.innerHTML = `<span class="sd ok"></span><span>${te("setup.upd.newest", { version: u.current || (ST.status && ST.status.version) || "?" })}${u.checked ? ` · ${te("setup.upd.checked", { when: whenText(u.checked) })}` : ""}</span>`;
      } catch (e) { line.innerHTML = `<span class="sd bad"></span><span>${te("setup.upd.failed", { error: e.message })}</span>`; }
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
  // the best non-NVIDIA card (AMD / Intel) the backend listed, for the wording on PCs without an NVIDIA card
  const otherCard = () => {
    const l = (det().gpus || []).filter(c => c.vendor !== "nvidia");
    return l.find(c => !c.integrated) || l[0] || null;
  };
  const modelOf = (name) => PK.get("model:" + name) || { id: "model:" + name, name, size_mb: MODEL_MB[name], cached: false };
  const sizeOf = (id) => { const p = PK.get(id); return (p && p.size_mb) || (isModel(id) ? MODEL_MB[id.slice(6)] || 0 : 0); };
  const vodDirShown = () => S.vodsDir || cur().vods_dir_effective || "";

  // the speech cards this PC gets: Fast (NVIDIA only) + Standard, or Standard + Light without an NVIDIA card.
  // name / text are read at render time (the UI language can change while the wizard is open)
  function speechChoices() {
    const nv = !!gpuNow().nvidia, ready = cudaReady(), c = PK.get("cuda");
    const rec = (S.data && S.data.recommended && S.data.recommended.stt_model) || "";
    const out = [];
    if (nv) out.push({ key: "fast", name: t("setup.choice.fast"), model: rec || "medium.en", cuda: !ready && !!c && c.available !== false && !isInstalled(c),
      text: t("setup.choice.fast_text") });
    out.push({ key: "standard", name: t("setup.choice.standard"), model: "small.en", cuda: false,
      text: t(nv ? (ready ? "setup.choice.standard_lighter" : "setup.choice.standard_cpu") : "setup.choice.standard_good") });
    if (!nv) out.push({ key: "light", name: t("setup.choice.light"), model: "tiny.en", cuda: false, text: t("setup.choice.light_text") });
    // a re-run whose model was picked in Settings > Add-ons and is none of the above: offer to keep it
    const cm = cur().stt_model;
    if (!S.first && cm && !out.some(o => o.model === cm)) out.push({ key: "keep", name: t("setup.choice.keep"), model: cm, cuda: false, text: t("setup.choice.keep_text") });
    return out;
  }
  // the downloads one card needs (what is already on this PC is left out)
  function choiceIds(ch) {
    const ids = [];
    if (ch.cuda) ids.push("cuda");
    const em = effModel(ch.model), m = modelOf(em); if (!isInstalled(m)) ids.push("model:" + em);
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
    // OBS's folders are pre-checked on a first run; a re-run lists them unchecked unless they are roots already
    ((dt.obs || {}).rec_paths || []).forEach(p => add(p, "obs", S.first || !(c.roots || []).length));
    S.aliases = (c.aliases || []).slice(); S.squad = (c.squad || []).slice();
    S.sttEnabled = c.stt_enabled !== false;
    S.sttLang = (S.first ? (d.recommended || {}).stt_language : c.stt_language) || c.stt_language || "en";
    // a first run takes the first card (Fast on an NVIDIA PC); a re-run the card of the model in use
    const ch = speechChoices();
    S.speech = (S.first ? ch[0] : (ch.find(x => x.model === c.stt_model) || ch[0])).key;
    S.vodsDir = c.vods_dir || "";
    const dn = PK.get("deno");
    S.vods = !S.first && (!!c.vods_dir || (!!dn && isInstalled(dn)));
    S.vodsEdit = false;
    S.step = 0; S.maxStep = 0; S.saved = false; S.started = []; S.err = ""; S.nameWarned = false; S.skipRoots = false; S.vodCheck = null;
  }

  const loadingHtml = () => `<div class="su-loading"><div class="spin"></div>${te("setup.loading")}</div>`;
  async function open(opts) {
    const d = $("dlg-setup"); if (!d) return;
    if (d.open) { d.focus(); return; }
    S.first = !!(opts && opts.first) || firstRun();
    d.classList.toggle("first", S.first);
    d.returnValue = "";
    $("su-body").innerHTML = loadingHtml();
    $("su-steps").innerHTML = ""; $("su-msg").textContent = "";
    S.open = true; S.loading = true; d.showModal();
    try { await load(); } finally { S.loading = false; }
    initFromData();
    render();
    $("su-next").focus();
  }
  function close() {
    const d = $("dlg-setup"); S.open = false; S.allowClose = true;
    if (d && d.open) d.close("done");
    S.allowClose = false;
  }

  // ---- steps ----
  const hwl = (cls, html) => `<div class="sline su-hwl"><span class="sd ${cls}"></span><span>${html}</span></div>`;
  function hwLines() {
    const dt = det(), g = gpuNow(), ff = dt.ffmpeg || {}, obs = dt.obs || {}, rv = dt.resolve || {};
    if (S.offline && !Object.keys(dt).length) return "";
    const out = [];
    const other = otherCard();
    out.push(g.nvidia ? hwl("ok", tHtml("setup.hw.nvidia_html", { name: g.name || "NVIDIA" }))
      : other ? hwl("", tHtml("setup.hw.other_html", { name: other.name }))
      : hwl("", te("setup.hw.no_nvidia")));
    if (obs.found) out.push(hwl("ok", te((obs.rec_paths || []).length ? "setup.hw.obs_found_paths" : "setup.hw.obs_found")));
    else out.push(hwl("", te("setup.hw.obs_missing")));
    if (rv.installed) out.push(hwl("ok", te("setup.hw.resolve")));
    // the video tools ship with the app: only worth a line when they are broken
    if (dt.ffmpeg && !ff.ok) out.push(hwl("bad", tHtml("setup.hw.ffmpeg_html")));
    return `<div class="su-sub">${te("setup.hw.title")}</div><div class="su-hw">${out.join("")}</div>`;
  }
  function stepWelcome() {
    return `<h2 tabindex="-1">${te("setup.welcome.title")}</h2>
      <p class="lead">${te("setup.welcome.lead")}</p>
      <p class="lead"><b>${te("setup.welcome.questions")}</b></p>
      ${hwLines()}
      <p class="hint">${te("setup.welcome.hint")}</p>`;
  }
  function rootInfo(c) {
    if (!c || c.pending) return `<span class="faint">${te("setup.root.checking")}</span>`;
    if (c.error) return `<span class="faint">${te("setup.root.cant_check")}</span>`;
    if (!c.exists || !c.is_dir) return `<span class="bad-t">${te("setup.root.not_found")}</span>`;
    if (!c.n_videos) return `<span class="warn-t">${te("setup.root.no_videos")}</span>`;
    const many = c.capped || c.n_videos >= 2000;
    return `<span class="ok-t">${te(many ? "setup.root.videos_capped" : "setup.root.videos", { count: c.n_videos })}</span>`;
  }
  function rootRow(r, i) {
    const src = r.src === "obs" ? `<span class="badge acc">${te("setup.root.from_obs")}</span>` : r.src === "current" ? `<span class="badge">${te("setup.badge.in_use")}</span>` : "";
    return `<div class="su-root${r.on ? " on" : ""}"><input type="checkbox" id="su-r${i}" data-ri="${i}" ${r.on ? "checked" : ""} aria-label="${te("setup.root.use_aria")}">
      <label for="su-r${i}" class="mono path" title="${esc(r.path)}">${esc(r.path)}</label><span class="meta">${src}<span class="info">${rootInfo(S.checks[r.k])}</span></span>
      <button type="button" class="ib sm" data-rx="${i}" title="${te("setup.root.remove_title")}" aria-label="${te("setup.root.remove_aria", { path: r.path })}">${icon("x")}</button></div>`;
  }
  const noRoots = () => `<div class="su-none">${te("setup.folders.none")}</div>`;
  function stepFolders() {
    return `<h2 tabindex="-1">${te("setup.folders.title")}</h2>
      <p class="lead">${te("setup.folders.lead")}</p>
      <div class="su-roots" id="su-roots">${S.roots.length ? S.roots.map(rootRow).join("") : noRoots()}</div>
      <div class="su-add">
        <button type="button" class="btn" id="su-pick" ${S.picking ? "disabled" : ""}>${icon("folder")}${te(S.picking ? "setup.folders.waiting" : "setup.folders.add_folder")}</button>
        <input class="inp" type="text" id="su-path" placeholder="${te("setup.folders.path_ph")}" autocomplete="off" spellcheck="false" aria-label="${te("setup.folders.path_aria")}">
        <button type="button" class="btn" id="su-path-add">${te("setup.folders.add")}</button>
      </div>
      <label class="su-check"><input type="checkbox" id="su-skiproots" ${S.skipRoots ? "checked" : ""}><span>${te("setup.folders.skip")}</span></label>`;
  }
  function chipBox(id, vals, ph) {
    return `<div class="su-chips" id="${id}" data-chips>${vals.map((v, i) => `<span class="su-tag">${esc(v)}<button type="button" data-cx="${i}" aria-label="${te("setup.chips.remove_aria", { name: v })}">${icon("x")}</button></span>`).join("")}<input type="text" placeholder="${esc(vals.length ? t("setup.chips.add_another_ph") : ph)}" autocomplete="off" spellcheck="false" aria-label="${esc(ph)}"></div>`;
  }
  function stepYou() {
    return `<h2 tabindex="-1">${te("setup.you.title")}</h2>
      <p class="lead">${te("setup.you.lead")}</p>
      <div class="su-field"><div class="su-lab"><b>${te("setup.you.name")}</b><small>${te("setup.you.name_help")}</small></div>${chipBox("su-aliases", S.aliases, chipPh("su-aliases"))}</div>
      <div class="su-field"><div class="su-lab"><b>${te("setup.you.squad")}</b><small>${te("setup.you.squad_help")}</small></div>${chipBox("su-squad", S.squad, chipPh("su-squad"))}</div>
      <p class="hint">${te("setup.you.hint")}</p>`;
  }
  function sizeLine(ids) {
    if (!ids.length) return `<span class="badge ok">${icon("check")}${te("setup.size.downloaded")}</span>`;
    const tot = ids.reduce((a, id) => a + sizeOf(id), 0);
    return tot ? `<span class="num">${te(ids.includes("cuda") ? "setup.size.one_time" : "setup.pack.about_size", { size: mb(tot) })}</span>` : "";
  }
  // the speech language: one language, or detect it (a mixed squad); a short list is set in Settings
  function speechLangField() {
    const cat = (S.data && S.data.speech_languages) || null;
    if (!cat) return "";
    const v = S.sttLang || "en", list = v.includes(",");
    const opts = cat.languages.map(l => `<option value="${esc(l.code)}" ${v === l.code ? "selected" : ""}>${l.native !== l.name ? te("setup.lang.option", { native: l.native, name: (window.SL && SL.nameOf && SL.nameOf(l.code)) || l.name }) : esc(l.native)}</option>`).join("")
      + `<option value="auto" ${v === "auto" ? "selected" : ""}>${te("setup.lang.auto")}</option>`
      + (list ? `<option value="${esc(v)}" selected>${te("setup.lang.picked")}</option>` : "");
    return `<div class="su-field"><div class="su-lab"><b>${te("setup.lang.title")}</b><small>${te("setup.lang.help")}</small></div>
      <select class="inp" id="su-lang" aria-label="${te("setup.lang.aria")}">${opts}</select></div>`;
  }
  function stepSpeech() {
    const ch = speechChoices();
    const cards = ch.map((c, i) => `<label class="opt su-card"><input type="radio" name="su-speech" value="${esc(c.key)}" ${c.key === S.speech ? "checked" : ""}>
        <span><b>${esc(c.name)}</b>${i === 0 && c.key !== "keep" ? ` <span class="badge acc">${te("setup.badge.recommended")}</span>` : ""}<small>${esc(c.text)}</small></span>
        ${sizeLine(choiceIds(c))}</label>`).join("");
    return `<h2 tabindex="-1">${te("setup.speech.title")}</h2>
      <p class="lead">${te("setup.speech.lead")}</p>
      <label class="su-switch"><input type="checkbox" class="toggle" id="su-stt" ${S.sttEnabled ? "checked" : ""}><span><b>${te("setup.speech.switch")}</b><small>${te("setup.speech.switch_help")}</small></span></label>
      ${S.sttEnabled ? speechLangField() : ""}
      ${S.sttEnabled ? `${gpuNow().nvidia ? "" : `<p class="hint">${te(otherCard() ? "setup.speech.other_card" : "setup.speech.no_card")}</p>`}<div class="su-sub">${te("setup.speech.pick")}</div><div class="su-cards">${cards}</div>
      <p class="hint">${te("setup.speech.models_hint")}</p>` : ""}`;
  }
  function vodInfo() {
    const vc = S.vodCheck;
    if (!vc || vc.error) return "";
    if (vc.pending) return `<span class="faint">${te("setup.root.checking")}</span>`;
    const free = vc.free_gb != null ? `<span class="faint"> · ${te("setup.vod.free", { size: fmtUnit(Math.round(vc.free_gb), "gigabyte") })}</span>` : "";
    if (!vc.exists) return `<span class="muted">${te("setup.vod.will_create")}</span>${free}`;
    if (!vc.is_dir) return `<span class="bad-t">${te("setup.vod.is_file")}</span>`;
    return `<span class="ok-t">${te("setup.vod.found")}</span>${free}`;
  }
  function stepVods() {
    const dn = PK.get("deno");
    const yes = S.vods;
    return `<h2 tabindex="-1">${te("setup.vods.title")}</h2>
      <p class="lead">${te("setup.vods.lead")}</p>
      <div class="su-cards">
        <label class="opt su-card"><input type="radio" name="su-vods" value="yes" ${yes ? "checked" : ""}><span><b>${te("setup.vods.yes")}</b><small>${te("setup.vods.yes_help")}</small></span></label>
        <label class="opt su-card"><input type="radio" name="su-vods" value="no" ${yes ? "" : "checked"}><span><b>${te("setup.vods.not_now")}</b><small>${te("setup.vods.not_now_help")}</small></span></label>
      </div>
      ${yes ? `<div class="su-vodsbox">
        <div class="su-saveto"><span class="muted">${te("setup.vods.saved_to")}</span> <span class="mono path" title="${esc(vodDirShown())}">${esc(vodDirShown() || t("setup.vods.videos_folder"))}</span>
          <button type="button" class="btn sm" id="su-vods-pick" ${S.picking ? "disabled" : ""}>${icon("folder")}${te(S.picking ? "setup.vods.waiting" : "setup.vods.change")}</button></div>
        ${S.vodsEdit ? `<div class="su-add"><input class="inp" type="text" id="su-vods" value="${esc(S.vodsDir)}" placeholder="${esc(cur().vods_dir_effective || t("setup.vods.path_ph"))}" autocomplete="off" spellcheck="false" aria-label="${te("setup.vods.path_aria")}"></div>` : ""}
        <div class="su-line" id="su-vods-line">${vodInfo()}</div>
        ${denoNeeded() ? `<div class="sline"><span class="sd"></span><span>${te("setup.vods.deno", { size: mb((dn && dn.size_mb) || 41) })}</span></div>` : ""}
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
  // the wizard's plain names for its downloads (setup.wiz.<id>)
  const WIZ_NAMES = { cuda: "setup.wiz.cuda", deno: "setup.wiz.deno" };
  const wizName = (id) => WIZ_NAMES[id] ? t(WIZ_NAMES[id]) : (isModel(id) ? t("setup.wiz.model") : titleOf(PK.get(id) || { id }));
  function stepFinish() {
    if (S.saved) return finishProgress();
    const roots = S.roots.filter(r => r.on);
    const inst = installList();
    const total = inst.reduce((a, id) => a + sizeOf(id), 0);
    const li = (k, v) => `<div class="su-sum"><span>${te(k)}</span><div>${v}</div></div>`;
    const ch = curChoice();
    return `<h2 tabindex="-1">${te("setup.finish.title")}</h2>
      <p class="lead">${te(roots.length ? "setup.finish.lead_scan" : "setup.finish.lead_save")}</p>
      <div class="su-summary">
        ${li("setup.sum.footage", roots.length ? `${te("setup.sum.folders", { count: roots.length })}${roots.map(r => `<div class="mono faint">${esc(r.path)}</div>`).join("")}` : `<span class="warn-t">${te("setup.sum.no_folders")}</span>`)}
        ${li("setup.sum.name", S.aliases.length ? esc(fmtList(S.aliases)) : `<span class="warn-t">${te("setup.sum.no_name")}</span>`)}
        ${S.squad.length ? li("setup.sum.squad", esc(fmtList(S.squad))) : ""}
        ${li("setup.sum.speech", S.sttEnabled ? (ch ? (ch.key === "keep" ? te("setup.sum.current_choice") : esc(ch.name)) : te("setup.sum.on")) : `<span class="faint">${te("setup.sum.off")}</span>`)}
        ${li("setup.sum.vods", S.vods ? te("setup.sum.yes") : `<span class="faint">${te("setup.sum.not_now")}</span>`)}
        ${li("setup.sum.download", inst.length ? `${te("setup.pack.about_size", { size: mb(total) })} <span class="faint">· ${esc(fmtList(inst.map(wizName)))}</span>` : `<span class="faint">${te("setup.sum.nothing")}</span>`)}
      </div>
      ${inst.length ? `<p class="hint">${te("setup.finish.hint")}</p>` : ""}`;
  }
  function finishProgress() {
    return `<h2 tabindex="-1">${te("setup.done.title")}</h2>
      <p class="lead">${te(S.roots.some(r => r.on) ? "setup.done.lead_scan" : "setup.done.lead_add")}</p>
      ${S.started.length ? `<div class="su-sub">${te("setup.done.downloading")}</div><div class="su-prog" id="su-prog"></div><p class="hint">${te("setup.done.hint")}</p>` : ""}`;
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
    if (ok || (p.state === "done" && !failed)) status = `<div class="pk-note ok">${te("setup.wiz.ready")}</div>`;
    else if (failed) status = `<div class="pk-note warn-t">${te("setup.wiz.failed")}</div>`;
    else if (stopped) status = `<div class="pk-note">${te("setup.wiz.stopped")}</div>`;
    const prog = act ? `<div class="pk-prog"><div class="pg"><i style="width:${cssPct(p.progress)}"></i></div><span class="num" data-pt>${esc(progText(p))}</span></div>` : "";
    const btn = act ? `<button type="button" class="btn sm" data-pk="cancel" data-id="${esc(p.id)}">${icon("x")}${te("setup.btn.cancel")}</button>`
      : failed || stopped ? `<button type="button" class="btn sm" data-pk="install" data-id="${esc(p.id)}">${icon("refresh")}${te("setup.btn.retry")}</button>` : "";
    return `<div class="pk-row${act ? " act" : ""}" id="${domId(p.id)}-w" data-sig="${esc(rowSig(p, "wiz"))}">
      <div class="pk-main"><div class="pk-t"><b>${esc(wizName(p.id))}</b>${sizeOf(p.id) ? `<span class="faint">${te("setup.pack.about_size", { size: mb(sizeOf(p.id)) })}</span>` : ""}</div>${prog}${status}</div>
      <div class="pk-acts">${btn}</div></div>`;
  }

  // ---- validation per step (Next): "" = fine, else {k, p} (a catalog key + its params) ----
  function validate(step) {
    if (step === 1) {
      const inp = $("su-path"); if (inp && inp.value.trim()) addRoot(inp.value, "typed");
      const on = S.roots.filter(r => r.on);
      if (!on.length && !S.skipRoots) return { k: "setup.err.no_folder" };
      const bad = on.filter(r => { const c = S.checks[r.k]; return c && !c.pending && !c.error && (!c.exists || !c.is_dir); });
      if (bad.length) return { k: "setup.err.folder_missing", p: { paths: fmtList(bad.map(r => r.path)) } };
    }
    if (step === 2) {
      commitChips();
      if (!S.aliases.length && !S.nameWarned) { S.nameWarned = true; return { k: "setup.err.no_name" }; }
    }
    if (step === 4) {
      readVodInput();
      const vc = S.vodCheck;
      if (S.vods && vc && !vc.pending && !vc.error && vc.exists && !vc.is_dir) return { k: "setup.err.vod_file" };
    }
    return "";
  }
  function readVodInput() { const v = $("su-vods"); if (v) S.vodsDir = v.value.trim(); }
  const errText = () => S.err ? t(S.err.k, S.err.p || {}) : "";
  const clearErr = () => { S.err = ""; $("su-msg").textContent = ""; };

  // ---- render + wire ----
  function render() {
    const body = $("su-body"); if (!body) return;
    const v = (hasST() && ST.status && ST.status.version) || "";
    $("su-ver").textContent = v ? t("setup.version", { version: v }) : "";
    $("su-steps").innerHTML = STEPS.map((n, i) => `<li class="${i === S.step ? "on" : i < S.step || (S.saved && i <= S.maxStep) ? "done" : ""}">
      <button type="button" data-st="${i}" ${i <= S.maxStep && !S.saved ? "" : "disabled"} ${i === S.step ? 'aria-current="step"' : ""}><span class="n">${i < S.step || S.saved ? icon("check") : i + 1}</span><span>${te("setup.step." + n)}</span></button></li>`).join("");
    body.innerHTML = [stepWelcome, stepFolders, stepYou, stepSpeech, stepVods, stepFinish][S.step]();
    const last = S.step === STEPS.length - 1;
    $("su-back").hidden = S.step === 0 || S.saved;
    $("su-next").innerHTML = S.saved ? te("setup.btn.open_app") : last ? `${icon("check")}${te("setup.btn.start")}` : S.step === 0 ? `${te("setup.btn.lets_go")}${icon("chev")}` : `${te("setup.btn.next")}${icon("chev")}`;
    $("su-next").disabled = S.saving;
    $("su-skip").hidden = S.saved;
    $("su-skip").textContent = t(S.first ? "setup.btn.skip" : "setup.btn.cancel");
    $("su-msg").textContent = errText(); $("su-msg").classList.toggle("bad", !!S.err);
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
    if (S.sttEnabled && S.sttLang) body.stt_language = S.sttLang;
    if (S.vods) body.vods_dir = S.vodsDir;                 // "Not now" leaves the VOD settings as they are
    try {
      let r;
      try { r = await req("POST", "/api/setup", body); }
      catch (e) {
        if (e.status !== 404 && e.status !== 405) throw e;
        // no setup route on this server: save the same choices through the plain settings API (no installs)
        const patch = { library: { roots: body.roots }, player: { aliases: body.aliases, squad: body.squad }, stt: { enabled: body.stt_enabled }, setup: { done: true } };
        if (body.stt_model) patch.stt.model = body.stt_model;
        if (body.stt_language) patch.stt.language = body.stt_language;
        if ("vods_dir" in body) patch.vods = { dir: body.vods_dir };
        r = await req("POST", "/api/settings", patch);
        r.started = []; if (install.length) toast(t("setup.toast.saved_no_installs"), 6000);
      }
      afterSaved(r);
      S.started = Array.isArray(r.started) ? r.started.map(x => typeof x === "string" ? x : x && x.id).filter(Boolean) : install;
      for (const id of S.started) if (!PK.get(id) || !isActive(PK.get(id))) merge(Object.assign({ id }, PK.get(id) || {}, { state: "downloading", progress: 0 }));
      S.saved = true; S.maxStep = STEPS.length - 1;
      refreshPacks();
    } catch (e) { S.err = { k: e.status ? "setup.err.save_failed" : "setup.err.no_answer" }; }
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
    catch (e) { toast(t("setup.toast.skip_failed"), 5000); return; }
    close(); toast(t("setup.toast.skipped"), 5000);
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
    try { const r = await req("POST", "/api/system/pick-folder", { initial: initial || "", title: title || t("setup.pick.title") }); return { path: r && r.path ? r.path : null }; }
    catch (e) {
      if (e.status === 409) toast(t("setup.pick.busy"), 5000);
      else if (e.status !== 403) toast(t("setup.pick.failed"), 5000);
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
  function chipPh(id) { return t(id === "su-aliases" ? "setup.chips.name_ph" : "setup.chips.squad_ph"); }
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
      const txt = (e.clipboardData || window.clipboardData).getData("text");
      if (!/[;,\n]/.test(txt)) return;
      e.preventDefault(); addNames(list, inp.value + txt); inp.value = ""; repaint();
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
      box.onchange = (e) => { const i = e.target.dataset.ri; if (i == null) return; S.roots[+i].on = e.target.checked; e.target.closest(".su-root").classList.toggle("on", e.target.checked); if (e.target.checked) S.skipRoots = false; clearErr(); };
      box.onclick = (e) => { const x = e.target.closest("[data-rx]"); if (!x) return; S.roots.splice(+x.dataset.rx, 1); paintRoots(); };
      const inp = $("su-path");
      const addTyped = () => { if (!inp.value.trim()) { inp.focus(); return; } addRoot(inp.value, "typed"); inp.value = ""; clearErr(); $("su-skiproots").checked = false; inp.focus(); };
      inp.onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); addTyped(); } };
      $("su-path-add").onclick = addTyped;
      $("su-pick").onclick = async () => {
        const first = S.roots.find(r => r.on) || S.roots[0];
        const r = await pick(first ? first.path : "", t("setup.pick.obs_title"));
        if (S.step !== 1) return;
        render();
        if (r.path) { addRoot(r.path, "picked"); clearErr(); $("su-skiproots").checked = false; }
        const b = r.failed ? $("su-path") : $("su-pick"); if (b) b.focus();
      };
      $("su-skiproots").onchange = (e) => { S.skipRoots = e.target.checked; clearErr(); };
    }
    if (S.step === 2) { wireChips("su-aliases", S.aliases); wireChips("su-squad", S.squad); }
    if (S.step === 3) {
      $("su-stt").onchange = (e) => { S.sttEnabled = e.target.checked; render(); $("su-stt").focus(); };
      $("su-body").querySelectorAll("input[name=su-speech]").forEach(r => r.onchange = () => { S.speech = r.value; });
      const sl = $("su-lang"); if (sl) sl.onchange = () => { S.sttLang = sl.value; render(); const n = $("su-lang"); if (n) n.focus(); };
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
        const r = await pick(vodDirShown(), t("setup.pick.vods_title"));
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
  function paintSaveTo() { const p = $("su-body").querySelector(".su-saveto .path"); if (p) { p.textContent = vodDirShown() || t("setup.vods.videos_folder"); p.title = vodDirShown(); } }
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
      if (e.key === "Escape" && S.first && !S.saved) { e.preventDefault(); e.stopPropagation(); toast(t("setup.toast.escape"), 3000); }
      // Alt+arrows step through the wizard from anywhere inside it
      else if (e.altKey && (e.key === "ArrowRight" || e.key === "ArrowLeft") && !S.saved) { e.preventDefault(); if (e.key === "ArrowRight") next(); else go(Math.max(0, S.step - 1)); }
    });
    d.addEventListener("cancel", (e) => { if (S.first && !S.saved) e.preventDefault(); });
    // last line of defense: a first-run wizard closed by anything but Finish / Skip (a browser that ignores the
    // cancel guard) opens again on the step it was on
    d.addEventListener("close", () => {
      if (S.open && S.first && !S.saved && !S.allowClose) { setTimeout(() => { if (!d.open) { d.showModal(); render(); } }, 0); return; }
      S.open = false;
    });
  }

  // a language switch: redraw what this module shows (the wizard keeps its step, its answers and what was typed
  // into the field on screen; the Settings > Add-ons rows repaint because the language is part of their sig)
  function onLanguage() {
    paintUpdLink(); renderAll();
    if (!S.open) return;
    if (S.loading) { $("su-body").innerHTML = loadingHtml(); return; }
    if (S.step === 4) readVodInput(); else if (S.step === 2) commitChips();
    const typed = S.step === 1 && $("su-path") ? $("su-path").value : "";
    const ae = document.activeElement, fid = ae && ae.id && $("dlg-setup").contains(ae) ? ae.id : "";
    render();
    if (typed && $("su-path")) $("su-path").value = typed;
    if (fid && $(fid)) $(fid).focus();
  }
  if (typeof I18N !== "undefined" && I18N.onChange) I18N.onChange(onLanguage);

  async function boot() {
    wireDialog();
    const sb = $("sb-dl"); if (sb) sb.onclick = () => { if (typeof openSettings === "function") openSettings("addons"); };
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
