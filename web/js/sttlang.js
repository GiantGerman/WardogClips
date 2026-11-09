/* Speech languages (global SL): the "Transcribe in..." menu of a recording, Settings > Speech-to-text language
   (+ the voice keywords per language in Settings > Highlights), and the small language tags on transcript lines
   and search hits. Backend: GET /api/stt/languages, POST /api/settings, POST /api/recordings/{id}/transcribe
   {language}. Uses core.js helpers (api, post, esc, icon, toast) and app.js's openMenu / ST. */
const SL = (() => {
  let CAT = null;                       // the last GET /api/stt/languages

  async function load(force) {
    if (!CAT || force) { try { CAT = await api("/api/stt/languages"); } catch (e) { /* older server: no pickers */ } }
    return CAT;
  }
  const langs = () => (CAT && CAT.languages) || [];
  // a language's name in the page's language (Intl), else the server's English name, else the code
  let DN = null, DNloc = "";
  function sysName(code) {
    try {
      if (!DN || DNloc !== I18N.locale) { DNloc = I18N.locale; DN = new Intl.DisplayNames([DNloc], { type: "language" }); }
      const n = DN.of(code);
      return n && n.toLowerCase() !== String(code).toLowerCase() ? n : "";
    } catch (e) { return ""; }
  }
  const nameOf = (code) => { const l = langs().find(x => x.code === code); return (code && sysName(code)) || (l ? l.name : String(code || "").toUpperCase()); };
  const nativeOf = (code) => { const l = langs().find(x => x.code === code); return l ? l.native : String(code || "").toUpperCase(); };
  const unsupportedNote = () => t("sttlang.unsupported");
  // a model size given in MB (decimal), with the language's unit names
  const mb = (n) => n >= 1000 ? fmtUnit(n / 1000, "gigabyte", 1) : fmtUnit(Math.round(n), "megabyte");
  // the speech setting in plain words: Detect automatically / a language / Detect among a few
  function describe(v) {
    const mode = modeOf(v);
    if (mode === "auto") return t("sttlang.auto");
    if (mode === "fixed") return nameOf(String(v || "en"));
    return t("sttlang.describe.list", { languages: fmtList(String(v).split(",").map(nameOf)) });
  }

  /* ---- right-click / "..." menu of a recording: Transcribe in... ---- */
  function menuItem(r) {
    return { ic: "mic", label: t("sttlang.menu.item"), sub: t("sttlang.menu.item_sub"), act: () => openPicker(r) };
  }
  async function openPicker(r) {
    await load();
    if (!CAT) { toast(t("sttlang.menu.no_picker"), 4000); return; }
    const own = r.stt_language || "";
    // code "" = the language from Settings, "auto" = detect, else one language
    const pick = (code) => async () => {
      const res = await post(`/api/recordings/${r.id}/transcribe`, { language: code });
      r.stt_language = code || null;
      if (ST.detail && ST.detail.id === r.id) ST.detail.stt_language = code || null;
      const kind = !code ? "settings" : code === "auto" ? "auto" : "lang";
      toast(t(`sttlang.${res.already_queued ? "already" : "queued"}.${kind}`, { language: kind === "lang" ? nameOf(code) : "" }), 3500);
    };
    const items = [{ h: t("sttlang.menu.head") },
      { ic: "settings", label: t("sttlang.menu.from_settings"), sub: CAT.setting ? describe(CAT.setting) : (CAT.describe || ""), on: !own, act: pick("") },
      { ic: "mic", label: t("sttlang.auto"), sub: t("sttlang.menu.auto_sub"), on: own === "auto", act: pick("auto") },
      "-"];
    for (const l of langs()) { const nm = nameOf(l.code); items.push({ ic: "tx", label: l.native, sub: l.native !== nm ? nm : "", on: own === l.code, act: pick(l.code) }); }
    items.push("-", { label: unsupportedNote(), disabled: true });
    const anchor = document.getElementById("rec-" + r.id) || $("d-transcribe") || $("t-run") || document.querySelector(".brand");
    openMenu(anchor, items);
    const p = $("pop"); if (p) { p.style.maxHeight = "70vh"; p.style.overflowY = "auto"; }
  }

  /* ---- Settings > Speech-to-text: the language + the model it really uses ---- */
  function modeOf(v) { return v === "auto" ? "auto" : String(v || "en").includes(",") ? "list" : "fixed"; }
  function renderSettings() {
    const box = $("sl-set"); if (!box || !CAT) return;
    const v = CAT.setting || "en", mode = modeOf(v), codes = mode === "auto" ? [] : v.split(",");
    const optName = (l) => { const nm = nameOf(l.code); return l.native !== nm ? te("sttlang.lang_option", { native: l.native, name: nm }) : esc(l.native); };
    const opts = [`<option value="auto" ${mode === "auto" ? "selected" : ""}>${te("sttlang.auto")}</option>`]
      .concat(langs().map(l => `<option value="${esc(l.code)}" ${mode === "fixed" && codes[0] === l.code ? "selected" : ""}>${optName(l)}</option>`))
      .concat([`<option value="list" ${mode === "list" ? "selected" : ""}>${te("sttlang.set.list_option")}</option>`]);
    let html = `<div class="field"><label>${te("sttlang.set.field")}</label><select class="inp" id="sl-mode">${opts.join("")}</select></div>`;
    if (mode === "list") {
      html += `<div class="sl-pick" id="sl-pick">${langs().map(l => `<label class="sl-opt"><input type="checkbox" value="${esc(l.code)}" ${codes.includes(l.code) ? "checked" : ""}><span>${esc(l.native)}</span></label>`).join("")}</div>`
        + `<div class="hint sl-hint">${te("sttlang.set.list_hint")}</div>`;
    }
    const eff = CAT.effective_model || CAT.model, multi = eff !== CAT.model;
    const sizeTxt = CAT.effective_size_mb ? mb(CAT.effective_size_mb) : "";
    html += `<div class="sline"><span class="sd ${CAT.effective_cached ? "ok" : "warn"}"></span><span>`
      + (multi ? tHtml("sttlang.model.multi_html", { model: eff, base: CAT.model }) : tHtml("sttlang.model.one_html", { model: eff }))
      // a second, separate sentence about the download
      + " " + (CAT.effective_cached ? te("sttlang.model.cached") : (sizeTxt ? te("sttlang.model.not_cached_size", { size: sizeTxt }) : te("sttlang.model.not_cached")))
      + `</span></div><div class="hint sl-hint">${esc(unsupportedNote())}</div>`;
    box.innerHTML = html;
    $("sl-mode").onchange = () => {
      const m = $("sl-mode").value;
      if (m === "list") { save(codes.length > 1 ? codes.join(",") : [codes[0] || "en", codes[0] === "es" ? "en" : "es"].join(",")); return; }
      save(m);
    };
    const pk = $("sl-pick");
    if (pk) pk.querySelectorAll("input").forEach(cb => cb.onchange = () => {
      const on = [...pk.querySelectorAll("input:checked")].map(x => x.value);
      save(on.length ? on.join(",") : "en");
    });
  }
  async function save(value) {
    try {
      const r = await post("/api/settings", { stt: { language: value } });
      ST.settings = r.settings;
      const m = $("dmsg"); if (m) { m.className = "msg"; m.textContent = r.changed.length ? t("sttlang.saved", { keys: r.changed.join(", ") }) : t("sttlang.saved_none"); }
      await load(true); renderSettings(); renderKeywords();
      if (window.SU && SU.refreshPacks) SU.refreshPacks();
    } catch (err) { const m = $("dmsg"); if (m) { m.className = "msg bad"; m.textContent = t("sttlang.error", { error: err.message }); } }
  }

  /* ---- Settings > Highlights: voice keywords per language ---- */
  function renderKeywords() {
    const box = $("sl-kw"); if (!box || !CAT) return;
    const kw = CAT.keywords || {};
    const spoken = CAT.setting && CAT.setting !== "auto" ? CAT.setting.split(",") : [];
    const order = [...new Set(["en", ...spoken, ...Object.keys(kw)])];
    box.innerHTML = order.map(code => `<div class="field"><label>${te("sttlang.kw.label", { language: nativeOf(code) })} <span class="faint">${te("sttlang.kw.sep")}</span></label>`
      + `<input class="inp" type="text" data-kw="${esc(code)}" value="${esc((kw[code] || []).join("; "))}"></div>`).join("")
      + `<div class="hint sl-hint">${te("sttlang.kw.hint")}</div>`;
    box.querySelectorAll("[data-kw]").forEach(inp => inp.onchange = async () => {
      const row = inp.closest(".field");
      try {
        const r = await post("/api/settings", { highlights: { keywords: { [inp.dataset.kw]: inp.value } } });
        ST.settings = r.settings; CAT.keywords = r.settings.highlights.keywords;
        const m = $("dmsg"); if (m) { m.className = "msg"; m.textContent = r.changed.length ? t("sttlang.saved", { keys: r.changed.join(", ") }) : t("sttlang.saved_none"); }
        if (row) { row.classList.add("saved"); setTimeout(() => row.classList.remove("saved"), 1400); }
      } catch (err) { const m = $("dmsg"); if (m) { m.className = "msg bad"; m.textContent = t("sttlang.error", { error: err.message }); } }
    });
  }
  async function mountSettings() { await load(true); renderSettings(); renderKeywords(); }

  /* ---- transcript lines + search hits ---- */
  // a tag when the line's language is not the recording's main one, and the English translation under it
  function line(s, summary) {
    let out = "";
    const main = summary && summary.lang;
    if (s.lang && main && s.lang !== main) out += `<span class="badge sl-tag" title="${te("sttlang.tag.spoken_title", { language: nameOf(s.lang) })}">${esc(s.lang)}</span>`;
    return out;
  }
  function lineEnglish(s) {
    return s.text_en ? `<span class="sl-en" title="${te("sttlang.tag.english_title")}">${esc(s.text_en)}</span>` : "";
  }
  function head(summary) {
    const ls = Object.keys((summary && summary.langs) || {});
    if (!ls.length) return "";
    return ` · <span title="${te("sttlang.tag.heard_title")}">${esc(fmtList(ls.map(nameOf)))}</span>`;
  }
  function hit(h) {
    let out = "";
    if (h.lang && h.lang !== "en") out += `<span class="badge sl-tag" title="${te("sttlang.tag.spoken_title", { language: nameOf(h.lang) })}">${esc(h.lang)}</span>`;
    // "EN" is the language code shown on the tag, like the other tags
    if (h.matched === "text_en") out += `<span class="badge sl-tag" title="${te("sttlang.tag.found_en_title")}">EN</span>`;
    return out;
  }

  load();                               // names for the tags; the pickers reload when they open
  return { load, menuItem, openPicker, mountSettings, line, lineEnglish, head, hit, nameOf };
})();
window.SL = SL;
