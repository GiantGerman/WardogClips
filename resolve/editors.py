"""Timeline exports for other editors: FCP 7 XML (xmeml, what Premiere Pro's File > Import reads)
and OpenTimelineIO (.otio JSON), both written with the standard library.

Input is the merged clip list of ``export.merge_clips`` (one clip per overlapping range of a
recording, each with its events). Both files carry the same content: one sequence / timeline,
the video of each clip on V1, every audio stream of the source file on its own audio track
(OBS writes up to 6 stereo tracks: stream N -> A<N>), and one sequence marker per event.

Frame math: a source's real rate is 60000/1001 for "59.94" (ntsc), 30000/1001 for "29.97",
24000/1001 for "23.976", else the integer rate. Source in / out = round(seconds * real rate).
The sequence takes the rate that covers the most clip time; a clip whose timebase matches the
sequence is laid in frame for frame (no implied speed change), any other clip by real time with
its clip item counted in sequence frames (the file keeps its own rate). OTIO places everything
by real time (each clip in its own rate). NTSC sequences use
drop-frame display (01:00:00;00), integer rates non-drop (01:00:00:00).
"""

import json
import logging
import re
import uuid
from pathlib import Path, PureWindowsPath
from urllib.parse import quote
from xml.sax.saxutils import escape

from resolve import export as ex

logger = logging.getLogger("wclips.export")

NTSC_BASES = (24, 30, 60)
SEQ_START_HOURS = 1
DEFAULT_SIZE = (1920, 1080)


# ---- rates and timecode ---------------------------------------------------------------------
def rate_info(fps) -> dict:
    """{timebase, ntsc, fps (the real rate)} for a probed frame rate."""
    f = float(fps or ex.DEFAULT_FPS)
    tb = max(1, int(round(f)))
    if tb in NTSC_BASES and abs(f - tb * 1000 / 1001) < 0.02 and abs(f - tb) > 0.02:
        return {"timebase": tb, "ntsc": True, "fps": tb * 1000 / 1001}
    return {"timebase": tb, "ntsc": False, "fps": float(tb) if abs(f - tb) < 0.02 else f}


def frames(t_s: float, rate: dict) -> int:
    return int(round(float(t_s) * rate["fps"]))


def drop_frame(rate: dict) -> int:
    """Frames dropped per minute in drop-frame timecode (0 = non-drop)."""
    if not rate["ntsc"] or rate["timebase"] not in (30, 60):
        return 0
    return 2 if rate["timebase"] == 30 else 4


