/* Work modes (server: core/workmode.py, GET / POST /api/system/workmode, SSE "workmode").
   Status-bar chip #sb-work (mode + why it is slowed down; click = quick switch menu) and the Settings > Performance
   group (WM.settingsHtml, autosaved by app.js like every [data-sec] field). Every UI string is a whole sentence from
   the "workmode" catalog; the server's English label / help / note are only used for a mode or note it does not know. */
"use strict";

const WM = {
  st: null,
  // the mode ids (API values); names and one-line descriptions come from the catalog at render time
  MODES: ["full", "balanced", "background"],

  // the server state's texts in the page's language (the server's English for an id the catalog does not know)
  label(s) { const k = "workmode.mode." + s.mode; return I18N.has(k) ? t(k) : (s.label || s.mode || ""); },
  help(s) { const k = "workmode.help." + s.mode; return I18N.has(k) ? t(k) : (s.help || ""); },
  note(s) {
    if (!s.note) return "";
    const k = "workmode.note." + (s.note_key || "");
    return s.note_key && I18N.has(k) ? t(k) : s.note;
  },

  async refresh() {
    try { WM.st = await api("/api/system/workmode"); } catch (e) { return null; }
    WM.render();
    return WM.st;
  },
  onEvent(d) { if (d && d.state) { WM.st = d.state; WM.render(); } },

  render() {
    const s = WM.st; const b = $("sb-work"); if (!s || !b) return;
    const slow = s.paused || s.waiting || s.throttled;
    const note = WM.note(s);
    b.querySelector(".sd").className = "sd " + (slow ? "warn" : s.mode === "full" ? "acc" : "ok");
    $("sb-work-t").textContent = WM.label(s);
    const n = $("sb-work-n"); n.textContent = note; n.hidden = !note;
    b.dataset.tip = note ? t("workmode.chip_tip_note", { help: WM.help(s), note }) : WM.help(s);
    const now = $("wm-now"); if (now) now.innerHTML = WM.nowHtml(s);
  },

  nowHtml(s) {
    const rows = [];
    const note = WM.note(s);
    rows.push(`<span class="sd ${s.paused || s.waiting || s.throttled ? "warn" : "ok"}"></span><span><b>${esc(WM.label(s))}</b>${note ? ` <span class="wm-note">${esc(note)}</span>` : ""}</span>`);
    const sig = [];
    if (s.gpu_3d != null) sig.push(t("workmode.sig.gpu", { percent: fmtPct(Math.round(s.gpu_3d) / 100) }));
    if (s.screen === "fullscreen_d3d") sig.push(t("workmode.sig.game_fullscreen"));
    else if (s.screen === "fullscreen_app" || s.screen === "presentation" || s.screen === "store_app_fullscreen") sig.push(t("workmode.sig.app_fullscreen"));
    if (s.encoder_sessions) sig.push(t("workmode.sig.encoder"));
    if (s.idle_s != null) sig.push(t("workmode.sig.idle", { ago: wmIdleText(s.idle_s) }));
    // each signal is a whole sentence; the dot only separates them
    return `<div class="wm-now-row">${rows.join("")}</div>` + (sig.length ? `<div class="hint">${sig.map(esc).join(" · ")}</div>` : "");
  },

  async set(body) {
    try { WM.st = await post("/api/system/workmode", body); WM.render(); if (typeof refreshStatus === "function") refreshStatus(); }
    catch (err) { toast(err.message, 5000); }
  },

  menu(anchor) {
    const s = WM.st || {};
    openMenu(anchor, [
      { h: t("workmode.menu.speed") },
      ...WM.MODES.map(id => ({ ic: "bolt", label: t("workmode.mode." + id), sub: t("workmode.mode." + id + "_sub"), on: s.mode === id, act: () => WM.set({ mode: id }) })),
      "-",
      { ic: "pausej", label: t("workmode.menu.pause"), sub: t("workmode.menu.pause_sub"), on: !!s.paused, act: () => WM.set({ paused: !s.paused }) },
      { ic: "settings", label: t("workmode.menu.settings"), act: () => openSettings("performance") },
    ]);
  },

  // Settings > Performance: the fields carry data-sec / data-f, so app.js autosaves them like the others
  settingsHtml(settings) {
    const p = (settings && settings.performance) || {};
    const opts = WM.MODES.map(id => `<option value="${id}" ${p.mode === id ? "selected" : ""}>${te("workmode.mode." + id)}</option>`).join("");
    return `<div class="hint wm-explain">${te("workmode.explain")}</div>` +
      `<div class="sline" id="wm-now">${WM.st ? WM.nowHtml(WM.st) : ""}</div>` +
      `<div class="field"><label>${te("workmode.set.mode")}</label><select class="inp" data-sec="performance" data-f="mode">${opts}</select></div>` +
      `<div class="field"><label>${te("workmode.set.auto_detect")}</label><input class="toggle" type="checkbox" data-sec="performance" data-f="auto_detect" ${p.auto_detect ? "checked" : ""}></div>` +
      `<div class="field"><label>${te("workmode.set.idle_min")}</label><input class="inp" type="number" step="1" min="1" max="120" data-sec="performance" data-f="idle_min" value="${esc(p.idle_min)}"></div>` +
      `<div class="field"><label>${te("workmode.set.gpu_busy_pct")}</label><input class="inp" type="number" step="5" min="10" max="95" data-sec="performance" data-f="gpu_busy_pct" value="${esc(p.gpu_busy_pct)}"></div>`;
  },
};

// how long ago the keyboard or mouse was last used, in the page's language ("7 min. ago")
function wmIdleText(sec) {
  sec = Math.max(0, Math.round(sec));
  return fmtRelative(Date.now() / 1000 - sec);
}

window.WM = WM;

(function wmBoot() {
  const b = $("sb-work"); if (!b) return;
  b.onclick = (e) => { e.stopPropagation(); if (popAnchor === b) { closePop(); return; } WM.menu(b); };
  // a language switch: the chip and the Settings line follow (the drawer itself is rebuilt by the language picker)
  if (typeof I18N === "object") I18N.onChange(() => WM.render());
  WM.refresh();
})();
