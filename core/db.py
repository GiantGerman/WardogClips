"""SQLite store for WardogClips (WAL mode, one connection per thread).

Schema is idempotent CREATE IF NOT EXISTS:
- recordings   every footage file the library has seen (identity = name + size, so a file
               moved to another drive keeps its row)
- tracks       per audio track: role (game / voice), speech share
- jobs         the stage queue with state / progress / error
- events       kills, deaths, assists, voice highlights ...
"""

import logging
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger("wclips.db")

_SLOW_SQL_S = 0.25

_SCHEMA = """
CREATE TABLE IF NOT EXISTS recordings (
    id            INTEGER PRIMARY KEY,
    key           TEXT NOT NULL UNIQUE,            -- identity: sha1(name|size)[:16]
    path          TEXT NOT NULL,
    name          TEXT NOT NULL,
    drive         TEXT NOT NULL DEFAULT '',
    kind          TEXT NOT NULL DEFAULT 'recording',  -- replay | recording
    name_utc      REAL,                              -- timestamp in the file name (a replay's = END of the buffer)
    size          INTEGER NOT NULL DEFAULT 0,
    mtime         REAL NOT NULL DEFAULT 0,
    duration_s    REAL,
    fps           REAL,
    width         INTEGER,
    height        INTEGER,
    vcodec        TEXT,
    pix_fmt       TEXT,
    bitrate       INTEGER,
    keyint_s      REAL,
    audio_tracks  TEXT NOT NULL DEFAULT '[]',        -- json [{idx, codec, sample_rate, channels}]
    hud_profile   TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'new',       -- new | probed | error
    missing       INTEGER NOT NULL DEFAULT 0,
    stages        TEXT NOT NULL DEFAULT '{}',        -- json {stage: {state, utc}}
    proxy_path    TEXT,
    error         TEXT,
    created_utc   REAL NOT NULL,
    updated_utc   REAL NOT NULL,
    last_seen_utc REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rec_name_utc ON recordings(name_utc);
CREATE INDEX IF NOT EXISTS idx_rec_status ON recordings(status, missing);

CREATE TABLE IF NOT EXISTS tracks (
    rec_id        INTEGER NOT NULL,
    idx           INTEGER NOT NULL,
    role          TEXT NOT NULL DEFAULT 'unknown',   -- unknown | game | voice | silent
    codec         TEXT,
    sample_rate   INTEGER,
    channels      INTEGER,
    speech_share  REAL,
    rms_mean_db   REAL,
    PRIMARY KEY (rec_id, idx)
);

CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY,
    rec_id        INTEGER,
    stage         TEXT NOT NULL,
    lane          TEXT NOT NULL,
    state         TEXT NOT NULL DEFAULT 'queued',    -- queued | running | done | failed | skipped
    priority      INTEGER NOT NULL DEFAULT 0,
    progress      REAL NOT NULL DEFAULT 0,
    message       TEXT NOT NULL DEFAULT '',
    error         TEXT,
    created_utc   REAL NOT NULL,
    started_utc   REAL,
    finished_utc  REAL
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state, lane, priority, id);
CREATE INDEX IF NOT EXISTS idx_jobs_rec ON jobs(rec_id, stage);

CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY,
    rec_id        INTEGER NOT NULL,
    t_s           REAL NOT NULL,
    t_end_s       REAL,
    kind          TEXT NOT NULL,                     -- kill | assist | death | spotted | voice | manual
    subkind       TEXT NOT NULL DEFAULT '',
    score         REAL NOT NULL DEFAULT 0,
    confidence    REAL NOT NULL DEFAULT 0,
    killer        TEXT,
    victim        TEXT,
    weapon        TEXT,
    amount        INTEGER,
    source        TEXT NOT NULL DEFAULT '{}',        -- json: which signals agreed
    crop_ticker   TEXT,
    crop_feed     TEXT,
    status        TEXT NOT NULL DEFAULT 'new',       -- new | kept | false
    resolve_marker TEXT,
    note          TEXT NOT NULL DEFAULT '',
    created_utc   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_rec ON events(rec_id, t_s);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind, status);

CREATE TABLE IF NOT EXISTS transcript_segments (
    id            INTEGER PRIMARY KEY,
    rec_id        INTEGER NOT NULL,
    t0            REAL NOT NULL,
    t1            REAL NOT NULL,
    text          TEXT NOT NULL,
    words         TEXT NOT NULL DEFAULT '[]',        -- json [{w, t0, t1, p}]
    avg_logprob   REAL,
    no_speech_prob REAL,
    model         TEXT,
    created_utc   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tseg_rec ON transcript_segments(rec_id, t0);

CREATE TABLE IF NOT EXISTS ticker_lines (          -- every reward-ticker line OCR'd (calibration + debugging)
    id            INTEGER PRIMARY KEY,
    rec_id        INTEGER NOT NULL,
    t_s           REAL NOT NULL,
    text          TEXT NOT NULL,
    label         TEXT NOT NULL DEFAULT 'other',
    cash          INTEGER,
    xp            INTEGER,
    conf          REAL,
    created_utc   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ticker_rec ON ticker_lines(rec_id, t_s);

CREATE TABLE IF NOT EXISTS ticker_labels (         -- user rules: raw text pattern -> label (calibration page)
    id            INTEGER PRIMARY KEY,
    pattern       TEXT NOT NULL UNIQUE,
    label         TEXT NOT NULL,
    note          TEXT NOT NULL DEFAULT '',
    created_utc   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS feed_rows (             -- kill-feed rows seen (first sighting)
    id            INTEGER PRIMARY KEY,
    rec_id        INTEGER NOT NULL,
    t_s           REAL NOT NULL,
    killer        TEXT NOT NULL,
    victim        TEXT NOT NULL,
    distance_m    INTEGER,
    role          TEXT NOT NULL DEFAULT '',
    raw           TEXT NOT NULL DEFAULT '',
    conf          REAL,
    created_utc   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feed_rec ON feed_rows(rec_id, t_s);

CREATE TABLE IF NOT EXISTS zone_templates (        -- per-streamer detection zones (core/zones.py), versioned by date
    id             INTEGER PRIMARY KEY,
    provider       TEXT NOT NULL,                    -- YouTube | Twitch | Kick | TikTok | Rumble | OBS
    channel_key    TEXT NOT NULL,                    -- media/vods.channel_identity key ('examplestreamer', '@examplechannel'); '*' = any
    channel_name   TEXT NOT NULL DEFAULT '',
    name           TEXT NOT NULL DEFAULT '',
    effective_from REAL NOT NULL DEFAULT 0,          -- epoch s: applies to recordings starting at / after this
    zones          TEXT NOT NULL DEFAULT '{}',       -- json, normalized 0..1 boxes (see core/zones.py)
    sample_w       INTEGER,                          -- the frame the zones were drawn on
    sample_h       INTEGER,
    sample_rec_id  INTEGER,
    sample_t       REAL,
    sample_jpg     TEXT,                             -- file name under <data>/zones/ (kept for the editor + export)
    note           TEXT NOT NULL DEFAULT '',
    created_utc    REAL NOT NULL,
    updated_utc    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_zone_tpl ON zone_templates(provider, channel_key, effective_from);

CREATE TABLE IF NOT EXISTS vod_favorites (         -- favorite streamers (VOD browser)
    id             INTEGER PRIMARY KEY,
    provider       TEXT NOT NULL,
    channel_key    TEXT NOT NULL,
    channel_name   TEXT NOT NULL DEFAULT '',
    channel_url    TEXT NOT NULL DEFAULT '',
    note           TEXT NOT NULL DEFAULT '',
    last_checked_utc REAL,                           -- when its VOD list was last fetched
    last_seen_ids  TEXT NOT NULL DEFAULT '[]',       -- json: VOD ids seen then (-> "new since" count)
    created_utc    REAL NOT NULL,
    UNIQUE (provider, channel_key)
);

CREATE TABLE IF NOT EXISTS drive_bench (           -- read throughput per library drive (core/drives.py); latest row wins
    id            INTEGER PRIMARY KEY,
    drive         TEXT NOT NULL,                     -- 'D:' / 'E:' / UNC share
    kind          TEXT NOT NULL DEFAULT '',          -- ssd | hdd | network (a guess from the numbers)
    mb_s          TEXT NOT NULL DEFAULT '{}',        -- json {"1": MB/s, "2": .., "4": ..} aggregate per reader count
    best_n        INTEGER NOT NULL DEFAULT 1,        -- largest n whose aggregate gained >= 15 % over the previous
    conc_n        INTEGER NOT NULL DEFAULT 1,        -- largest n that kept >= 90 % of the best aggregate
    cache_bypass  INTEGER NOT NULL DEFAULT 0,        -- 1 = FILE_FLAG_NO_BUFFERING reads
    files         INTEGER NOT NULL DEFAULT 0,
    bytes         INTEGER NOT NULL DEFAULT 0,
    seconds       REAL NOT NULL DEFAULT 0,
    note          TEXT NOT NULL DEFAULT '',
    utc           REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bench_drive ON drive_bench(drive, utc);
"""