def tc_string(n: int, rate: dict) -> str:
    """Frame count -> timecode; drop-frame (';' before the frames) for 29.97 / 59.94."""
    tb, drop = rate["timebase"], drop_frame(rate)
    n = max(0, int(n))
    if drop:
        per_min = tb * 60 - drop
        per_10 = tb * 600 - drop * 9
        d, m = divmod(n, per_10)
        n += drop * 9 * d + (drop * ((m - drop) // per_min) if m > drop else 0)
    ff, s = n % tb, n // tb
    sep = ";" if drop else ":"
    return f"{s // 3600:02d}:{(s // 60) % 60:02d}:{s % 60:02d}{sep}{ff:02d}"


def tc_frames(h: int, m: int, s: int, f: int, rate: dict) -> int:
    """Timecode fields -> frame count (inverse of tc_string)."""
    tb, drop = rate["timebase"], drop_frame(rate)
    total_min = h * 60 + m
    return ((h * 3600 + m * 60 + s) * tb + f) - drop * (total_min - total_min // 10)


def pathurl(path: str) -> str:
    """Windows path -> file://localhost/F%3a/dir%20name/file.mp4 (percent-encoded UTF-8, the
    form Premiere writes); a UNC path \\\\host\\share\\x -> file://host/share/x."""
    p = str(path)
    if p.startswith("\\\\") or p.startswith("//"):
        parts = p.replace("\\", "/").lstrip("/").split("/", 1)
        return "file://" + quote(parts[0]) + "/" + quote(parts[1] if len(parts) > 1 else "", safe="/")
    wp = PureWindowsPath(p)
    if wp.drive and len(wp.drive) == 2:
        rest = p[2:].replace("\\", "/").lstrip("/")
        return f"file://localhost/{wp.drive[0].upper()}%3a/" + quote(rest, safe="/")
    return "file://localhost/" + quote(p.replace("\\", "/").lstrip("/"), safe="/")


def otio_url(path: str) -> str:
    """Windows path -> file:///F:/dir%20name/file.mp4 (OpenTimelineIO's own convention)."""
    p = str(path)
    if p.startswith("\\\\") or p.startswith("//"):
        return "file:" + quote(p.replace("\\", "/"), safe="/")
    return "file:///" + quote(p.replace("\\", "/").lstrip("/"), safe="/:")


# ---- recording facts ------------------------------------------------------------------------
def _audio_streams(rec: dict, track_rows: list[dict]) -> list[dict]:
    try:
        tracks = json.loads(rec.get("audio_tracks") or "[]")
    except (TypeError, ValueError):
        tracks = []
    if not isinstance(tracks, list) or not tracks:
        tracks = [dict(t) for t in track_rows]
    out = []
    for t in tracks:
        if not isinstance(t, dict):
            continue
        codec = str(t.get("codec") or "")
        depth = 24 if "24" in codec else 32 if "32" in codec else 16
        try:
            ch, sr = int(t.get("channels") or 2), int(float(t.get("sample_rate") or 48000))
        except (TypeError, ValueError):
            ch, sr = 2, 48000
        out.append({"channels": max(1, ch), "sample_rate": sr, "depth": depth})
    # nothing probed: assume one stereo track rather than dropping the sound
    return out or [{"channels": 2, "sample_rate": 48000, "depth": 16}]


def rec_facts(db, rec_ids) -> dict:
    """rec_id -> {width, height, audio: [{channels, sample_rate, depth}]}."""
    ids = sorted({int(r) for r in rec_ids})
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    recs = db.query(f"SELECT id, width, height, audio_tracks FROM recordings WHERE id IN ({marks})", ids)
    rows = db.query(f"SELECT rec_id, idx, codec, sample_rate, channels FROM tracks WHERE rec_id IN ({marks})"
                    " ORDER BY rec_id, idx", ids)
    by_rec: dict[int, list] = {}
    for t in rows:
        by_rec.setdefault(t["rec_id"], []).append(t)
    out = {}
    for r in recs:
        w, h = r.get("width") or DEFAULT_SIZE[0], r.get("height") or DEFAULT_SIZE[1]
        out[r["id"]] = {"width": int(w), "height": int(h), "audio": _audio_streams(r, by_rec.get(r["id"], []))}
    return out


# ---- the shared layout (both formats are written from this) ---------------------------------
def sequence_rate(clips: list[dict]) -> dict:
    """The rate that covers the most clip time (ties: the earlier clip), so one 30 fps VOD clip
    at the top of a score-ordered list does not turn a 60 fps selects sequence into 30."""
    if not clips:
        return rate_info(ex.DEFAULT_FPS)
    total: dict[tuple, float] = {}
    first: dict[tuple, int] = {}
    for i, c in enumerate(clips):
        r = rate_info(c["fps"])
        key = (r["timebase"], r["ntsc"], round(r["fps"], 4))
        total[key] = total.get(key, 0.0) + max(0.0, float(c["clip_out_s"]) - float(c["clip_in_s"]))
        first.setdefault(key, i)
    best = max(total, key=lambda k: (total[k], -first[k]))
    return rate_info(clips[first[best]]["fps"])


def layout(db, clips: list[dict], title: str) -> dict:
    """Sequence rate + per clip: source rate, src in / out frames, sequence start / end frames,
    the file's length in frames and its audio streams; markers in sequence frames."""
    facts = rec_facts(db, [c["rec_id"] for c in clips])
    seq_rate = sequence_rate(clips)
    pos = 0
    real_pos = 0.0                                      # seconds, for OTIO (rational time per clip)
    items, markers = [], []
    for c in clips:
        r = rate_info(c["fps"])
        f = facts.get(c["rec_id"]) or {"width": DEFAULT_SIZE[0], "height": DEFAULT_SIZE[1],
                                         "audio": _audio_streams({}, [])}
        src_in = frames(c["clip_in_s"], r)
        src_out = max(src_in + 1, frames(c["clip_out_s"], r))
        n = src_out - src_in
        file_frames = max(frames(c["duration_s"], r) if c.get("duration_s") else src_out, src_out)
        if r["timebase"] == seq_rate["timebase"]:
            ci = {"ci_rate": r, "ci_in": src_in, "ci_out": src_out, "ci_dur": file_frames}
        else:
            # another timebase: the clip item counts in sequence frames (out - in == end - start, so
            # no editor reads it as a speed change); the file keeps its own rate
            n = max(1, int(round(n * seq_rate["fps"] / r["fps"])))
            ci_in = frames(src_in / r["fps"], seq_rate)
            ci = {"ci_rate": seq_rate, "ci_in": ci_in, "ci_out": ci_in + n,
                  "ci_dur": max(frames(file_frames / r["fps"], seq_rate), ci_in + n)}
        real_len = (src_out - src_in) / r["fps"]
        items.append({"clip": c, "rate": r, "src_in": src_in, "src_out": src_out, "start": pos, "end": pos + n,
                      "file_frames": file_frames, "width": f["width"], "height": f["height"],
                      "audio": f["audio"], "real_start_s": real_pos, "real_len_s": real_len, **ci})
        for ev in sorted(c["events"], key=lambda e: e["t_s"]):
            off = float(ev["t_s"]) - c["clip_in_s"]
            at = pos + int(round(off * seq_rate["fps"]))
            markers.append({"frame": min(max(pos, at), pos + n - 1), "name": marker_title(ev),
                            "comment": ex.marker_note(ev), "kind": ev.get("kind") or "event", "event_id": ev["id"],
                            "real_s": real_pos + min(max(0.0, off), max(0.0, real_len - 1.0 / r["fps"]))})
        pos += n
        real_pos += real_len
    return {"title": title, "rate": seq_rate, "duration": pos, "items": items, "markers": markers,
            "audio_tracks": max((len(i["audio"]) for i in items), default=0),
            "start_frame": tc_frames(SEQ_START_HOURS, 0, 0, 0, seq_rate)}


def marker_title(ev: dict) -> str:
    """Kind (+ names) and the user's note: 'KILL Bob - nice flank'."""
    name = ex.marker_name(ev)
    note = (ev.get("note") or "").strip().splitlines()
    if note and (ev.get("kind") or "") != "manual":
        name = f"{name} - {note[0][:60]}"
    return name


# ---- FCP 7 XML (xmeml) ----------------------------------------------------------------------
_XML_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def _e(text) -> str:
    # control characters (e.g. pasted into a note) are not allowed anywhere in XML 1.0
    return escape(_XML_BAD.sub(" ", str(text)), {'"': "&quot;"})


def _rate_xml(r: dict) -> str:
    return f"<rate><timebase>{r['timebase']}</timebase><ntsc>{'TRUE' if r['ntsc'] else 'FALSE'}</ntsc></rate>"


def _tc_xml(n: int, r: dict) -> str:
    return (f"<timecode>{_rate_xml(r)}<string>{tc_string(n, r)}</string><frame>{n}</frame>"
            f"<displayformat>{'DF' if drop_frame(r) else 'NDF'}</displayformat></timecode>")


def _file_xml(it: dict, fid: str, full: bool) -> str:
    if not full:
        return f'<file id="{fid}"/>'
    c, r = it["clip"], it["rate"]
    audio = "".join(
        f"<audio><samplecharacteristics><depth>{a['depth']}</depth><samplerate>{a['sample_rate']}</samplerate>"
        f"</samplecharacteristics><channelcount>{a['channels']}</channelcount></audio>" for a in it["audio"])
    return (f'<file id="{fid}"><name>{_e(c["rec_name"])}</name><pathurl>{_e(pathurl(c["rec_path"]))}</pathurl>'
            f"{_rate_xml(r)}<duration>{it['file_frames']}</duration>{_tc_xml(0, r)}"
            f"<media><video><samplecharacteristics>{_rate_xml(r)}<width>{it['width']}</width>"
            f"<height>{it['height']}</height><anamorphic>FALSE</anamorphic><pixelaspectratio>square</pixelaspectratio>"
            f"<fielddominance>none</fielddominance></samplecharacteristics></video>{audio}</media></file>")


def xmeml_text(lay: dict) -> str:
    sr = lay["rate"]
    first = lay["items"][0] if lay["items"] else {"width": DEFAULT_SIZE[0], "height": DEFAULT_SIZE[1]}
    n_audio = lay["audio_tracks"]
    seen_files: set[str] = set()
    video_items: list[str] = []
    audio_items: list[list[str]] = [[] for _ in range(n_audio)]
    for i, it in enumerate(lay["items"], 1):
        c, r = it["clip"], it["rate"]
        fid = f"file-{c['rec_id']}"
        vid = f"clipitem-{i}-v"
        aids = [f"clipitem-{i}-a{k}" for k in range(1, len(it["audio"]) + 1)]
        links = [f"<link><linkclipref>{vid}</linkclipref><mediatype>video</mediatype><trackindex>1</trackindex>"
                 f"<clipindex>{len(video_items) + 1}</clipindex></link>"]
        for k, aid in enumerate(aids, 1):
            links.append(f"<link><linkclipref>{aid}</linkclipref><mediatype>audio</mediatype>"
                         f"<trackindex>{k}</trackindex><clipindex>{len(audio_items[k - 1]) + 1}</clipindex>"
                         f"<groupindex>1</groupindex></link>")
        common = (f"<name>{_e(c['rec_name'])}</name>"
                  f"<enabled>TRUE</enabled><duration>{it['ci_dur']}</duration>{_rate_xml(it['ci_rate'])}"
                  f"<start>{it['start']}</start><end>{it['end']}</end><in>{it['ci_in']}</in><out>{it['ci_out']}</out>")
        full = fid not in seen_files
        seen_files.add(fid)
        video_items.append(f'<clipitem id="{vid}">{common}<alphatype>none</alphatype>{_file_xml(it, fid, full)}'
                           f"{''.join(links)}</clipitem>")
        for k, aid in enumerate(aids, 1):
            ptype = "stereo" if it["audio"][k - 1]["channels"] == 2 else "mono"
            audio_items[k - 1].append(
                f'<clipitem id="{aid}" premiereChannelType="{ptype}">{common}{_file_xml(it, fid, False)}'
                f"<sourcetrack><mediatype>audio</mediatype><trackindex>{k}</trackindex></sourcetrack>"
                f"{''.join(links)}</clipitem>")
    audio_tracks = "".join(
        f'<track premiereTrackType="Stereo">{"".join(items)}<enabled>TRUE</enabled><locked>FALSE</locked>'
        f"<outputchannelindex>1</outputchannelindex></track>" for items in audio_items)
    markers = "".join(
        f"<marker><name>{_e(m['name'])}</name><comment>{_e(m['comment'])}</comment><in>{m['frame']}</in>"
        f"<out>-1</out></marker>" for m in lay["markers"])
    a0 = (lay["items"][0]["audio"][0] if lay["items"] and lay["items"][0]["audio"]
          else {"depth": 16, "sample_rate": 48000})
    body = (f'<sequence id="sequence-1"><uuid>{uuid.uuid4()}</uuid><name>{_e(lay["title"])}</name>'
            f"<duration>{lay['duration']}</duration>{_rate_xml(sr)}{_tc_xml(lay['start_frame'], sr)}"
            f"<media><video><format><samplecharacteristics>{_rate_xml(sr)}<width>{first['width']}</width>"
            f"<height>{first['height']}</height><anamorphic>FALSE</anamorphic><pixelaspectratio>square</pixelaspectratio>"
            f"<fielddominance>none</fielddominance></samplecharacteristics></format>"
            f"<track>{''.join(video_items)}<enabled>TRUE</enabled><locked>FALSE</locked></track></video>"
            f"<audio><numOutputChannels>2</numOutputChannels><format><samplecharacteristics>"
            f"<depth>{a0['depth']}</depth><samplerate>{a0['sample_rate']}</samplerate></samplecharacteristics></format>"
            f"{audio_tracks}</audio></media>{markers}</sequence>")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n<xmeml version="4">'
            + body + "</xmeml>\n")


# ---- OpenTimelineIO -------------------------------------------------------------------------
def _rt(value: int, r: dict) -> dict:
    return {"OTIO_SCHEMA": "RationalTime.1", "rate": r["fps"], "value": float(value)}


def _range(start: int, dur: int, r: dict) -> dict:
    return {"OTIO_SCHEMA": "TimeRange.1", "duration": _rt(dur, r), "start_time": _rt(start, r)}


MARKER_COLORS = {"kill": "RED", "assist": "ORANGE", "death": "PURPLE", "voice": "GREEN", "manual": "BLUE",
                 "spotted": "YELLOW", "revive": "CYAN", "squad": "PINK"}


def otio_dict(lay: dict) -> dict:
    sr = lay["rate"]

    def clip(it: dict, audio_track: int | None) -> dict:
        c, r = it["clip"], it["rate"]
        meta = {"wardogclips": {"rec_id": c["rec_id"], "event_ids": c["event_ids"], "score": c["score"]}}
        if audio_track:
            meta["wardogclips"]["source_audio_track"] = audio_track
        return {"OTIO_SCHEMA": "Clip.1", "name": c["rec_name"], "metadata": meta,
                "source_range": _range(it["src_in"], it["src_out"] - it["src_in"], r),
                "effects": [], "markers": [], "enabled": True,
                "media_reference": {"OTIO_SCHEMA": "ExternalReference.1", "name": c["rec_name"],
                                    "target_url": otio_url(c["rec_path"]),
                                    "available_range": _range(0, it["file_frames"], r),
                                    "available_image_bounds": None, "metadata": {}}}

    def track(name: str, kind: str, children: list) -> dict:
        return {"OTIO_SCHEMA": "Track.1", "name": name, "kind": kind, "children": children, "effects": [],
                "markers": [], "enabled": True, "metadata": {}, "source_range": None}

    tracks = [track("V1", "Video", [clip(it, None) for it in lay["items"]])]
    def seq_frames(seconds: float) -> float:
        # OTIO clips keep their own rate, so positions are real time; whole frames when they are
        v = seconds * sr["fps"]
        return float(round(v)) if abs(v - round(v)) < 1e-4 else round(v, 6)

    for k in range(1, lay["audio_tracks"] + 1):
        kids, pos_s = [], 0.0
        for it in lay["items"]:
            if it["real_start_s"] > pos_s + 1e-9:       # a recording without this stream: keep the sync
                kids.append({"OTIO_SCHEMA": "Gap.1", "name": "", "metadata": {}, "effects": [], "markers": [],
                             "enabled": True,
                             "source_range": _range(0, seq_frames(it["real_start_s"] - pos_s), sr)})
            if k <= len(it["audio"]):
                kids.append(clip(it, k))
                pos_s = it["real_start_s"] + it["real_len_s"]
        tracks.append(track(f"A{k}", "Audio", kids))
    markers = [{"OTIO_SCHEMA": "Marker.2", "name": m["name"], "comment": m["comment"],
                "color": MARKER_COLORS.get(m["kind"], "RED"),
                "marked_range": _range(int(round(seq_frames(m["real_s"]))), 1, sr),
                "metadata": {"wardogclips": {"event_id": m["event_id"], "kind": m["kind"]}}} for m in lay["markers"]]
    return {"OTIO_SCHEMA": "Timeline.1", "name": lay["title"], "metadata": {},
            "global_start_time": _rt(lay["start_frame"], sr),
            "tracks": {"OTIO_SCHEMA": "Stack.1", "name": "tracks", "children": tracks, "effects": [],
                       "markers": markers, "enabled": True, "metadata": {}, "source_range": None}}


# ---- files ----------------------------------------------------------------------------------
def write_xmeml(db, clips: list[dict], path: Path, title: str) -> int:
    path.write_text(xmeml_text(layout(db, clips, title)), encoding="utf-8")
    return len(clips)


def write_otio(db, clips: list[dict], path: Path, title: str) -> int:
    path.write_text(json.dumps(otio_dict(layout(db, clips, title)), indent=2, ensure_ascii=False),
                    encoding="utf-8")
    return len(clips)
