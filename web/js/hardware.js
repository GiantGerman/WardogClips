/* Settings > Hardware: the graphics cards and what WardogClips may do with each (GET /api/system/gpus).
   app.js openDrawer renders the mount #hw-gpus and calls HW.mount(); a role change saves at once like every
   Settings field (POST /api/settings {hardware: {leave_alone: [...]}}). */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  // video memory (MiB from the driver) in whole GB / MB, with the language's unit names
  const gb = (mb) => mb >= 1024 ? fmtUnit(Math.round(mb / 1024), "gigabyte") : fmtUnit(mb, "megabyte");

  // one whole sentence per kind of card (catalog "hardware")
  function canDo(g) {
    if (g.vendor === "nvidia") return t("hardware.can.nvidia");
    if (g.integrated) return t("hardware.can.integrated");
    return t("hardware.can.other");
  }

  function render(d) {
    const box = $("hw-gpus"); if (!box) return;
    const gpus = (d && d.gpus) || [];
    if (!gpus.length) {
      box.innerHTML = `<div class="sline"><span class="sd warn"></span><span>${te("hardware.none_found")}</span></div>`;
      return;
    }
    const leave = new Set(gpus.filter(g => g.role === "leave").map(g => g.key).concat((d.unknown_keys || [])));
    let html = gpus.map(g => `<div class="field hw-gpu" data-key="${esc(g.key)}">
        <label>${esc(g.name)}${g.vram_mb ? ` <span class="muted">${esc(gb(g.vram_mb))}</span>` : ""}<br><small class="muted">${esc(canDo(g))}</small></label>
        <select class="inp" data-hw-key="${esc(g.key)}">
          <option value="use" ${g.role === "use" ? "selected" : ""}>${te("hardware.role.use")}</option>
          <option value="leave" ${g.role === "leave" ? "selected" : ""}>${te("hardware.role.leave")}</option>
        </select></div>`).join("");
    const used = gpus.filter(g => g.role === "use").length;
    if (!used) html += `<div class="sline"><span class="sd warn"></span><span>${te("hardware.all_left_alone")}</span></div>`;
    html += `<div class="hint" style="padding:4px 12px 8px">${te("hardware.shared_hint")}</div>`;
    html += `<div class="hint" style="padding:0 12px 8px">${te("hardware.apply_hint")}</div>`;
    box.innerHTML = html;
    box.querySelectorAll("select[data-hw-key]").forEach(sel => sel.onchange = async () => {
      const key = sel.dataset.hwKey;
      if (sel.value === "leave") leave.add(key); else leave.delete(key);
      const row = sel.closest(".field");
      try {
        const r = await post("/api/settings", { hardware: { leave_alone: [...leave] } });
        if (typeof ST !== "undefined") ST.settings = r.settings;     // app.js state (a top-level const, not on window)
        const m = $("dmsg"); if (m) { m.className = "msg"; m.textContent = r.changed.length ? t("hardware.saved", { keys: r.changed.join(", ") }) : t("hardware.saved_none"); }
        if (row) { row.classList.add("saved"); setTimeout(() => row.classList.remove("saved"), 1400); }
        if (typeof refreshEncoderLine === "function") refreshEncoderLine();
        load();
      } catch (err) { const m = $("dmsg"); if (m) { m.className = "msg bad"; m.textContent = t("hardware.error", { error: err.message }); } }
    });
  }

  async function load() {
    try { render(await api("/api/system/gpus")); }
    catch (e) { const box = $("hw-gpus"); if (box) box.innerHTML = `<div class="hint">${te("hardware.load_failed")}</div>`; }
  }

  window.HW = { mount: load, render };
})();
