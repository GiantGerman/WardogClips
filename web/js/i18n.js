/* UI language runtime. Loaded right after /api/i18n/bundle.js (which sets window.__WC_I18N__: the catalogs of the
   active language, its base language and en-US) and BEFORE core.js, so every module can call t() at load time.

   t(key, params)        text of "<namespace>.<key>"; {name} placeholders; a plural value {one, other, few, ...} is
                         picked by params.count through Intl.PluralRules. Lookup: exact (pt-BR) -> base (pt) -> en-US
                         -> the key itself (never blank). A missing key is logged once on the console in a dev run.
   te(key, params)       t() HTML-escaped, for template strings that build innerHTML.
   tHtml(key, params)    a catalog text that holds markup (keys ending in _html): the text is trusted, params escaped.
   translate(root)       applies data-i18n, data-i18n-html, data-i18n-title, data-i18n-placeholder,
                         data-i18n-aria-label, data-i18n-tip (the app's data-tip tooltips), data-i18n-alt
                         (+ data-i18n-params='{"count":2}') inside root (and to root itself).
   srvText(obj, field)   a server text: obj[field + "_i18n"] {code, params} or obj.code / obj.params when the page has
                         that key, else obj[field] (the English text the server sent).
   I18N.onChange(fn)     fn(lang) after a language switch (re-render dynamic content there).
   fmt*                  dates, times, numbers, sizes, durations through Intl with the active locale.
   Pseudo-locale "qps" (?pseudo=1 shows it in the language menu, ?lang=qps turns it on): every en-US text with
   accented letters, in brackets and ~40 % longer, to find overflowing layouts and text that is not translated. */
"use strict";