# FTS5 is an optional sqlite module; python.org builds have it, some embedded builds do not.
_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS transcript_fts USING fts5(
    text, content='transcript_segments', content_rowid='id', tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS tseg_ai AFTER INSERT ON transcript_segments BEGIN
    INSERT INTO transcript_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS tseg_ad AFTER DELETE ON transcript_segments BEGIN
    INSERT INTO transcript_fts(transcript_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS tseg_au AFTER UPDATE ON transcript_segments BEGIN
    INSERT INTO transcript_fts(transcript_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO transcript_fts(rowid, text) VALUES (new.id, new.text);
END;
"""

# the English translations (transcript_segments.text_en) get their own index; only rows that have one are in it
_FTS_EN = """
CREATE VIRTUAL TABLE IF NOT EXISTS transcript_fts_en USING fts5(
    text_en, content='transcript_segments', content_rowid='id', tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS tseg_en_ai AFTER INSERT ON transcript_segments WHEN COALESCE(new.text_en, '') != '' BEGIN
    INSERT INTO transcript_fts_en(rowid, text_en) VALUES (new.id, new.text_en);
END;
CREATE TRIGGER IF NOT EXISTS tseg_en_ad AFTER DELETE ON transcript_segments WHEN COALESCE(old.text_en, '') != '' BEGIN
    INSERT INTO transcript_fts_en(transcript_fts_en, rowid, text_en) VALUES ('delete', old.id, old.text_en);
END;
CREATE TRIGGER IF NOT EXISTS tseg_en_au AFTER UPDATE OF text_en ON transcript_segments BEGIN
    INSERT INTO transcript_fts_en(transcript_fts_en, rowid, text_en)
        SELECT 'delete', old.id, old.text_en WHERE COALESCE(old.text_en, '') != '';
    INSERT INTO transcript_fts_en(rowid, text_en) SELECT new.id, new.text_en WHERE COALESCE(new.text_en, '') != '';
END;
"""


class DB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._local = threading.local()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = self.connect()
        con.executescript(_SCHEMA)
        self.fts = True
        try:
            con.executescript(_FTS)
        except sqlite3.OperationalError as e:
            self.fts = False
            logger.warning("[DB] FTS5 unavailable (%s) -- transcript search falls back to LIKE", e)
        self._migrate(con)
        logger.info("[DB] open %s fts=%s", self.path, self.fts)

    # columns added after a table first shipped: (table, column, declaration)
    _ADDED_COLUMNS = (
        ("events", "clip_in", "REAL"),      # user-set clip bounds (s); NULL = t_s - pre_roll
        ("events", "clip_out", "REAL"),     # NULL = t_s + post_roll
        ("transcript_segments", "track", "INTEGER"),   # the OBS track (0-based) a line came from
        ("transcript_segments", "speaker", "TEXT"),    # me (mic) | squad (Discord) | '' (unnamed / mixed track)
        ("jobs", "arg", "TEXT"),            # a job's parameters as JSON (download: url / provider / title)
        ("recordings", "title", "TEXT"),    # a stream VOD's title (from its .vod.json sidecar)
        ("recordings", "source_url", "TEXT"),   # ... and the link it was downloaded from
        ("recordings", "channel", "TEXT"),      # a VOD's streamer (display name)
        ("recordings", "channel_key", "TEXT"),  # ... stable key per site (media/vods.channel_identity): '@examplechannel', 'examplestreamer'
        ("recordings", "channel_url", "TEXT"),  # ... their page
        ("recordings", "zone_template_id", "INTEGER"),   # a zone template pinned to this recording (NULL = by streamer)
        ("transcript_segments", "lang", "TEXT"),       # the speech language of the line ('en', 'es', ...)
        ("transcript_segments", "text_en", "TEXT"),    # its English translation (stt.translate), NULL = none
        ("tracks", "lang", "TEXT"),                    # speech language detected on the track
        ("tracks", "lang_prob", "REAL"),               # ... and how sure the detection was (NULL = fixed by setting)
        ("recordings", "stt_language", "TEXT"),        # speech language override for this recording (NULL = Settings)
        ("jobs", "gpu", "TEXT"),            # the graphics card a video job ran on (its name; 'CPU' = none in use)
    )

    def _migrate(self, con: sqlite3.Connection) -> None:
        for table, col, decl in self._ADDED_COLUMNS:
            have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
            if col not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                logger.info("[DB] migrated: %s.%s added", table, col)
        self.fts_en = False
        if self.fts:
            try:
                con.executescript(_FTS_EN)       # after the columns: its triggers name text_en
                self.fts_en = True
            except sqlite3.OperationalError as e:
                logger.warning("[DB] translation index unavailable (%s)", e)

    def connect(self) -> sqlite3.Connection:
        con = getattr(self._local, "con", None)
        if con is None:
            # autocommit mode: single statements commit themselves; multi-statement work
            # uses explicit BEGIN IMMEDIATE ... COMMIT (see jobs.claim)
            con = sqlite3.connect(str(self.path), timeout=10, isolation_level=None, check_same_thread=False)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
            con.execute("PRAGMA busy_timeout=5000")
            self._local.con = con
        return con

    def close_thread(self) -> None:
        con = getattr(self._local, "con", None)
        if con is not None:
            try:
                con.close()
            finally:
                self._local.con = None

    # -- helpers ------------------------------------------------------------------
    def _exec(self, sql: str, params=()):
        t0 = time.monotonic()
        try:
            return self.connect().execute(sql, params)
        finally:
            dt = time.monotonic() - t0
            if dt > _SLOW_SQL_S:
                logger.debug("[DB] slow query %.2fs: %s", dt, sql[:120])

    def execute(self, sql: str, params=()) -> int:
        """Run a write; returns lastrowid (or rowcount for UPDATE / DELETE)."""
        cur = self._exec(sql, params)
        return cur.lastrowid if sql.lstrip()[:6].upper() == "INSERT" else cur.rowcount

    def executemany(self, sql: str, rows) -> None:
        self.connect().executemany(sql, rows)

    def query(self, sql: str, params=()) -> list[dict]:
        return [dict(r) for r in self._exec(sql, params).fetchall()]

    def one(self, sql: str, params=()) -> dict | None:
        r = self._exec(sql, params).fetchone()
        return dict(r) if r is not None else None

    def scalar(self, sql: str, params=()):
        r = self._exec(sql, params).fetchone()
        return r[0] if r is not None else None
