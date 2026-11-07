/* VOD browser + favorites (server side = server/routes/vodbrowse.py).
   Lives in the VODs side panel (#vb-panel, mounted by app.js through VB.mountPanel each time
   the panel is shown). Two faces in that one panel: the LIST face (search box, site chips, search results, favorites
   + Check for new) and the STREAMER face (one streamer's recent VODs: tick, Import selected, Load older). Without a
   panel mount (an older index.html) the same markup opens in a modal <dialog id="dlg-vb">.
   A listing is a yt-dlp run on the server (1-5 s, longer when a site is slow), so every answer carries a request
   token: a slow older answer never paints over a newer view, and a channel's ticks / import results live on that
   channel's own state object, so an import that finishes after you moved on cannot mark rows of another streamer.
   Only window.VB is global. */
(function () {
  "use strict";
  const SITES = ["YouTube", "Twitch", "Kick", "TikTok", "Rumble"];
  const LS_KEY = "wclips.vb";               // per-viewer convenience: which site chips are on
  const PAGE = 30;                          // VODs per listing; "Load older" asks for 30 more
  const MAX_LIST = 100;                     // the server clamps a listing to 100 (media/vodbrowse.list_vods)

  const S = {
    host: null,                             // the element holding the markup (#vb-panel, or the dialog's body)
    sites: new Set(SITES), sitesLoaded: false,
    favs: [], favErr: "", favBusy: false, favTok: 0, refreshTok: 0, newCounts: {}, favErrors: {}, newIds: {},
    favLoaded: 0,
    view: "home",                           // home | busy | error | search | channel | single
    viewTok: 0, inflight: "", busyMsg: "", errMsg: "",
    q: "", results: [],                     // the last search (the streamer face's back arrow returns to it)
    ch: null,                               // the streamer on screen, see openChannel()
    single: null,                           // a VOD link whose streamer could not be told
  };

  /* ---------------- small helpers ---------------- */
  const lc = (s) => String(s ?? "").trim().toLowerCase();
  const chKey = (provider, key) => lc(provider) + "|" + lc(key);
  const asList = (x, field) => Array.isArray(x) ? x : (x && Array.isArray(x[field]) ? x[field] : []);
  const httpUrl = (u) => typeof u === "string" && /^https?:\/\//i.test(u) ? u : "";
  const favKey = (f) => f.channel_key ?? f.key ?? "";
  const favName = (f) => f.channel_name || f.name || favKey(f);
  const favUrl = (f) => f.channel_url || f.url || "";
  const favOf = (provider, key) => S.favs.find(f => chKey(f.provider, favKey(f)) === chKey(provider, key)) || null;
  // a streamer opened by one key (YouTube 'channel/UC...') may come back under the site's canonical one
  // ('@handle'): ch.key is the canonical key, ch.alias every key it was opened by -- lookups try them all
  const chKeys = (ch) => [ch.key].concat(ch.alias ? [...ch.alias] : []);
  const isCh = (ch, provider, key) => !!ch && chKeys(ch).some(k => chKey(ch.provider, k) === chKey(provider, key));
  const chFav = (ch) => { for (const k of chKeys(ch)) { const f = favOf(ch.provider, k); if (f) return f; } return null; };
  const chNewIds = (ch) => { for (const k of chKeys(ch)) { const s = S.newIds[chKey(ch.provider, k)]; if (s) return s; } return null; };
  // a comparable form of a VOD link, as media/vodbrowse.url_key: no fragment, no query except YouTube's v=,
  // host lower case without www. / m., no trailing slash, path lower case except on YouTube (ids are case-sensitive)
  function urlKey(u) {
    let p;
    try { p = new URL(String(u || "").trim().split("#", 1)[0]); }
    catch (e) {
      try { p = new URL("https://" + String(u || "").trim().split("#", 1)[0]); } catch (e2) { return ""; }
    }
    let host = p.hostname.toLowerCase();
    for (const pre of ["www.", "m."]) if (host.startsWith(pre)) host = host.slice(pre.length);
    const yt = host.includes("youtube");
    const path = p.pathname.replace(/\/+$/, "");
    const v = yt ? p.searchParams.get("v") : null;
    return host + (yt ? path : path.toLowerCase()) + (v ? "?v=" + v : "");
  }
  // the site's badge (same look as the Recordings rows: .prov.p-<site>)
  const badge = (p) => `<span class="prov p-${esc(lc(p) || "vod")}">${esc(String(p || "VOD").toUpperCase())}</span>`;
  const ic = (name) => typeof icon === "function" ? icon(name) : "";
  // a link of any site goes through /resolve (which says vod / channel / name / unknown); plain words are a name
  const isLink = (t) => !/\s/.test(t) && /^(https?:\/\/)?([a-z0-9-]+\.)+[a-z]{2,}(:\d+)?(\/\S*)?$/i.test(t);
  const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;

  function note(msg, kind) {
    const n = $("vb-note"); if (!n) return;
    n.textContent = msg || ""; n.className = "vbnote" + (kind ? " " + kind : "");
    n.title = msg || "";
  }
  function loadSites() {
    if (S.sitesLoaded) return; S.sitesLoaded = true;
    try {
      const v = JSON.parse(localStorage.getItem(LS_KEY) || "null");
      if (v && Array.isArray(v.sites)) { const on = v.sites.filter(s => SITES.includes(s)); if (on.length) S.sites = new Set(on); }
    } catch (e) { /* storage blocked / junk: all sites on */ }
  }
  function saveSites() { try { localStorage.setItem(LS_KEY, JSON.stringify({ sites: [...S.sites] })); } catch (e) { } }

  // a date the site gave only as a day (upload_date, YouTube's 'N days ago') arrives as 00:00 UTC and
  // date_approx: show the day, not a made-up time
  function vodDate(v) {
    const ts = v.start_utc; if (!ts) return "";
    if (v.date_approx || ts % 86400 === 0) return new Date(ts * 1000).toISOString().slice(0, 10);
    return fmtDate(ts);
  }
  const isLive = (v) => !!v.live || v.live_status === "is_live" || v.live_status === "is_upcoming";
  // live / upcoming / still processing (post_live): listed, not importable yet (the server says so in `importable`)
  const notReady = (v) => isLive(v) || v.importable === false;

  /* ---------------- the markup (panel or dialog) ---------------- */
  function skeleton(inDialog) {
    const paste = typeof openVod === "function"
      ? `<button type="button" class="btn sm ghost" id="vb-paste" data-tip="Import one VOD from its link (looks it up first)">${ic("link")}Paste link</button>` : "";
    const close = inDialog ? `<button type="button" class="ib sm" id="vb-x" data-tip="Close (Esc)">${ic("x")}</button>` : "";
    return `<div class="vbp">
      <section class="vbface" id="vb-list">
        <div class="phd"><h2>Stream VODs</h2><span class="sp"></span>${paste}${close}</div>
        <div class="psub vbq">
          <div class="vbsearch">${ic("search")}<input type="text" class="inp sm" id="vb-q" placeholder="Streamer name or a VOD / profile link" autocomplete="off" spellcheck="false"></div>
          <button type="button" class="btn sm" id="vb-go" data-tip="Search by name, or open the streamer of a pasted link (Enter)">Search</button>
        </div>
        <div class="psub"><div class="chips" id="vb-sites">${SITES.map(s => `<button type="button" class="chip" data-site="${s}">${s}</button>`).join("")}</div></div>
        <div class="pbody vbl">
          <div id="vb-res"></div>
          <div class="sect-t vbfh">Favorites<span class="sp"></span><button type="button" class="btn ghost sm" id="vb-check" data-tip="List every favorite again and count the VODs that appeared since the last check">${ic("refresh")}<span>Check for new</span></button></div>
          <div id="vb-favs"></div>
        </div>
      </section>
      <section class="vbface vbc" id="vb-ch" hidden></section>
      <div class="pfoot vbfoot"><span class="vbnote" id="vb-note"></span><span class="sp"></span><span class="faint" id="vb-favfoot"></span></div>
    </div>`;
  }

  function mount(host, inDialog) {
    loadSites();
    if (S.host === host && host.querySelector(".vbp")) return false;     // idempotent: already built here
    // moving between panel and dialog: the old markup goes, the state stays
    if (S.host && S.host !== host) S.host.innerHTML = "";
    S.host = host;
    host.innerHTML = skeleton(inDialog);
    wire(host);
    renderSites(); renderFavs(); renderMain();
    return true;
  }

  function wire(host) {
    const q = $("vb-q");
    q.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); go(); }
      else if (e.key === "Escape" && q.value) { e.preventDefault(); e.stopPropagation(); q.value = ""; syncGo(); }
    });
    q.addEventListener("paste", () => setTimeout(() => { if (isLink(q.value.trim())) go(); }, 0));
    q.addEventListener("input", syncGo);
    $("vb-go").onclick = () => go();
    const paste = $("vb-paste"); if (paste) paste.onclick = () => openVod();
    const x = $("vb-x"); if (x) x.onclick = () => { const d = $("dlg-vb"); if (d) d.close(); };
    $("vb-check").onclick = refreshFavs;
    $("vb-sites").addEventListener("click", (e) => {
      const c = e.target.closest(".chip"); if (!c) return;
      const s = c.dataset.site;
      if (S.sites.has(s)) { if (S.sites.size === 1) { note("at least one site stays on"); return; } S.sites.delete(s); } else S.sites.add(s);
      saveSites(); renderSites();
    });
    $("vb-favs").addEventListener("click", (e) => {
      const rm = e.target.closest("[data-rm]");
      if (rm) { e.stopPropagation(); removeFav(+rm.dataset.rm, rm); return; }
      const row = e.target.closest("[data-fi]"); if (!row) return;
      const f = S.favs[+row.dataset.fi]; if (!f) return;
      openChannel({ provider: f.provider, key: favKey(f), name: favName(f), url: favUrl(f) });
    });
    $("vb-favs").addEventListener("keydown", (e) => {
      if ((e.key === "Enter" || e.key === " ") && e.target.matches("[data-fi]")) { e.preventDefault(); e.target.click(); }
    });
    $("vb-res").addEventListener("click", onActClick);
    $("vb-res").addEventListener("keydown", (e) => {
      if ((e.key === "Enter" || e.key === " ") && e.target.matches(".vbres[data-act]")) { e.preventDefault(); e.target.click(); }
    });
    const ch = $("vb-ch");
    ch.addEventListener("click", onActClick);
    ch.addEventListener("change", (e) => { if (e.target.matches("input[type=checkbox][data-u]")) onTick(e.target); });
  }

  // the Search button is off while the very text in the box is being answered (a double click / Enter
  // must not run the same yt-dlp listings twice); a different text may always go -- the newer answer wins
  function syncGo() {
    const b = $("vb-go"); if (!b) return;
    b.disabled = !!S.inflight && $("vb-q").value.trim() === S.inflight;
  }
  function begin(text, msg) {
    const tok = ++S.viewTok;
    S.inflight = text || ""; syncGo();
    if (msg != null) { S.view = "busy"; S.busyMsg = msg; renderMain(); }
    return tok;
  }
  function end(tok) { if (tok === S.viewTok) { S.inflight = ""; syncGo(); } }
  function fail(tok, msg) { if (tok !== S.viewTok) return; S.view = "error"; S.errMsg = msg; renderMain(); }

  function renderSites() {
    document.querySelectorAll("#vb-sites .chip").forEach(c => {
      const on = S.sites.has(c.dataset.site);
      c.classList.toggle("on", on); c.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }

  /* ---------------- search / links ---------------- */
  function go(text) {
    const t = String(text ?? $("vb-q").value).trim();
    if (!t) { note("type a streamer name or paste a VOD / profile link"); $("vb-q").focus(); return; }
    if (S.inflight && t === S.inflight) return;
    return isLink(t) ? resolveLink(t) : search(t);
  }

  async function search(q) {
    const name = q.replace(/^@+/, "").trim();          // '@examplestreamer' is a handle: the server adds the '@' per site
    const sites = SITES.filter(s => S.sites.has(s));
    if (!name) { note("type a streamer name"); return; }
    const tok = begin(q, `Asking ${sites.join(", ")} for "${name}" -- every site at once, a few seconds`);
    note("");
    try {
      const r = await api(`/api/vodbrowse/search?q=${encodeURIComponent(name)}&providers=${encodeURIComponent(sites.join(","))}`);
      if (tok !== S.viewTok) return;
      const rows = asList(r, "results");
      // hits first, site order kept inside each group
      S.results = rows.filter(x => x && x.ok !== false).concat(rows.filter(x => x && x.ok === false));
      S.q = name; S.view = "search"; renderMain();
      const hits = S.results.filter(x => x.ok !== false).length;
      note(hits ? `${plural(hits, "streamer")} found` : `no streamer called "${name}" on ${sites.join(", ")}`);
    } catch (err) { fail(tok, err.message); }
    finally { end(tok); }
  }

  async function resolveLink(t) {
    const tok = begin(t, "Reading the link -- a VOD link asks the site whose it is, ~3 s");
    note("");
    let r;
    try { r = await post("/api/vodbrowse/resolve", { text: t }); }
    catch (err) { fail(tok, err.message); end(tok); return; }
    end(tok);
    if (tok !== S.viewTok) return;
    const kind = r && r.kind;
    if (kind === "name") return search(t);
    if (kind === "channel" && r.provider && r.key) return openChannel({ provider: r.provider, key: r.key, name: r.name, url: r.url });
    if (kind === "vod") {
      const m = r.meta || r.info || {};
      const al = r.already || m.already;
      // r.error = the site's inspect failed (live now, members only, removed): say so and never tick it
      const pre = { url: r.url || t, pasted: t, id: r.id ?? r.vod_id ?? m.id ?? null, title: r.title || m.title || "",
        imported: !!al, rec_id: (al && typeof al === "object") ? (al.rec_id ?? null) : null, error: r.error || "" };
      const ch = r.channel;
      if (pre.error) note(pre.error, "warn");
      if (ch && ch.provider && ch.key) return openChannel(ch, { preselect: pre });
      S.single = { provider: r.provider || m.provider || "", url: pre.url, title: pre.title, error: r.error || "", state: null, busy: false };
      S.view = "single"; renderMain();
      return;
    }
    fail(tok, (r && r.error) || "Not a link of a supported site -- YouTube, Twitch, Kick, TikTok and Rumble links work (a VOD or the streamer's page).");
  }

  /* ---------------- favorites ---------------- */
  async function loadFavs() {
    const tok = ++S.favTok;
    try {
      const r = await api("/api/vodbrowse/favorites");
      if (tok !== S.favTok) return;
      S.favs = asList(r, "favorites"); S.favErr = ""; S.favLoaded = Date.now();
    } catch (err) { if (tok !== S.favTok) return; S.favErr = err.message; }
    renderFavs(); syncStar();
    if (S.view === "search") renderMain();          // the result rows' favorite marks
  }

  async function refreshFavs() {
    if (S.favBusy) return;
    if (!S.favs.length) { note("no favorites yet: open a streamer and click the star"); return; }
    S.favBusy = true; renderFavs();
    const tok = ++S.refreshTok;
    note(`checking ${plural(S.favs.length, "favorite")} for new VODs ...`);
    try {
      const r = await post("/api/vodbrowse/favorites/refresh");
      if (tok !== S.refreshTok) return;
      const list = asList(r, "favorites");
      // the counts are kept apart from the list: a plain reload of the list (star / remove) must not lose them
      S.newCounts = {}; S.favErrors = {}; S.newIds = {};
      list.forEach(f => {
        const k = chKey(f.provider, favKey(f));
        S.newCounts[k] = +f.new_count || 0;
        if (f.error) S.favErrors[k] = f.error;
        if (Array.isArray(f.new_ids) && f.new_ids.length) S.newIds[k] = new Set(f.new_ids.map(String));
      });
      // the list itself is NOT taken from this answer: it is the favorites as they were when the check began
      // (a star / remove meanwhile would come back) -- the plain reload below gives today's list, the counts
      // above stay keyed by streamer
      const n = list.reduce((a, f) => a + (+f.new_count || 0), 0);
      const bad = list.filter(f => f.error).length;
      const msg = (n ? `${plural(n, "new VOD")} across your favorites` : "nothing new since the last check") + (bad ? ` (${bad} could not be checked)` : "");
      note(msg, bad ? "warn" : "");
      toast(msg, 3000);
    } catch (err) { if (tok === S.refreshTok) note(err.message, "bad"); }
    finally { S.favBusy = false; renderFavs(); }
    await loadFavs();                             // names / last_checked_utc the check wrote
    if (S.view === "channel") renderMain();       // the "new" chips of the streamer on screen
  }

  async function removeFav(i, btn) {
    const f = S.favs[i]; if (!f || f.id == null) return;
    btn.disabled = true;
    try {
      await del(`/api/vodbrowse/favorites/${encodeURIComponent(f.id)}`);
      note(`${favName(f)} removed from favorites`);
    } catch (err) { note(err.message, "bad"); btn.disabled = false; return; }
    await loadFavs();
    if (S.view === "channel") renderMain();
  }

  // the star follows the favorites list (the source of truth once it is loaded)
  function syncStar() {
    const ch = S.ch; if (!ch) return;
    const f = chFav(ch);
    if (f) { ch.favorite = true; ch.favorite_id = f.id; }
    else if (!S.favErr) { ch.favorite = false; ch.favorite_id = null; }
  }

  async function toggleStar() {
    const ch = S.ch; if (!ch || ch.starBusy) return;
    ch.starBusy = true; renderMain();
    try {
      const f = chFav(ch);
      const id = f ? f.id : ch.favorite_id;
      if (ch.favorite && id != null) {
        await del(`/api/vodbrowse/favorites/${encodeURIComponent(id)}`);
        note(`${ch.name} removed from favorites`);
      } else {
        await post("/api/vodbrowse/favorites", { provider: ch.provider, key: ch.key, name: ch.name || ch.key, url: ch.url || "" });
        note(`${ch.name} added to favorites`);
      }
    } catch (err) { note(err.message, "bad"); }   // e.g. 409: it was a favorite already -- the reload below shows it
    ch.starBusy = false;
    await loadFavs();
    if (S.ch === ch) renderMain();
  }

  function renderFavs() {
    const el = $("vb-favs"); if (!el) return;
    const b = $("vb-check");
    if (b) { b.disabled = S.favBusy; const t = b.querySelector("span"); if (t) t.textContent = S.favBusy ? "Checking..." : "Check for new"; b.classList.toggle("busy", S.favBusy); }
    if (S.favErr && !S.favs.length) { el.innerHTML = `<div class="vberr">${esc(S.favErr)}</div>`; }
    else if (!S.favs.length) { el.innerHTML = `<div class="empty vbempty"><b>No favorites yet</b>Open a streamer and click the star to keep them here.</div>`; }
    else {
      const cur = S.view === "channel" ? S.ch : null;
      el.innerHTML = S.favs.map((f, i) => {
        const k = chKey(f.provider, favKey(f));
        const n = f.new_count != null ? +f.new_count : (S.newCounts[k] || 0);
        const er = f.error || S.favErrors[k];
        const sub = er ? `<span class="err">${esc(er)}</span>` : f.note ? `<span class="fnote">${esc(f.note)}</span>` : lc(favKey(f)) !== lc(favName(f)) ? `<span class="mono faint">${esc(favKey(f))}</span>` : "";
        return `<div class="row vbf${isCh(cur, f.provider, favKey(f)) ? " sel" : ""}" data-fi="${i}" tabindex="0" title="${esc(favUrl(f) || favKey(f))}">
          <div class="main"><div class="l1">${badge(f.provider)}<span class="t">${esc(favName(f))}</span></div>${sub ? `<div class="l2">${sub}</div>` : ""}</div>
          ${n ? `<span class="badge acc" title="VODs that appeared since the check before">${n} new</span>` : ""}
          <button type="button" class="ib sm vbrm" data-rm="${i}" title="Remove from favorites" aria-label="Remove ${esc(favName(f))} from favorites">${ic("x")}</button></div>`;
      }).join("") + (S.favErr ? `<div class="vberr">${esc(S.favErr)}</div>` : "");
    }
    const last = S.favs.reduce((a, f) => Math.max(a, +f.last_checked_utc || 0), 0);
    const foot = $("vb-favfoot"); if (foot) foot.textContent = last ? `checked ${fmtAgo(last)}` : "";
  }

  /* ---------------- one streamer ---------------- */
  // ch = {provider, key (canonical once listed), alias: Set(keys it was opened by), answered, name, url, vods, error,
  //       loading, limit, favorite, favorite_id, fromSearch, checked: Set(url), rows: {url: {cls, text, title, done}},
  //       pre, importing, starBusy}
  async function openChannel(c, opts = {}) {
    if (!c || !c.provider || !c.key) return;
    const same = isCh(S.ch, c.provider, c.key);
    const fromSearch = S.view === "search" || (same && S.ch.fromSearch);
    if (!same) {
      S.ch = { provider: c.provider, key: c.key, alias: new Set(), answered: false, name: c.name || c.key, url: httpUrl(c.url),
        vods: [], error: "", loading: true, limit: PAGE, favorite: false, favorite_id: null, fromSearch, checked: new Set(),
        rows: {}, pre: null, importing: false, starBusy: false };
    }
    const ch = S.ch;
    if (opts.preselect) ch.pre = Object.assign({ applied: false }, opts.preselect);
    if (opts.limit) ch.limit = Math.min(opts.limit, MAX_LIST);
    ch.fromSearch = fromSearch;
    ch.loading = true; ch.error = "";
    syncStar();
    const tok = begin("");                       // no busy page: the header shows at once, the list says "listing"
    S.view = "channel"; renderMain(); renderFavs();
    try {
      const r = await api(`/api/vodbrowse/channel?provider=${encodeURIComponent(ch.provider)}&key=${encodeURIComponent(ch.key)}&limit=${ch.limit}&refresh=${opts.refresh ? 1 : 0}`);
      if (tok !== S.viewTok || S.ch !== ch) return;     // stale: checked against the view / object, not the key
      const info = (r && r.channel) || {};
      // the server's canonical key ('@handle' for a 'channel/UC...' open): adopt it, so the star, the favorites
      // and the "new" chips all speak of one streamer; the old key stays an alias for lookups
      if (info.key && (!info.provider || info.provider === ch.provider) && chKey(ch.provider, info.key) !== chKey(ch.provider, ch.key)) {
        ch.alias.add(ch.key); ch.key = String(info.key);
      }
      ch.answered = true;
      ch.name = info.name || ch.name; ch.url = httpUrl(info.url) || ch.url;
      ch.vods = asList(r, "vods").filter(v => v && v.url);
      ch.error = (r && r.error) || "";
      if (r && r.favorite != null) { ch.favorite = !!r.favorite; ch.favorite_id = r.favorite_id ?? ch.favorite_id; }
      syncStar();
      applyPreselect(ch);
    } catch (err) {
      if (tok !== S.viewTok || S.ch !== ch) return;
      ch.error = err.message;
    } finally {
      end(tok);
      // a superseded answer leaves the flag alone: a newer listing of this streamer may still be running
      if (tok === S.viewTok && S.ch === ch) { ch.loading = false; if (S.view === "channel") renderMain(); }
    }
  }

  // a pasted VOD link: tick it in the list, or -- older than the listing reaches -- show it on top of it
  function applyPreselect(ch) {
    const p = ch.pre; if (!p) return;
    const pid = p.id != null && p.id !== "" ? String(p.id) : "";
    // links differ in www. / m., tracking queries, a trailing slash, the host's case: compare url_key forms
    const want = new Set([urlKey(p.url), urlKey(p.pasted)].filter(Boolean));
    const hit = ch.vods.find(v => (pid && v.id != null && String(v.id) === pid) || want.has(urlKey(v.url)));
    let v = hit;
    if (!hit) {
      v = { id: p.id, url: p.url, title: p.title || p.error || p.url, imported: p.imported, rec_id: p.rec_id, _extra: true };
      // inspect failed (live, members only, removed): listed so you see it, never importable from here
      if (p.error) { v.importable = false; v.error = p.error; }
      ch.vods.unshift(v);
    } else if (p.imported && !v.imported) {
      // the import dialog / inspect knew it is in the library even if this listing's marks missed it
      v.imported = true; if (v.rec_id == null && p.rec_id != null) v.rec_id = p.rec_id;
    }
    v._pre = true;
    if (!p.applied) {
      p.applied = true;
      if (!p.error && !p.imported && importable(ch, v)) ch.checked.add(v.url);
      if (p.error) note(p.error + (hit ? "" : " -- shown on top of the list, it cannot be imported now"), "warn");
      else if (v.imported) note("that VOD is in the library already");
      else if (!hit) note("that VOD is older than the recent list: it is shown on top");
    }
  }

  const importable = (ch, v) => !v.imported && !v.queued && !notReady(v) && !(ch.rows[v.url] && ch.rows[v.url].done);
  const selected = (ch) => ch.vods.filter(v => ch.checked.has(v.url) && importable(ch, v));

  function onTick(cb) {
    const ch = S.ch; if (!ch) return;
    const url = cb.dataset.u;
    if (cb.checked) ch.checked.add(url); else ch.checked.delete(url);
    const row = cb.closest(".vbv"); if (row) row.classList.toggle("on", cb.checked);
    syncImportBtn();
  }
  function syncImportBtn() {
    const ch = S.ch; const b = $("vb-import"); if (!ch || !b) return;
    const n = selected(ch).length;
    const t = b.querySelector("span");
    const label = ch.importing ? "Importing..." : n ? `Import ${n}` : "Import";
    if (t) t.textContent = label; else b.textContent = label;
    b.title = ch.importing ? "" : n ? `queue ${plural(n, "download")}` : "tick the VODs to import";
    b.disabled = ch.importing || !n;
    const none = $("vb-none"); if (none) none.disabled = !ch.checked.size || ch.importing;
  }

  async function doImport(items, ch) {
    // ch = the streamer view the items belong to (null for a lone VOD link); its rows get the result chips
    if (!items.length) return null;
    note(`queueing ${plural(items.length, "VOD")} -- each link is checked with the site first ...`);
    let res;
    try { res = asList(await post("/api/vodbrowse/import", { items: items.map(v => ({ url: v.url })) }), "results"); }
    catch (err) { note(err.message, "bad"); toast(err.message, 5000); return null; }
    let queued = 0, already = 0, failed = 0;
    const out = items.map((v, i) => {
      const x = res.find(z => z && z.url === v.url) || res[i] || { ok: false, error: "no answer for this link" };
      let st;
      if (x.already) {
        already++; st = { cls: "ok", text: "in library", title: typeof x.already === "object" && x.already.name ? x.already.name : "imported before", done: true };
        v.imported = true; if (typeof x.already === "object" && x.already.rec_id != null) v.rec_id = x.already.rec_id;
      } else if (x.ok && x.already_queued) {
        already++; st = { cls: "warn", text: "already queued", title: x.job != null ? `job ${x.job}` : "", done: true }; v.queued = true;
      } else if (x.ok) {
        queued++; st = { cls: "acc", text: "queued", title: x.job != null ? `download job ${x.job}` : "", done: true }; v.queued = true;
      } else {
        failed++; st = { cls: "bad", text: "failed", title: x.error || "failed", done: false };
      }
      if (ch) { ch.rows[v.url] = st; if (st.done) ch.checked.delete(v.url); }
      return st;
    });
    const msg = [queued && `${queued} queued`, already && `${already} already there`, failed && `${failed} failed (hover the red chip)`].filter(Boolean).join(", ");
    note(msg + (queued ? " -- Jobs shows the downloads" : ""), failed ? "bad" : "");
    toast(`VODs: ${msg}`, 4000);
    if (queued && typeof ST === "object" && ST && ST.tab === "jobs" && typeof loadJobs === "function") loadJobs();
    return out;
  }

  async function importSelected() {
    const ch = S.ch; if (!ch || ch.importing) return;
    const items = selected(ch); if (!items.length) return;
    ch.importing = true; renderMain();
    try { await doImport(items, ch); }
    finally { ch.importing = false; if (S.ch === ch && S.view === "channel") renderMain(); }
  }

  async function importSingle() {
    const s = S.single; if (!s || s.busy) return;
    s.busy = true; renderMain();
    try { const out = await doImport([{ url: s.url }], null); if (out) s.state = out[0]; }
    finally { s.busy = false; if (S.single === s && S.view === "single") renderMain(); }
  }

  function openRecording(id) {
    if (id == null) return;
    const d = $("dlg-vb"); if (d && d.open) d.close();
    if (typeof openPanel === "function") openPanel("rec");
    else if (typeof showTab === "function") showTab("rec");
    if (typeof select === "function") select(+id);
  }

  // the list face's "back" target: the search results if there were any, else the start
  function backToList() {
    S.view = S.results.length || S.q ? "search" : "home";
    renderMain(); renderFavs();
    const q = $("vb-q"); if (q && S.q && !q.value) q.value = S.q;
  }

  function onActClick(e) {
    const a = e.target.closest("[data-act]"); if (!a || a.disabled) return;
    const act = a.dataset.act; const ch = S.ch;
    if (act === "res") { const r = S.results[+a.dataset.i]; if (r && r.ok !== false && r.key) openChannel({ provider: r.provider, key: r.key, name: r.name, url: r.url }); }
    else if (act === "back") backToList();
    else if (act === "star") toggleStar();
    else if (act === "refresh" && ch && !ch.loading) openChannel(ch, { refresh: true });
    else if (act === "more" && ch && !ch.loading && ch.limit < MAX_LIST) openChannel(ch, { limit: Math.min(ch.limit + PAGE, MAX_LIST) });
    else if (act === "allnew" && ch) { ch.vods.forEach(v => { if (importable(ch, v)) ch.checked.add(v.url); }); renderMain(); }
    else if (act === "none" && ch) { ch.checked.clear(); renderMain(); }
    else if (act === "import") importSelected();
    else if (act === "import1") importSingle();
    else if (act === "openrec") { e.preventDefault(); e.stopPropagation(); openRecording(a.dataset.rec); }
    else if (act === "clear") { S.view = "home"; S.results = []; S.q = ""; S.single = null; renderMain(); }
  }

  /* ---------------- rendering ---------------- */
  function renderMain() {
    const listFace = $("vb-list"), chFace = $("vb-ch"); if (!listFace || !chFace) return;
    const onCh = S.view === "channel" && !!S.ch;
    if (onCh) {
      const list = chFace.querySelector(".vblist");
      const keyNow = chKey(S.ch.provider, S.ch.key);
      const keep = list && !chFace.hidden && chFace.dataset.ch === keyNow ? list.scrollTop : 0;
      chFace.dataset.ch = keyNow;
      chFace.innerHTML = channelHtml(S.ch);
      chFace.querySelectorAll("img").forEach(img => { img.onerror = () => img.remove(); });
      const nl = chFace.querySelector(".vblist"); if (nl && keep) nl.scrollTop = keep;
      syncImportBtn();
    } else {
      chFace.innerHTML = ""; chFace.dataset.ch = "";
      const el = $("vb-res");
      if (S.view === "busy") el.innerHTML = `<div class="vbwait"><span class="spin sm"></span><span>${esc(S.busyMsg)}</span></div>`;
      else if (S.view === "error") el.innerHTML = `<div class="vberr">${esc(S.errMsg)}</div>`;
      else if (S.view === "search") el.innerHTML = searchHtml();
      else if (S.view === "single" && S.single) el.innerHTML = singleHtml(S.single);
      else el.innerHTML = homeHtml();
    }
    listFace.hidden = onCh; chFace.hidden = !onCh;
    if (typeof hydrateIcons === "function") hydrateIcons(S.host || document);
  }

  function homeHtml() {
    // a short how-to while there is nothing else to look at; it shrinks to one line once favorites exist
    if (S.favs.length) return `<p class="hint vbhow">Type a name and press Enter, or paste a VOD / profile link.</p>`;
    return `<div class="vbhow">
      <p><b>Find a streamer</b> -- type their name and press Enter; every site that is on is asked at once.</p>
      <p><b>Or paste a link</b> -- a streamer's page lists their recent VODs; a VOD link opens its streamer with that VOD ticked.</p>
      <p>Tick VODs and <b>Import</b>: they download one after the other (Jobs), then get transcribed and scanned like a recording.</p>
    </div>`;
  }

  function searchHtml() {
    const rows = S.results;
    const hits = rows.filter(r => r.ok !== false).length;
    const head = `<div class="sect-t">Results for "${esc(S.q)}"<span class="sp"></span><button type="button" class="ib sm" data-act="clear" title="Clear the results" aria-label="Clear the results">${ic("x")}</button></div>`;
    if (!rows.length) return head + `<div class="empty vbempty"><b>Nothing found</b>Check the spelling, turn more sites on, or paste their page's link.</div>`;
    return head + (hits ? "" : `<p class="hint vbhow">No streamer by that name on the sites that are on.</p>`) + rows.map((r, i) => {
      const fav = r.key && favOf(r.provider, r.key);
      if (r.ok === false) {
        return `<div class="row vbres off">${badge(r.provider)}<div class="main"><div class="l2">${esc(r.error || "not found")}</div></div></div>`;
      }
      const titles = asList(r.sample, "titles").slice(0, 3);
      const n = r.n != null ? +r.n : titles.length;
      return `<div class="row vbres" data-act="res" data-i="${i}" tabindex="0" title="${esc(r.url || r.key || "")}">
        <div class="main"><div class="l1">${badge(r.provider)}<span class="t">${esc(r.name || r.key)}</span>${fav ? `<span class="vbfavmark" title="a favorite">${ic("star")}</span>` : ""}</div>
          <div class="l2"><span class="mono faint vbkey">${esc(r.key || "")}</span>${n ? `<span class="faint">${n}${n >= 3 ? "+" : ""} recent</span>` : ""}</div>
          ${titles.length ? `<div class="vbsamples">${titles.slice(0, 2).map(t => `<span>${esc(t)}</span>`).join("")}</div>` : `<div class="vbsamples"><span class="faint">no recent VODs listed</span></div>`}</div>
        <span class="vbgo">${ic("chev")}</span></div>`;
    }).join("");
  }

  function vodRow(ch, v) {
    const url = v.url; const live = isLive(v); const st = ch.rows[url];
    const dis = !importable(ch, v) || ch.importing;
    const on = ch.checked.has(url) && importable(ch, v);
    const chips = [];
    if (v._pre) chips.push(`<span class="badge acc" title="the link you pasted">your link</span>`);
    if (v.imported) chips.push(v.rec_id != null
      ? `<button type="button" class="badge ok vbopen" data-act="openrec" data-rec="${esc(v.rec_id)}" title="open the recording">in library</button>`
      : `<span class="badge ok">in library</span>`);
    else if (v.queued && !st) chips.push(`<span class="badge warn" title="${v.job_id != null ? `job ${esc(v.job_id)}` : ""}">queued</span>`);
    if (live) chips.push(`<span class="badge bad" title="can be imported once the stream has ended">${v.live_status === "is_upcoming" ? "upcoming" : "live now"}</span>`);
    else if (v.error && !v.imported && !v.queued) chips.push(`<span class="badge bad" title="${esc(v.error)}">unavailable</span>`);
    else if (v.importable === false && !v.imported && !v.queued) chips.push(`<span class="badge warn" title="${esc(v.live_status === "post_live" ? "the stream just ended and the site is still processing it" : "not importable yet")}">not ready</span>`);
    const fresh = chNewIds(ch);
    if (fresh && v.id != null && fresh.has(String(v.id)) && importable(ch, v)) chips.push(`<span class="badge acc" title="appeared since the check before the last one">new</span>`);
    if (st && !(v.imported && st.text === "in library")) chips.push(`<span class="badge ${st.cls}" title="${esc(st.title)}">${esc(st.text)}</span>`);
    const meta = [vodDate(v), v.duration_s ? fmtDur(v.duration_s) : "", v._extra ? "older than the list" : ""].filter(Boolean);
    const thumb = httpUrl(v.thumbnail);
    const link = httpUrl(url);
    return `<label class="vbv${on ? " on" : ""}${dis ? " dis" : ""}${v._pre ? " pre" : ""}">
      <input type="checkbox" data-u="${esc(url)}"${on ? " checked" : ""}${dis ? " disabled" : ""} aria-label="${esc(v.title || url)}">
      <span class="th">${thumb ? `<img src="${esc(thumb)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : ""}</span>
      <span class="tx"><span class="t" title="${esc(v.error || v.title || url)}">${esc(v.title || v.id || url)}</span>
        <span class="m"><span class="num">${meta.map(esc).join(" &middot; ") || "&nbsp;"}</span>${chips.join("")}
          ${link ? `<a class="lk" href="${esc(link)}" target="_blank" rel="noopener noreferrer" title="open on ${esc(ch.provider)}">${ic("ext")}</a>` : ""}</span></span>
    </label>`;
  }

  function channelHtml(ch) {
    const nNew = ch.vods.filter(v => importable(ch, v)).length;
    const nLib = ch.vods.filter(v => v.imported).length;
    const page = httpUrl(ch.url);
    const starOff = ch.starBusy || (ch.loading && !ch.answered);
    const head = `<div class="phd vbchh">
      <button type="button" class="ib sm vbback" data-act="back" title="${S.results.length ? "Back to the results" : "Back to favorites"}" aria-label="Back">${ic("chev")}</button>
      ${badge(ch.provider)}<h2 class="vbname" title="${esc(ch.name || ch.key)}">${esc(ch.name || ch.key)}</h2><span class="sp"></span>
      <button type="button" class="ib sm vbstar${ch.favorite ? " on" : ""}" data-act="star"${starOff ? " disabled" : ""} title="${ch.loading && !ch.answered ? "wait for the listing (it tells the streamer's own key)" : ch.favorite ? "Remove from favorites" : "Add to favorites"}" aria-pressed="${ch.favorite ? "true" : "false"}">${ic("star")}</button>
      <button type="button" class="ib sm${ch.loading ? " busy" : ""}" data-act="refresh"${ch.loading ? " disabled" : ""} title="${ch.loading ? "Listing..." : "List again from the site (the list is cached 5 minutes)"}" aria-label="Refresh">${ic("refresh")}</button>
      ${page ? `<a class="ib sm" href="${esc(page)}" target="_blank" rel="noopener noreferrer" title="their page: ${esc(page)}" aria-label="their page">${ic("ext")}</a>` : ""}
    </div>`;
    const counts = ch.loading && !ch.vods.length ? "listing ..." : `${ch.vods.length} listed &middot; ${nNew} new${nLib ? ` &middot; ${nLib} in library` : ""}`;
    const sub = `<div class="psub vbchsub"><span class="mono faint vbkey" title="${esc(ch.key)}">${esc(ch.key)}</span><span class="sp"></span><span class="faint">${counts}</span></div>`;
    const tools = `<div class="psub vbtools">
      <button type="button" class="btn ghost sm" data-act="allnew"${!nNew || ch.importing ? " disabled" : ""} title="tick every VOD that is not in the library or queued yet">Select new</button>
      <button type="button" class="btn ghost sm" id="vb-none" data-act="none"${!ch.checked.size || ch.importing ? " disabled" : ""}>None</button>
      <span class="sp"></span>
      <button type="button" class="btn pri sm" id="vb-import" data-act="import" disabled>${ic("download")}<span>Import</span></button>
    </div>`;
    const err = ch.error ? `<div class="vberr">${esc(ch.error)}</div>` : "";
    let body;
    if (ch.loading && !ch.vods.length) body = `<div class="vbwait"><span class="spin sm"></span><span>Listing ${esc(ch.name || ch.key)}'s VODs on ${esc(ch.provider)} ...</span></div>`;
    else if (!ch.vods.length) body = ch.error ? "" : `<div class="empty vbempty"><b>No VODs listed</b>The streamer may keep no past broadcasts, or they are for subscribers only.</div>`;
    else body = ch.vods.map(v => vodRow(ch, v)).join("")
      + (ch.loading ? `<div class="vbwait"><span class="spin sm"></span><span>listing again ...</span></div>` : "")
      + (!ch.loading && ch.vods.filter(v => !v._extra).length >= ch.limit
        ? (ch.limit < MAX_LIST
          ? `<div class="vbmore"><button type="button" class="btn ghost sm" data-act="more">Load older (${Math.min(ch.limit + PAGE, MAX_LIST)})</button></div>`
          : `<div class="vbmore hint">Only the newest ${MAX_LIST} are listed -- paste an older VOD's link to import it.</div>`)
        : "");
    // a site whose listing has no thumbnails (Kick): no column of empty boxes
    const noThumb = !(ch.vods || []).some(v => httpUrl(v.thumbnail));
    return `${head}${sub}${tools}${err}<div class="pbody vblist${noThumb ? " nothumb" : ""}">${body}</div>`;
  }

  function singleHtml(s) {
    const link = httpUrl(s.url);
    return `<div class="sect-t">One VOD<span class="sp"></span><button type="button" class="ib sm" data-act="clear" title="Clear" aria-label="Clear">${ic("x")}</button></div>
      <div class="vbsingle">
      <div class="t">${esc(s.title || s.url)}</div>
      <dl class="kv"><dt>Site</dt><dd>${badge(s.provider)}</dd>
        <dt>Link</dt><dd class="mono">${link ? `<a href="${esc(link)}" target="_blank" rel="noopener noreferrer">${esc(s.url)}</a>` : esc(s.url)}</dd></dl>
      <p class="hint">${s.error ? `<span class="bad">${esc(s.error)}</span> Import checks the link with the site again.` : "The site did not say whose VOD this is, so there is no streamer to list. This one VOD can still be imported."}</p>
      <div class="vbsrow"><button type="button" class="btn pri sm" data-act="import1"${s.busy || (s.state && s.state.done) ? " disabled" : ""}>${ic("download")}${s.busy ? "Importing..." : "Import this VOD"}</button>
        ${s.state ? `<span class="badge ${s.state.cls}" title="${esc(s.state.title)}">${esc(s.state.text)}</span>` : ""}</div>
    </div>`;
  }

  /* ---------------- where it lives ---------------- */
  const panelEl = () => $("vb-panel");
  const hasPanel = () => !!panelEl() && typeof openPanel === "function";

  // app.js calls this every time the VODs panel is shown: build once, then only refresh the favorites
  function mountPanel(el) {
    el = el || panelEl(); if (!el) return;
    const d = $("dlg-vb"); if (d && d.open) d.close();
    mount(el, false);
    if (Date.now() - S.favLoaded > 15000) loadFavs(); else renderFavs();
  }

  // fallback home: a modal dialog with the same markup (no panel mount in the page)
  function dialogHost() {
    let d = $("dlg-vb");
    if (!d) {
      d = document.createElement("dialog");
      d.id = "dlg-vb"; d.className = "dlg vb";
      d.innerHTML = `<div class="vbdhost"></div>`;
      document.body.appendChild(d);
    }
    return d;
  }

  // show the browser: the side panel when the page has one, else the dialog
  function show() {
    if (hasPanel()) {
      if (window.ST && ST.panelOpen && ST.tab === "vods" && S.host === panelEl()) { mountPanel(panelEl()); return; }
      openPanel("vods");                       // -> showTab -> VB.mountPanel
      if (S.host !== panelEl()) mountPanel(panelEl());
      return;
    }
    const d = dialogHost();
    mount(d.querySelector(".vbdhost"), true);
    d.returnValue = "";
    if (!d.open) d.showModal();
    loadFavs();
  }

  function open(opts = {}) {
    show();
    const o = opts || {};
    if (o.provider && o.key) {
      openChannel({ provider: o.provider, key: o.key, name: o.name, url: o.url },
        o.preselect_url ? { preselect: { url: o.preselect_url, pasted: o.preselect_url, id: o.preselect_id ?? null,
          title: o.preselect_title || "", error: o.preselect_error || "", imported: !!o.already,
          rec_id: (o.already && typeof o.already === "object") ? (o.already.rec_id ?? null) : null } } : {});
      return;
    }
    if (o.query) { $("vb-q").value = String(o.query); go(String(o.query)); }
    else if (o.preselect_url) { $("vb-q").value = String(o.preselect_url); go(String(o.preselect_url)); }
    else { renderMain(); renderFavs(); }       // reopened: the last view as it was
    if (S.view !== "channel") { const q = $("vb-q"); if (q) q.focus(); }
  }

  function resolveAndOpen(text) {
    const t = String(text ?? "").trim();
    open({});
    if (t) { $("vb-q").value = t; go(t); }
  }

  window.VB = { open, resolveAndOpen, mountPanel };
})();
