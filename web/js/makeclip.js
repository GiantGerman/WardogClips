/* Make clip: a trimmed copy of a moment into the clip folder (media/makeclip.py on the server).
   Entry points: the player's clip tools (#pl-makeclip), the event card (#ev-makeclip), the command palette.
   The server answers when the job is queued; the finished clip arrives on SSE "makeclip" (MC.onEvent). */
const MC = (() => {
  if (typeof ICONS === "object" && !ICONS.cut) {
    ICONS.cut = '<circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M20 4L8.1 15.9M14.5 14.5L20 20M8.1 8.1L12 12"/>';
  }

  // what to cut: the event asked for (event card, palette) or the event in the player, with the player's in / out
  // marks when it is that event; the player's button (fromPlayer) otherwise cuts a window around the playhead
  // of the recording on screen; with nothing in the player, the selected event's clip
  function target(opts) {
    opts = opts || {};
    const P = window.PL ? PL.state : {};
    const playing = !!(P.rec && P.kind && P.url);
    const ev = opts.event || (P.ev && P.kind ? P.ev : null) || (opts.fromPlayer && playing ? null : ST.event) || null;
    if (ev) {
      const body = { event_id: ev.id };
      if (P.ev && P.ev.id === ev.id && P.inT != null && P.outT != null) { body.in = P.inT; body.out = P.outT; }
      return body;
    }
    if (playing && $("pl-video")) {
      const v = $("pl-video"); const at = (P.kind === "full" ? 0 : (P.w0 || 0)) + (v.currentTime || 0);
      const pre = (ST.settings && ST.settings.highlights && ST.settings.highlights.pre_roll_s) || 8;
      const post = (ST.settings && ST.settings.highlights && ST.settings.highlights.post_roll_s) || 4;
      return { rec_id: P.rec.id, in: Math.max(0, at - pre), out: at + post };
    }
    return null;
  }

  async function make(opts) {
    const body = target(opts);
    if (!body) { toast(t("makeclip.pick_first"), 4000); return; }
    if (opts && opts.mode) body.mode = opts.mode;
    try {
      const r = await post("/api/makeclip", body);
      if (r.already_queued) toast(t("makeclip.already"), 4000);
      else toast(r.mode === "exact"
        ? t("makeclip.making_exact")
        : t("makeclip.making_copy"), 4000);
    } catch (err) { toast(err.message, 6000); }
  }

  async function openFolder(id) {
    try { await post("/api/makeclip/open-folder", id != null ? { id } : {}); }
    catch (err) { toast(err.message, 6000); }
  }

  // a toast with an "Open folder" button (toast() sets plain text, so the button is added after it)
  function doneToast(msg, id) {
    toast(msg, 9000);
    const tb = $("toast"); if (!tb) return;
    const b = document.createElement("button");
    b.type = "button"; b.className = "btn sm"; b.textContent = t("makeclip.open_folder");
    b.onclick = () => { tb.hidden = true; openFolder(id); };
    tb.appendChild(b);
  }

  function onEvent(d) {
    if (d.action === "done" && d.clip) {
      const c = d.clip;
      const early = c.mode === "copy" && c.req_in - c.in_s > 0.0005 ? c.req_in - c.in_s : 0;
      let msg = early ? t("makeclip.saved_early", { name: c.name, seconds: fmtUnit(early, "second", 1) }) : t("makeclip.saved", { name: c.name });
      // a separate sentence on its own line
      if (c.checks && c.checks.ok === false) msg += "\n" + t("makeclip.check_mismatch");
      doneToast(msg, c.id);
    } else if (d.action === "failed") {
      toast(t("makeclip.failed", { error: (d.error && srvText(d, "error")) || t("makeclip.unknown_error") }), 8000);
    }
  }

  function wire() {
    const b = $("pl-makeclip"); if (!b) return;
    b.onclick = () => make({ fromPlayer: true });
    // the page swapped its icon placeholders before this file added the "cut" icon: fill it in now
    const sv = b.querySelector("svg.i"); if (sv && !sv.innerHTML.trim() && typeof ICONS === "object") sv.innerHTML = ICONS.cut;
  }
  wire();
  return { make, openFolder, onEvent, target };
})();
window.MC = MC;
