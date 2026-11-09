/* Queue moves (window.QM): "Move to top of queue" for a recording, and the Jobs panel's quick moves --
   per-row Top / Up / Down / Bottom, shift / ctrl multi-select moved as a block, Alt+Up / Alt+Down and
   Home / End while the list has the focus, "Group by recording", and a "Next up" mark on the first queued
   job of each lane. Every move is one POST /api/jobs/move; app.js calls in here from renderJobs, the
   recording menu, the Recording tab, the palette and the T key. */
"use strict";
(function () {
  Object.assign(ICONS, {
    qtop: '<path d="M5 4h14M12 20V9M7 13l5-5 5 5"/>', qbot: '<path d="M5 20h14M12 4v11M7 11l5 5 5-5"/>',
    qup: '<path d="M12 19V6M6 11l6-6 6 6"/>', qdown: '<path d="M12 5v13M6 13l6 6 6-6"/>',
  });
  const GROUP_LS = "wclips.jobsGroup";
  const QM = {
    sel: new Set(),          // selected queued job ids
    anchor: null,            // last plainly clicked row (shift+click extends from it)
    grouped: false,
    collapsed: new Set(),    // recording ids folded in the grouped view
    busy: false,
    keepFocus: false,        // the Jobs list had the keyboard focus before a re-render
  };
  try { QM.grouped = localStorage.getItem(GROUP_LS) === "1"; } catch (e) { }

  async function move(body) {
    if (QM.busy) return null;
    QM.busy = true;
    try { return await post("/api/jobs/move", body); }
    finally { QM.busy = false; }
  }

  /* -- a recording (or several) to the top ------------------------------------------------------ */
  QM.recToTop = async function (recIds) {
    const ids = (Array.isArray(recIds) ? recIds : [recIds]).filter(Boolean);
    if (!ids.length) { toast(t("queue.select_first")); return; }
    let r;
    try { r = await move({ rec_ids: ids, to: "top" }); } catch (err) { toast(err.message, 5000); return; }
    if (!r) return;
    if (r.moved) toast(r.idle.length ? t("queue.rec.moved_some_idle")
      : ids.length > 1 ? t("queue.rec.moved_many")
      : t("queue.rec.moved_one"), 3500);
    else if (r.idle.length) {
      if (!confirm(t("queue.rec.nothing_queued_confirm"))) return;
      try { r = await move({ rec_ids: ids, to: "top", queue_missing: true }); } catch (err) { toast(err.message, 5000); return; }
      if (!r) return;
      toast(r.queued.length ? t("queue.rec.queued_missing") : t("queue.rec.nothing_left"), 3500);
    } else if (r.boosted.length) toast(t("queue.rec.boosted"), 4000);
    if (ST.tab === "jobs") loadJobs();
  };

  // "Move this stage type to top": every queued job of that stage, keeping their order
  QM.stageToTop = async function (stage) {
    let r;
    try { r = await move({ stages: [stage], to: "top" }); } catch (err) { toast(err.message, 5000); return; }
    if (!r) return;
    const key = stage === "transcribe" || stage === "detect" ? stage : "other";
    toast(t(`queue.stage.${key}_${r.moved ? "moved" : "none"}`), 3000);
    if (ST.tab === "jobs") loadJobs();
  };

  /* -- the Jobs panel ------------------------------------------------------------------------- */
  const queuedIds = () => (ST.jobs || []).filter(j => j.state === "queued").map(j => j.id);

  // the queued rows as the panel shows them: claim order, or grouped by recording (first appearance)
  QM.arrange = function (rows) {
    if (!QM.grouped) return rows;
    const groups = new Map();
    // jobs without a recording (VOD downloads) share one group at the end, under their own header
    for (const j of rows) { const k = j.rec_id || "other"; if (!groups.has(k)) groups.set(k, []); groups.get(k).push(j); }
    const other = groups.get("other"); if (other) { groups.delete("other"); groups.set("other", other); }
    return [].concat(...groups.values());
  };
  // the Queued section title as HTML-escaped text (app.js renderJobs inserts it into markup as is)
  QM.sectionLabel = function (nq) {
    if (nq <= 1) return te("queue.section.queued");
    return QM.grouped ? te("queue.section.grouped") : te("queue.section.drag");
  };
  QM.groupToggle = function () {
    return `<button type="button" class="btn ghost sm qm-grp ${QM.grouped ? "on" : ""}" id="qm-group" aria-pressed="${QM.grouped}" title="${te("queue.group.button_title")}">${te("queue.group.button")}</button>`;
  };
  // markup placed before a queued row: the recording header in the grouped view
  QM.before = function (j, prev) {
    if (!QM.grouped || j.state !== "queued") return "";
    const k = j.rec_id || 0;
    if (prev && prev.state === "queued" && (prev.rec_id || 0) === k) return "";
    if (!k) return `<div class="qm-ghd"><span class="qm-gname">${te("queue.group.other")}</span></div>`;
    const n = ST.jobs.filter(x => x.state === "queued" && x.rec_id === k).length;
    const name = recLabelFor(k, j.rec_name || "");
    const open = !QM.collapsed.has(k);
    return `<div class="qm-ghd" data-grec="${k}"><button type="button" class="ib sm qm-fold" data-fold="${k}" aria-expanded="${open}" title="${open ? te("queue.group.fold_title") : te("queue.group.unfold_title")}">${icon(open ? "chevd" : "chev")}</button>
      <span class="qm-gname" title="${esc(name)}">${esc(name)}</span><span class="faint t-xs num">${esc(fmtNum(n))}</span>
      <button type="button" class="ib sm" data-gtop="${k}" title="${te("queue.group.top_title")}">${icon("qtop")}</button></div>`;
  };
  QM.hidden = (j) => QM.grouped && j.state === "queued" && j.rec_id && QM.collapsed.has(j.rec_id);
  QM.rowClass = (j) => (j.state === "queued" && QM.sel.has(j.id) ? " jsel" : "");
  // the "Next up" mark: the first queued job of each lane (what that lane claims when a slot frees)
  QM.nextUp = function () {
    const seen = new Set(), out = new Set();
    for (const j of ST.jobs || []) if (j.state === "queued" && !seen.has(j.lane)) { seen.add(j.lane); out.add(j.id); }
    return out;
  };
  QM.badge = (j, next) => next.has(j.id) ? `<span class="badge acc qm-next" title="${te("queue.next.badge_title")}">${te("queue.next.badge")}</span>` : "";
  QM.tools = (j) => j.state !== "queued" ? "" : `<span class="qm-tools">
      <button type="button" class="ib sm" data-qmv="top" data-id="${j.id}" title="${te("queue.tool.top_title")}">${icon("qtop")}</button>
      <button type="button" class="ib sm" data-qmv="up" data-id="${j.id}" title="${te("queue.tool.up_title")}">${icon("qup")}</button>
      <button type="button" class="ib sm" data-qmv="down" data-id="${j.id}" title="${te("queue.tool.down_title")}">${icon("qdown")}</button>
      <button type="button" class="ib sm" data-qmv="bottom" data-id="${j.id}" title="${te("queue.tool.bottom_title")}">${icon("qbot")}</button></span>`;

  // the jobs a move acts on: the whole selection when the row is part of it, else that row
  function targets(id) {
    const q = queuedIds();
    if (id != null && !QM.sel.has(id)) return [id];
    return q.filter(x => QM.sel.has(x));
  }
  async function moveJobs(ids, to) {
    if (!ids.length) return;
    let r;
    try { r = await move({ ids, to }); } catch (err) { toast(t("queue.move.failed_refreshed", { error: err.message }), 4000); loadJobs(); return; }
    if (!r) return;
    if (!r.ids.length) toast(t("queue.move.gone"), 3500);
    else if (!r.moved) toast(to === "up" || to === "top" ? t("queue.move.at_top") : t("queue.move.at_bottom"));
    await loadJobs();
    const row = document.getElementById("job-" + ids[0]); if (row) row.scrollIntoView({ block: "nearest" });
  }

  QM.wire = function (el) {
    const live = new Set(queuedIds());
    for (const id of [...QM.sel]) if (!live.has(id)) QM.sel.delete(id);
    const g = el.querySelector("#qm-group");
    if (g) g.onclick = (e) => { e.stopPropagation(); QM.grouped = !QM.grouped; try { localStorage.setItem(GROUP_LS, QM.grouped ? "1" : "0"); } catch (x) { } renderJobs(); };
    el.querySelectorAll("button[data-qmv]").forEach(b => b.onclick = (e) => { e.stopPropagation(); moveJobs(targets(+b.dataset.id), b.dataset.qmv); });
    el.querySelectorAll("button[data-fold]").forEach(b => b.onclick = (e) => {
      e.stopPropagation(); const k = +b.dataset.fold; if (QM.collapsed.has(k)) QM.collapsed.delete(k); else QM.collapsed.add(k); renderJobs();
    });
    el.querySelectorAll("button[data-gtop]").forEach(b => b.onclick = (e) => { e.stopPropagation(); QM.recToTop(+b.dataset.gtop); });
    if (QM.grouped) el.querySelectorAll(".jrow.jq").forEach(r => { r.draggable = false; r.classList.add("nodrag"); });
    const list = el.querySelector("#jlist");
    if (list) {
      list.tabIndex = 0;
      list.addEventListener("focusin", () => { QM.keepFocus = true; });
      // a re-render replaces the list: give the focus back so Alt+arrows / Home / End keep working
      if (QM.keepFocus && ST.tab === "jobs" && ST.panelOpen && !document.querySelector("dialog[open]")) {
        const a = document.activeElement;
        if (!a || a === document.body) list.focus({ preventScroll: true });
      }
    }
  };
  document.addEventListener("mousedown", (e) => { const p = document.getElementById("tab-jobs"); if (!p || !p.contains(e.target)) QM.keepFocus = false; }, true);

  // shift / ctrl click on a queued row selects; a plain click selects that row (and app.js opens its recording)
  QM.click = function (e, row) {
    if (!row.classList.contains("jq")) { QM.sel.clear(); paintSel(); return false; }
    const id = +row.dataset.job;
    if (e.shiftKey && QM.anchor != null) {
      const q = [...document.querySelectorAll("#jlist .jrow.jq")].map(r => +r.dataset.job);
      const a = q.indexOf(QM.anchor), b = q.indexOf(id);
      if (a >= 0 && b >= 0) { if (!e.ctrlKey && !e.metaKey) QM.sel.clear(); for (let i = Math.min(a, b); i <= Math.max(a, b); i++) QM.sel.add(q[i]); }
      paintSel(); return true;
    }
    if (e.ctrlKey || e.metaKey) { if (QM.sel.has(id)) QM.sel.delete(id); else QM.sel.add(id); QM.anchor = id; paintSel(); return true; }
    QM.sel.clear(); QM.sel.add(id); QM.anchor = id; paintSel();
    return false;
  };
  function paintSel() { document.querySelectorAll("#jlist .jrow").forEach(r => r.classList.toggle("jsel", QM.sel.has(+r.dataset.job))); }

  // keys while the Jobs list has the focus: Alt+Up / Alt+Down move the selection one step, Home / End to the top / bottom
  QM.key = function (e) {
    if (!QM.sel.size || document.querySelector("dialog[open]")) return;
    const t = e.target; if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT")) return;
    let to = null;
    if (e.altKey && e.key === "ArrowUp") to = "up";
    else if (e.altKey && e.key === "ArrowDown") to = "down";
    else if (!e.altKey && !e.ctrlKey && !e.metaKey && e.key === "Home") to = "top";
    else if (!e.altKey && !e.ctrlKey && !e.metaKey && e.key === "End") to = "bottom";
    if (!to) return;
    e.preventDefault(); e.stopPropagation();
    moveJobs(targets(null), to);
  };
  document.addEventListener("keydown", (e) => { const p = document.getElementById("tab-jobs"); if (p && p.contains(e.target)) QM.key(e); }, true);

  window.QM = QM;
})();
