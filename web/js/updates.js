/* Update prompt (global UPD, also window.UPD).
   - State = GET /api/updates (setup.js checkUpdates hands every answer to UPD.onState) and the SSE "updates" events
     (app.js connectSSE -> UPD.onState). The server checks on its own (startup + every few hours).
   - A non-blocking card (#upd-card, bottom right; no backdrop) shows "WardogClips <ver> is available", the release
     notes as PLAIN TEXT (textContent only: remote text never becomes markup) and Update now / Later / Skip this version.
     A source run (or a release without an installer) gets "Download page" instead of Update now.
   - Update now: POST /api/updates/install; a release without a checksum asks a second time (Install anyway);
     the card then shows the download, the check and "Starting the installer"; the app closes itself after that.
   - The status bar link #sb-update and the tray's "Update available..." (it opens the page with #update) reopen it.
   - Settings > Updates: UPD.mountSettings() wires #up-check and fills #up-res (state, last checked). */
"use strict";
const UPD = (() => {
  let st = null;                 // the last state from the server
  let shownFor = "";             // the version the card was closed for this page session ("" = none)
  let forced = false;            // opened by the user (status bar / tray / Settings): show even after Later
  let confirming = false;        // the second question for a release without a checksum
  let busy = false;
  let wantHash = location.hash === "#update";

  const safeUrl = (u) => /^https:\/\/github\.com\//i.test(String(u || "")) ? String(u) : "";
  const ver = (v) => String(v || "").replace(/^v/i, "");
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const btn = (text, cls, fn) => { const b = el("button", "btn sm" + (cls ? " " + cls : ""), text); b.type = "button"; b.onclick = fn; return b; };
  const when = (iso) => { const d = new Date(String(iso || "")); return isNaN(d.getTime()) ? String(iso || "").replace("T", " ").slice(0, 16) : fmtDateTime(d, "datetime"); };
  // a reason the server gave by code, in the page's language (the server's English text when the code is unknown)
  const reasonText = (code, english) => code && I18N.has("updates.reason." + code) ? t("updates.reason." + code) : (english || "");

  function card() {
    let c = $("upd-card");
    if (!c) { c = el("div", "upd-card"); c.id = "upd-card"; c.setAttribute("role", "dialog"); c.setAttribute("aria-live", "polite"); c.hidden = true; document.body.appendChild(c); }
    return c;
  }

  function wanted() {
    if (!st) return false;
    if (["downloading", "verifying", "starting"].includes(st.state)) return true;
    if (forced) return st.newer || st.state === "failed";
    if (st.state === "failed") return shownFor !== "x-failed";
    return !!st.prompt && shownFor !== st.latest;
  }

  function render() {
    const c = card();
    if (!wanted()) { c.hidden = true; return; }
    c.replaceChildren();
    const head = el("div", "upd-h");
    head.appendChild(el("b", "", t("updates.card.title", { version: ver(st.latest) })));
    const x = el("button", "ib sm upd-x"); x.type = "button"; x.title = t("updates.card.close"); x.setAttribute("aria-label", t("updates.card.close")); x.innerHTML = icon("x");
    x.onclick = () => close(false);
    if (!["downloading", "verifying", "starting"].includes(st.state)) head.appendChild(x);
    c.appendChild(head);
    c.appendChild(el("div", "upd-sub muted", t("updates.card.current", { version: ver(st.current) })));
    const page = safeUrl(st.url);
    const acts = el("div", "upd-acts");

    if (st.state === "downloading" || st.state === "verifying" || st.state === "starting") {
      const p = st.state === "downloading" ? (st.total ? st.progress : 0) : 1;
      const msg = st.state === "downloading" ? t("updates.card.downloading", { percent: fmtPct(p) })
        : st.state === "verifying" ? t("updates.card.verifying") : t("updates.card.starting");
      c.appendChild(el("div", "upd-msg", msg));
      const bar = el("div", "pg upd-pg"); const fill = el("i"); fill.style.width = (p * 100).toFixed(1) + "%"; bar.appendChild(fill); c.appendChild(bar);
      if (st.state === "downloading") acts.appendChild(btn(t("updates.card.cancel"), "ghost", () => act("/api/updates/cancel")));
      c.appendChild(acts); c.hidden = false; return;
    }
    if (st.state === "failed") {
      c.appendChild(el("div", "upd-msg bad", reasonText(st.reason_code, st.reason) || t("updates.card.failed")));
      if (st.can_install) acts.appendChild(btn(t("updates.card.try_again"), "pri", () => install(false)));
      if (page) acts.appendChild(link(t("updates.card.download_page"), page));
      acts.appendChild(btn(t("updates.card.close"), "ghost", () => close(false)));
      c.appendChild(acts); c.hidden = false; return;
    }
    if (confirming) {
      c.appendChild(el("div", "upd-msg warn", t("updates.reason.need_confirm")));
      acts.appendChild(btn(t("updates.card.install_anyway"), "pri", () => install(true)));
      acts.appendChild(btn(t("updates.card.cancel"), "ghost", () => { confirming = false; render(); }));
      c.appendChild(acts); c.hidden = false; return;
    }
    if (st.notes) { const n = el("div", "upd-notes"); n.textContent = st.notes; c.appendChild(n); }
    if (st.reason_code === "cancelled") c.appendChild(el("div", "upd-msg muted", t("updates.card.canceled")));
    if (st.can_install) acts.appendChild(btn(t("updates.card.update_now"), "pri", () => install(false)));
    else if (page) acts.appendChild(link(t("updates.card.download_page"), page, true));
    acts.appendChild(btn(t("updates.card.later"), "", later));
    acts.appendChild(btn(t("updates.card.skip"), "ghost", skip));
    c.appendChild(acts);
    if (!st.can_install && st.known !== false) c.appendChild(el("div", "upd-foot muted", st.installed_build ? t("updates.card.no_installer") : t("updates.card.from_source")));
    c.hidden = false;
  }

  function link(text, href, pri) {
    const a = el("a", "btn sm" + (pri ? " pri" : ""), text); a.href = href; a.target = "_blank"; a.rel = "noopener noreferrer"; return a;
  }

  function close(keepForced) {
    if (st) shownFor = st.state === "failed" ? "x-failed" : st.latest;
    if (!keepForced) forced = false;
    confirming = false;
    card().hidden = true;
  }

  async function act(url, body) {
    if (busy) return null; busy = true;
    try { const r = await post(url, body || {}); onState(r); return r; }
    catch (e) { if (typeof toast === "function") toast(e.message, 5000); return null; }
    finally { busy = false; }
  }

  async function later() { await act("/api/updates/later"); close(false); }
  async function skip() { await act("/api/updates/skip", { version: st && st.latest }); close(false); }

  async function install(confirmUnverified) {
    if (busy) return; busy = true;
    try {
      const r = await fetch("/api/updates/install", { method: "POST", headers: Object.assign({ "Content-Type": "application/json" }, WC_HDR), body: JSON.stringify({ confirm_unverified: !!confirmUnverified }) });
      const j = await r.json().catch(() => ({}));
      if (j && j.code === "need_confirm") { confirming = true; onState(j); return; }
      confirming = false;
      if (!r.ok) { if (j && j.reason && typeof toast === "function") toast(reasonText(j.code, j.reason), 6000); if (j && j.state) onState(j); return; }
      forced = true; onState(j);
    } catch (e) { if (typeof toast === "function") toast(e.message, 5000); }
    finally { busy = false; }
  }

  function statusBar() {
    const a = $("sb-update"); if (!a || !st) return;
    const show = !!(st.newer && st.latest && !st.skipped);
    a.hidden = !show;
    if (show) { a.textContent = t("updates.bar.link", { version: ver(st.latest) }); a.href = safeUrl(st.url) || "#"; a.title = t("updates.bar.link_title"); }
  }

  function settingsLine() {
    const line = $("up-res"); if (!line || !st) return;
    line.replaceChildren();
    const dot = el("span", "sd"), txt = el("span");
    const s = st.state;
    if (s === "disabled") { dot.classList.add("warn"); txt.textContent = t("updates.set.disabled"); }
    else if (s === "checking") { txt.textContent = t("updates.set.checking"); }
    else if (s === "could_not_check") { dot.classList.add("warn"); txt.textContent = reasonText(st.reason_code, st.reason) || t("updates.set.could_not_check"); }
    else if (st.newer && st.latest) {
      dot.classList.add("acc"); txt.textContent = st.skipped ? t("updates.set.available_skipped", { version: ver(st.latest) }) : t("updates.set.available", { version: ver(st.latest) });
    } else if (s === "up_to_date") { dot.classList.add("ok"); txt.textContent = t("updates.set.up_to_date", { version: ver(st.current) }); }
    else { txt.textContent = t("updates.set.not_checked"); }
    line.appendChild(dot); line.appendChild(txt);
    if (st.newer && st.latest) { const b = btn(t("updates.set.show_update"), "", () => show()); b.style.marginLeft = "8px"; line.appendChild(b); }
    const last = el("div", "upd-last muted");
    last.textContent = st.checked ? t("updates.set.last_checked", { when: when(st.checked) }) : t("updates.set.last_checked_never");
    line.appendChild(last);
  }

  function onState(s) {
    if (!s || typeof s !== "object" || !("state" in s)) return;
    const prev = st;
    st = s;
    if (prev && prev.latest !== s.latest) shownFor = "";
    if (wantHash) { wantHash = false; forced = true; shownFor = ""; try { history.replaceState(null, "", location.pathname + location.search); } catch (e) { } }
    statusBar(); settingsLine(); render();
  }

  function show() {
    forced = true; shownFor = ""; confirming = false;
    // after a restart only the saved version number is known: look the release up again (notes, installer)
    if (st && st.known === false && !["checking", "downloading", "verifying", "starting"].includes(st.state)) act("/api/updates/check");
    else render();
  }

  function mountSettings() {
    const cn = $("up-check");
    if (cn) cn.onclick = async () => {
      cn.disabled = true;
      try { const u = await post("/api/updates/check", {}); onState(u); }
      catch (e) { const line = $("up-res"); if (line) line.textContent = e.message; }
      finally { cn.disabled = false; }
    };
    if (st) settingsLine(); else api("/api/updates").then(onState).catch(() => { });
  }

  function boot() {
    const a = $("sb-update");
    if (a) a.addEventListener("click", (e) => { if (st && st.newer) { e.preventDefault(); show(); } });
    window.addEventListener("hashchange", () => { if (location.hash === "#update") { wantHash = true; if (st) onState(st); } });
    // a language switch: redraw what is on screen in the new language
    if (typeof I18N === "object") I18N.onChange(() => { if (st) { statusBar(); settingsLine(); render(); } });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else boot();

  return { onState, show, mountSettings, get state() { return st; } };
})();
window.UPD = UPD;
