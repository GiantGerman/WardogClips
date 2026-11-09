/* UI language picker (server: server/routes/i18n.py; runtime: i18n.js).
   - Top bar: label#lang-pick > select#lang-sel (native names; machine-translated languages in a "beta" group).
   - Settings > General: LP.settingsHtml() / LP.mountSettings() (app.js openDrawer draws the group first).
   - Ctrl+K palette: "Change language..." -> LP.menu() (a menu under the top-bar picker).
   A switch saves ui.language (POST /api/i18n/language), loads the new catalogs, re-translates the static page and
   asks every module to draw its content again (I18N.onChange listeners, then the shell's render functions here).
   Loaded last: it only looks up app.js globals when a function runs. */
"use strict";

const LP = {
  st: null,          // GET /api/i18n/languages: {active, setting, system, languages: [...]}

  async refresh() {
    try { LP.st = await api("/api/i18n/languages"); } catch (e) { LP.st = null; }
    LP.fill($("lang-sel"), false); LP.wizard();
    return LP.st;
  },

  langs() { return (LP.st && LP.st.languages) || I18N.languages; },
  nameOf(code) {
    if (code === I18N.PSEUDO) return t("core.lang.pseudo");
    const x = LP.langs().find(l => l.code === code); return x ? x.native : code;
  },

  // <option>s: the released languages, then the machine-translated ones under a "beta" heading
  optionsHtml(selected, withSystem) {
    const L = LP.langs(); const o = (x) => `<option value="${esc(x.code)}" lang="${esc(x.code)}" ${x.code === selected ? "selected" : ""}>${esc(x.native)}</option>`;
    let h = "";
    if (withSystem) h += `<option value="" ${selected === "" ? "selected" : ""}>${te("core.lang.system", { name: LP.nameOf((LP.st && LP.st.system) || "en-US") })}</option>`;
    h += L.filter(x => !x.beta).map(o).join("");
    const beta = L.filter(x => x.beta);
    if (beta.length) h += `<optgroup label="${te("core.lang.beta_group")}">${beta.map(o).join("")}</optgroup>`;
    if (I18N.pseudoAllowed) h += `<option value="${I18N.PSEUDO}" ${selected === I18N.PSEUDO ? "selected" : ""}>${te("core.lang.pseudo")}</option>`;
    return h;
  },
  fill(sel, withSystem) {
    if (!sel) return;
    const cur = withSystem ? (I18N.pseudo ? I18N.PSEUDO : ((LP.st && LP.st.setting) || "")) : I18N.lang;
    sel.innerHTML = LP.optionsHtml(cur, withSystem);
    sel.value = cur;
  },

  async set(code) {
    if (LP.busy) return;
    LP.busy = true;
    try {
      await I18N.setLanguage(code);
      if (code !== I18N.PSEUDO) {
        await LP.refresh();
        if (window.ST && ST.settings) { ST.settings.ui = Object.assign({}, ST.settings.ui, { language: (LP.st && LP.st.setting) || code }); }
      } else LP.fill($("lang-sel"), false);
      toast(t("core.lang.switched", { name: LP.nameOf(I18N.lang) }));
    } catch (err) {
      toast(t("core.lang.failed", { error: err.message }), 5000);
      LP.fill($("lang-sel"), false);
    } finally { LP.busy = false; }
  },

  // Settings > General
  settingsHtml() {
    const x = LP.langs().find(l => l.code === I18N.lang);
    return `<div class="field"><label>${te("core.lang.field")}</label><select class="inp" id="lang-set">${LP.optionsHtml((LP.st && LP.st.setting) || "", true)}</select></div>` +
      (x && x.beta ? `<div class="hint lp-beta">${te("core.lang.beta_note", { name: x.native })}</div>` : "");
  },
  mountSettings() {
    const s = $("lang-set"); if (!s) return;
    LP.fill(s, true);
    s.onchange = () => LP.set(s.value);
  },

  // the palette entry: a menu of every language under the top-bar picker
  menu() {
    const anchor = $("lang-pick");
    if (typeof openMenu !== "function" || !anchor) { const s = $("lang-sel"); if (s) { s.focus(); try { s.showPicker(); } catch (e) { } } return; }
    const items = [{ h: t("core.lang.label") }];
    for (const x of LP.langs()) items.push({ ic: "globe", label: x.native, sub: x.native !== x.english ? x.english : "", on: x.code === I18N.lang, act: () => LP.set(x.code) });
    if (I18N.pseudoAllowed) items.push("-", { ic: "globe", label: t("core.lang.pseudo"), on: I18N.pseudo, act: () => LP.set(I18N.PSEUDO) });
    openMenu(anchor, items);
  },

  // the setup wizard's own language menu (the top-bar picker sits behind the wizard)
  wizard() {
    const s = $("su-lang"); if (!s) return;
    LP.fill(s, false);
    const l = $("su-lang-l"); if (l) l.textContent = t("core.lang.field");
    s.onchange = () => { if (s.value !== I18N.lang) LP.set(s.value); };
  },

  // after a switch: the shell draws its dynamic parts again (each module also listens on I18N.onChange)
  rerender() {
    const call = (name, ...a) => { const fn = window[name]; if (typeof fn === "function") { try { return fn(...a); } catch (e) { console.error(e); } } };
    LP.fill($("lang-sel"), false);
    LP.wizard();
    ["renderHelp", "renderRail", "renderCrumb", "renderCtxExtra", "renderLegend", "renderPill", "renderResolvePill", "renderRecs",
      "renderEvents", "renderDetail", "renderSaid", "renderTranscript", "renderClips", "renderJobs"].forEach(n => call(n));
    call("refreshStatus");
    const d = $("drawer");
    if (d && !d.hidden && typeof openSettings === "function") { const top = $("dbody") ? $("dbody").scrollTop : 0; Promise.resolve(openSettings()).then(() => { if ($("dbody")) $("dbody").scrollTop = top; }); }
  },
};
window.LP = LP;

(function lpBoot() {
  const sel = $("lang-sel"); if (!sel) return;
  LP.fill(sel, false);
  LP.wizard();
  sel.onchange = () => { if (sel.value !== I18N.lang) LP.set(sel.value); };
  I18N.onChange(() => {
    LP.rerender();
  });
  LP.refresh();
})();
