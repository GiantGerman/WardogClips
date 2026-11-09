# Changelog

## 0.3.0 (2026-11-09)

### Make clip
- Make a trimmed clip from any recording without leaving the app: set in and out in the player and save it to the
  clip pool (default `Videos\WardogClips\Clips`, Settings > Clip pool). Every audio track is kept as its own track.
- Two modes: Copy (lossless and instant; starts on the keyframe before the in point) and Frame-exact (starts on the
  exact frame; the picture is re-encoded to match the source).
- The clip pool is never scanned or analyzed: the source recording already was.

### Export for other editors (experimental)
- Besides the Resolve hand-off and EDL: Premiere Pro XML (xmeml) and OpenTimelineIO (.otio), with every audio track
  and the event markers. Not yet tested in Premiere itself.

### Queue
- Move jobs to the top, up, down or to the bottom; select several rows and move them together.
- "Move to top" on a recording moves all its queued steps; "Group by recording" view and a "Next up" marker.

### Graphics cards
- AMD and Intel cards: hardware decoding that works on any card, and AMD (AMF) / Intel (QSV) video encoding.
  Without an NVIDIA card transcription runs on the processor and the NVIDIA speech add-on is not offered.
- Several graphics cards: Settings > Hardware lists them all; pick which ones WardogClips may use.

### Work modes
- Full speed, Balanced (default) and Background. While a game, a full-screen app or an OBS recording runs,
  WardogClips lowers its priority and runs fewer jobs at once; the status bar says when it has slowed down.
- Tray menu: "Pause while I play" and the work mode.

### Speech in other languages
- The language of each audio track is detected on its own; multilingual Whisper models are used when needed.
- Excitement keywords and filters for made-up lines per language; optional translation to English.

### Updates
- Checks GitHub for a new release at startup and every 24 hours, then asks: Update now, Later or Skip this version.
  The download is checked against its sha256 before the installer runs.

### Languages
- The UI can switch language instantly: a language menu in the top bar, in Settings > General and in the Ctrl+K
  palette ("Change language..."). By default it follows the language the installer ran in, else the Windows display
  language. 31 languages of the Americas and the EU are listed; translations other than English are marked beta.
- Dates, times, numbers, sizes and durations follow the chosen language. English is American English: month-day-year
  and a 12-hour clock.
- Until a speech language is picked, the speech language follows the UI language (English when Whisper cannot
  transcribe it).
- The tray menu, the launcher's messages and the installer use the same language; the installer offers every listed
  language.
- Server errors carry a message key, so they show in the UI language.
- American English spelling throughout.

## 0.2.0 (2026-11-07)

The first public release: WardogClips is now a normal Windows program. The scanner itself (detection, transcription,
player, Resolve hand-off, VOD import) is the same as in 0.1.

### Install and launch
- Per-user Inno Setup installer `WardogClips-Setup-0.2.0.exe` (164.6 MB, no admin rights, ~750 MB installed) into
  `%LOCALAPPDATA%\Programs\WardogClips`: embedded Python 3.13.3, the pinned packages, FFmpeg 9.0.2 (gyan.dev
  essentials), the Visual C++ runtime app-local, a Start-menu shortcut and an optional desktop icon.
- `WardogClips.exe` launcher (C#): starts the app with no console window, or reopens the browser when this data
  dir's WardogClips already runs; `--quit` stops it (used by the installer before an upgrade / uninstall). One start
  decision at a time per data dir (named mutex); another WardogClips copy with a different data dir on the same port
  is not mistaken for ours.
- Tray icon (`main.py --tray`, pystray): Open WardogClips, Open data folder, Open log, Quit; a one-time "right-click
  the tray icon to quit" balloon. Logging goes to the file only in tray mode; child processes never flash a console.
- Upgrade over a running copy: the installer asks it to quit first and never mixes old and new program files.
- Uninstall asks whether to delete the data too (default No; a silent uninstall keeps it).

### First-run setup and add-ons
- Setup wizard in the page (Welcome, Footage, Your name, Speech, Streams, Start), in plain language: footage folders
  proposed from the OBS profile, in-game name + squad, speech choice Fast (NVIDIA) / Standard / Light, optional stream
  VODs. "Run setup again" in Settings > Add-ons and the Ctrl+K palette. An existing settings file with library folders or
  an in-game name skips the wizard.
- Add-ons downloaded in the app, only when the user asks (Settings > Add-ons, or the wizard): the speech engine for
  NVIDIA cards (CUDA libraries from NVIDIA's PyPI wheels, ~1.3 GB), the YouTube helper (Deno 2.9.8, ~41 MB), an
  "Update VOD downloader" (a newer yt-dlp) and every Whisper speech model. Resumable, checksum-verified, cancellable,
  only from an allow-list of PyPI / GitHub / Hugging Face hosts (and their download servers); stored in `%LOCALAPPDATA%\WardogClips\packs`, so they survive
  upgrades.
- Status bar: download progress, and a "vX available" link when the optional release check finds a newer version.
- Settings > Updates: release check against GitHub (`updates.repo`, empty by default = never checks; at most once a
  day).

### Data, ports and instances
- Data dir moved to `%LOCALAPPDATA%\WardogClips` (installed and source runs; `WARDOGCLIPS_DATA_DIR` still overrides).
- Port 8558 by default, `WARDOGCLIPS_PORT` overrides; a port held by another program -> the next free one (up to +20),
  recorded in `instance.json`.
- Single instance per data dir: `instance.lock` held for the process life; a second launch opens the running one.
  `main.py --check-running` reports a running copy; `POST /api/system/quit` (tray / launcher / stop script).
- Stream VODs default to `Videos\WardogClips VODs`.
- Settings schema 3: empty library / name / squad defaults, new `setup` and `updates` sections.

### Security
- Request guard against CSRF and DNS rebinding: the Host must be a loopback name, a present Origin must be this
  server, and every state-changing `/api/*` call needs the `X-WardogClips: 1` header (the page, launcher and stop
  script send it).
- The wizard's native folder picker answers requests from this computer only; the folder check needs the header.

### Fixes
- Non-ASCII user names / data dirs: evidence crops are written through Python (`utils/imgio.py`)
  instead of OpenCV's ANSI file API, which wrote nothing on such a path.
- The log keeps writing when the midnight rollover is blocked by another process (retried later).
- Child processes (yt-dlp, the Resolve probe) run with a console `python.exe` even under `pythonw.exe`.

### Licensing
- MIT `LICENSE` and `THIRD_PARTY_NOTICES.md`; the installer adds the exact FFmpeg / Python / VC++ versions it ships.

## 0.1 (2026-10-16 to 2026-11-06)

Run from source only: OBS library scan, HUD kill / assist / death detection, local transcription, clip player with
track mixer, clip search, stream VOD import, per-streamer detection zones and the DaVinci Resolve hand-off.