const I18N = (() => {
  const empty = { lang: "en-US", chain: ["en-US"], catalogs: {}, languages: [], dev: false, source: "en-US" };
  const S = { lang: "en-US", chain: ["en-US"], cats: {}, langs: [], dev: false, pseudo: false, missing: new Set(), listeners: [], cache: {} };
  const qs = new URLSearchParams(location.search);
  const PSEUDO = "qps";
  // Intl has no data for some of our languages: format dates / numbers the way their region does
  const FORMAT_LOCALE = { gn: "es-PY", ay: "es-BO", qu: "es-PE", ht: "fr-HT" };
  const has = (o, k) => o != null && Object.prototype.hasOwnProperty.call(o, k);
  const escH = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function load(b) {
    b = b || empty;
    S.lang = b.lang || "en-US";
    S.chain = Array.isArray(b.chain) && b.chain.length ? b.chain : [S.lang, "en-US"];
    S.cats = b.catalogs || {};
    if (Array.isArray(b.languages) && b.languages.length) S.langs = b.languages;
    S.dev = !!b.dev;
    S.cache = {};
  }
  load(window.__WC_I18N__);
  S.pseudoAllowed = qs.get("pseudo") === "1" || qs.get("lang") === PSEUDO;
  if (qs.get("lang") === PSEUDO) S.pseudo = true;

  function locale() {
    if (S.pseudo) return "en-US";
    const base = S.lang.split("-")[0];
    try { if (Intl.DateTimeFormat.supportedLocalesOf([S.lang]).length) return S.lang; } catch (e) { }
    return FORMAT_LOCALE[base] || "en-US";
  }
  function intl(kind, opts) {
    const k = kind + JSON.stringify(opts || {});
    if (!S.cache[k]) {
      const loc = locale();
      S.cache[k] = kind === "num" ? new Intl.NumberFormat(loc, opts) : kind === "dt" ? new Intl.DateTimeFormat(loc, opts)
        : kind === "plural" ? new Intl.PluralRules(loc, opts) : kind === "rel" ? new Intl.RelativeTimeFormat(loc, opts)
        : new Intl.ListFormat(loc, opts);
    }
    return S.cache[k];
  }

  function raw(key) {
    const chain = S.pseudo ? ["en-US"] : S.chain;
    for (const c of chain) { const cat = S.cats[c]; if (has(cat, key)) return cat[key]; }
    return undefined;
  }
  function miss(key) {
    if (!S.dev || S.missing.has(key)) return;
    S.missing.add(key); console.warn("[i18n] missing key " + key + " lang=" + S.lang);
  }
  function pick(v, p) {
    if (v && typeof v === "object") {
      const n = Number(p && p.count);
      let cat = "other";
      try { cat = intl("plural").select(isNaN(n) ? 0 : n); } catch (e) { }
      return has(v, cat) ? v[cat] : (has(v, "other") ? v.other : "");
    }
    return v;
  }
  // a param that is itself a server text carries "<name>_i18n" {code, params} beside it: shown translated
  function val(k, v, p) {
    const m = p && p[k + "_i18n"];
    if (m && typeof m === "object" && m.code && raw(m.code) !== undefined) return text(m.code, m.params || {}, false);
    return k === "count" && typeof v === "number" ? fmtNum(v) : String(v ?? "");
  }
  function fill(s, p, escape) {
    return String(s).replace(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g, (m, k) => has(p, k) ? (escape ? escH(val(k, p[k], p)) : val(k, p[k], p)) : m);
  }
  // template (catalog text) -> final text
  function text(key, p, escape) {
    const v = raw(key);
    if (v === undefined) { miss(key); return escape ? escH(key) : key; }
    let s = String(pick(v, p) ?? "");
    if (S.pseudo) s = pseudo(s);
    return fill(s, p, escape);
  }

  // ---- pseudo-locale ----
  const PS = { a: "á", b: "ƀ", c: "ç", d: "đ", e: "é", f: "ƒ", g: "ĝ", h: "ĥ", i: "í", j: "ĵ", k: "ķ", l: "ĺ", m: "ɱ", n: "ñ", o: "ö", p: "þ", q: "ǫ", r: "ŕ", s: "š", t: "ţ", u: "ü", v: "ṽ", w: "ŵ", x: "ẋ", y: "ý", z: "ž",
    A: "Å", B: "Ɓ", C: "Ç", D: "Đ", E: "É", F: "Ƒ", G: "Ĝ", H: "Ĥ", I: "Í", J: "Ĵ", K: "Ķ", L: "Ĺ", M: "Ṁ", N: "Ñ", O: "Ö", P: "Þ", Q: "Ǫ", R: "Ŕ", S: "Š", T: "Ţ", U: "Ü", V: "Ṽ", W: "Ŵ", X: "Ẋ", Y: "Ý", Z: "Ž" };
  function pseudo(s) {
    let letters = 0;
    // placeholders, tags and entities stay as they are
    const out = String(s).split(/(\{[A-Za-z_][A-Za-z0-9_]*\}|<[^>]*>|&[#\w]+;)/).map((part, i) => {
      if (i % 2) return part;
      return part.replace(/[A-Za-z]/g, (c) => { letters++; return PS[c] || c; });
    }).join("");
    if (!letters) return out;
    const pad = Math.max(1, Math.ceil(letters * 0.4));
    const words = []; for (let n = pad; n > 0; n -= 6) words.push("~".repeat(Math.min(6, n)));
    return "[" + out + " " + words.join(" ") + "]";
  }

  // ---- DOM ----
  const ATTRS = [["i18nTitle", "title"], ["i18nPlaceholder", "placeholder"], ["i18nAriaLabel", "aria-label"], ["i18nTip", "data-tip"], ["i18nAlt", "alt"]];
  const SEL = "[data-i18n],[data-i18n-html],[data-i18n-title],[data-i18n-placeholder],[data-i18n-aria-label],[data-i18n-tip],[data-i18n-alt]";
  // the element's own text: the first non-blank text node directly inside it (icons and other child elements stay)
  function setText(el, s) {
    let done = false;
    for (const n of Array.from(el.childNodes)) {
      if (n.nodeType !== 3 || !n.nodeValue.trim()) continue;
      if (done) { n.remove(); continue; }
      const m = n.nodeValue.match(/^(\s*)[\s\S]*?(\s*)$/);
      n.nodeValue = m[1] + s + m[2]; done = true;
    }
    if (done) return;
    if (!el.children.length) el.textContent = s;
    else el.appendChild(document.createTextNode(s));
  }
  function apply(el) {
    const d = el.dataset; let p;
    if (d.i18nParams) { try { p = JSON.parse(d.i18nParams); } catch (e) { p = undefined; } }
    if (d.i18n) setText(el, text(d.i18n, p));
    if (d.i18nHtml) el.innerHTML = text(d.i18nHtml, p, true);
    for (const [k, attr] of ATTRS) if (d[k]) el.setAttribute(attr, text(d[k], p));
  }
  function translate(root) {
    root = root || document;
    if (root.nodeType === 1 && root.matches(SEL)) apply(root);
    if (root.querySelectorAll) root.querySelectorAll(SEL).forEach(apply);
    return root;
  }

  async function setLanguage(code) {
    if (code === PSEUDO) { S.pseudo = true; S.pseudoAllowed = true; S.cache = {}; changed(); return S.lang; }
    // post() comes from core.js (loaded after this file; only called once the page runs)
    const r = await post("/api/i18n/language", { language: code || "" });
    S.pseudo = false;
    load(r.bundle);
    changed();
    return S.lang;
  }
  function changed() {
    document.documentElement.lang = S.pseudo ? "en-US" : S.lang;
    translate(document);
    for (const fn of S.listeners.slice()) { try { fn(S.pseudo ? PSEUDO : S.lang); } catch (e) { console.error(e); } }
    document.dispatchEvent(new CustomEvent("wc:language", { detail: { lang: S.pseudo ? PSEUDO : S.lang } }));
  }

  return {
    get lang() { return S.pseudo ? PSEUDO : S.lang; },
    get locale() { return locale(); },
    get languages() { return S.langs.slice(); },
    get pseudo() { return S.pseudo; },
    get pseudoAllowed() { return S.pseudoAllowed; },
    get dev() { return S.dev; },
    PSEUDO,
    t: (key, p) => text(key, p, false),
    te: (key, p) => escH(text(key, p, false)),
    tHtml: (key, p) => text(key, p, true),
    has: (key) => raw(key) !== undefined,
    translate, setLanguage, load,
    onChange(fn) { if (typeof fn === "function" && !S.listeners.includes(fn)) S.listeners.push(fn); },
    offChange(fn) { S.listeners = S.listeners.filter(f => f !== fn); },
    missing: () => Array.from(S.missing),
    intl,
  };
})();

const t = I18N.t, te = I18N.te, tHtml = I18N.tHtml, translate = I18N.translate;

/* A text that came from the server. Shapes: {field: "English", field_i18n: {code, params}} (with_msg), or an object
   {message | detail, code, params} (msg() / an AppError body). The page's translation when it has the key, else the
   English text. */
function srvText(obj, field) {
  if (obj == null) return "";
  if (typeof obj === "string") return obj;
  field = field || "message";
  const meta = obj[field + "_i18n"];
  if (meta && meta.code && I18N.has(meta.code)) return t(meta.code, meta.params || {});
  if (obj.code && I18N.has(obj.code) && (field === "message" || field === "detail")) return t(obj.code, obj.params || {});
  const v = obj[field] ?? obj.message ?? obj.detail;
  return typeof v === "string" ? v : (v && typeof v === "object" ? srvText(v, "message") : "");
}

/* ---------------- locale-aware formatting (the active UI language's conventions; en-US = month day year, 12-hour
   clock with AM / PM). Times are unix seconds (UTC), shown in the PC's time zone. ---------------- */
function fmtNum(n, opts) { if (n == null || isNaN(n)) return "-"; return I18N.intl("num", opts || { maximumFractionDigits: 2 }).format(n); }
function fmtPct(x, digits) { if (x == null || isNaN(x)) return "-"; return I18N.intl("num", { style: "percent", maximumFractionDigits: digits || 0 }).format(x); }
const DT_STYLES = {
  when: { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" },     // Sat, Nov 7, 3:45 PM
  day: { weekday: "short", month: "short", day: "numeric", year: "numeric" },                       // Sat, Nov 7, 2026
  date: { year: "numeric", month: "2-digit", day: "2-digit" },                                       // 11/07/2026
  datetime: { year: "numeric", month: "2-digit", day: "2-digit", hour: "numeric", minute: "2-digit" }, // 11/07/2026, 3:45 PM
  time: { hour: "numeric", minute: "2-digit", second: "2-digit" },                                    // 3:45:07 PM
  hm: { hour: "numeric", minute: "2-digit" },                                                         // 3:45 PM
  monthday: { month: "short", day: "numeric" },                                                      // Nov 7
  long: { weekday: "long", year: "numeric", month: "long", day: "numeric", hour: "numeric", minute: "2-digit" },
};
function fmtDateTime(utc, style) {
  if (utc == null || utc === "" || (typeof utc === "number" && !utc)) return "-";
  const d = utc instanceof Date ? utc : new Date(Number(utc) * 1000);
  if (isNaN(d.getTime())) return "-";
  return I18N.intl("dt", DT_STYLES[style || "datetime"] || style).format(d);
}
function fmtUnit(v, unit, digits, minInt) {
  const o = { style: "unit", unit, unitDisplay: "short", maximumFractionDigits: digits || 0, minimumFractionDigits: digits || 0 };
  if (minInt) o.minimumIntegerDigits = minInt;
  return I18N.intl("num", o).format(v);
}
function fmtSize(b) {
  if (b == null || isNaN(b)) return "-";
  if (b >= 1e12) return fmtUnit(b / 1e12, "terabyte", 2);
  if (b >= 1e9) return fmtUnit(b / 1e9, "gigabyte", 1);
  if (b >= 1e6) return fmtUnit(b / 1e6, "megabyte", 0);
  return fmtUnit(Math.round(b / 1e3), "kilobyte", 0);
}
// 1h 02m 03s style, with the language's unit abbreviations
function fmtDuration(s) {
  if (s == null || isNaN(s)) return "-";
  s = Math.round(s); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const u = (v, unit, minInt) => I18N.intl("num", { style: "unit", unit, unitDisplay: "narrow", minimumIntegerDigits: minInt || 1 }).format(v);
  return (h ? u(h, "hour") + " " : "") + u(m, "minute", h ? 2 : 1) + " " + u(sec, "second", 2);
}
function fmtRelative(utc) {
  if (!utc) return "-";
  const s = Math.max(0, Date.now() / 1000 - utc);
  const rel = I18N.intl("rel", { numeric: "auto", style: "short" });
  if (s < 60) return rel.format(-Math.round(s), "second");
  if (s < 3600) return rel.format(-Math.round(s / 60), "minute");
  if (s < 86400) return rel.format(-Math.round(s / 360) / 10, "hour");
  return rel.format(-Math.round(s / 86400), "day");
}
function fmtList(items, type) { return I18N.intl("list", { style: "long", type: type || "conjunction" }).format((items || []).map(String)); }

// static text of index.html now (the scripts run at the end of <body>, so the whole page is parsed)
document.documentElement.lang = I18N.pseudo ? "en-US" : I18N.lang;
if (I18N.pseudo || I18N.lang !== "en-US") translate(document);
