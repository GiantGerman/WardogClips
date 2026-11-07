"""Frame readers over ffmpeg (NVDEC when available), read-only on the footage.

- `keyframe_pass`: every keyframe (OBS: one per 2 s) -> the HUD crops stacked into one
  raw frame on a pipe, timestamps from `showinfo` on stderr. 300 frames for a 10-minute
  replay, well under a minute.
- `window_pass`: one ROI at the full frame rate for a short window (the refine step).
- `grab_frame`: one JPEG at a time (calibration view, evidence).
"""

import logging
import queue
import re
import subprocess
import threading
import time

import numpy as np

from utils import paths
from vision.hud import Roi

logger = logging.getLogger("wclips.frames")

_NO_WINDOW = 0x08000000
_PTS_RE = re.compile(r"pts_time:\s*([0-9]+(?:\.[0-9]+)?)")


class FrameError(RuntimeError):
    pass


def _ffmpeg() -> str:
    exe = paths.ffmpeg_exe()
    if not exe:
        raise FrameError("ffmpeg not found on PATH")
    return exe


def _pts_reader(stream, out: list, done: threading.Event, tail: list) -> None:
    """Drain stderr: collect showinfo pts, keep the last lines for error reports. This MUST
    keep up -- showinfo is chatty and ffmpeg blocks the moment the stderr pipe is full."""
    try:
        for line in stream:
            m = _PTS_RE.search(line)
            if m:
                out.append(float(m.group(1)))
            else:
                tail.append(line.rstrip())
                if len(tail) > 30:
                    del tail[0]
    finally:
        done.set()


def _read_exact(stream, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return None if not buf else bytes(buf)
        buf += chunk
    return bytes(buf)


def keyframe_pass(path: str, width: int, height: int, rois: dict[str, Roi], small_w: int = 620,
                  hwaccel: bool = True, keyframes_only: bool = True, fps: float | None = None, start_s: int = 0,
                  skip_upto: float | None = None):
    """Yields (t_s, {"ticker": bgr, "feed": bgr, "small": bgr}) per decoded frame.
    `keyframes_only` = the fast triage; `fps` decodes everything and samples at that rate.
    `start_s` (whole seconds, a resumed detect) seeks there first: ffmpeg's input -ss drops the frames before it and
    shifts the timestamps by exactly that much, so t = start_s + pts is the same t a pass from 0 reports (e.g.
    -ss 100 -> pts 1.716667 for the frame at 101.716667; a whole second is exact in any time base, a fraction is off
    by up to a tick)."""
    start_s = max(0, int(start_s))
    last_t = None                            # the last frame given out (a CPU continuation starts after it)
    tk, fd = rois["ticker"], rois["feed"]
    w = (max(tk.w, fd.w, small_w) + 1) // 2 * 2        # even: 4:2:0 pad / vstack refuse an odd width
    small_h = int(round(height * w / width / 2.0)) * 2
    chain = (f"[0:v]split=3[a][b][c];"
             f"[a]crop={tk.w}:{tk.h}:{tk.x}:{tk.y},pad={w}:{tk.h}:0:0:black[a2];"
             f"[b]crop={fd.w}:{fd.h}:{fd.x}:{fd.y},pad={w}:{fd.h}:0:0:black[b2];"
             f"[c]scale={w}:{small_h}[c2];"
             f"[a2][b2][c2]vstack=inputs=3,showinfo[o]")
    pre = ["-v", "info", "-nostdin", "-nostats"]
    if hwaccel:
        pre += ["-hwaccel", "cuda"]
    if keyframes_only:
        pre += ["-skip_frame", "nokey"]
    inp = (["-ss", str(start_s)] if start_s else []) + ["-i", path]
    if fps and not keyframes_only:
        chain = chain.replace("[0:v]split", f"[0:v]fps={fps},split")
    cmd = [_ffmpeg(), *pre, *inp, "-filter_complex", chain, "-map", "[o]", "-fps_mode", "passthrough",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
    frame_h = tk.h + fd.h + small_h
    nbytes = w * frame_h * 3
    t0 = time.monotonic()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=False, creationflags=_NO_WINDOW,
                            bufsize=nbytes * 4)
    pts: list[float] = []
    tail: list[str] = []
    done = threading.Event()
    th = threading.Thread(target=_pts_reader, args=(_Text(proc.stderr), pts, done, tail), daemon=True)
    th.start()
    n = 0
    try:
        while True:
            raw = _read_exact(proc.stdout, nbytes)
            if raw is None or len(raw) < nbytes:
                break
            frame = np.frombuffer(raw, np.uint8).reshape(frame_h, w, 3)
            # showinfo logs a frame before the muxer writes it, but the stderr thread may lag a hair
            waited = 0.0
            while len(pts) <= n and not done.is_set() and waited < 5.0:
                time.sleep(0.005)
                waited += 0.005
            t = pts[n] if len(pts) > n else (pts[-1] if pts else 0.0)
            if start_s:
                t = round(start_s + t, 6)
            if skip_upto is not None and t <= skip_upto + 1e-3:
                n += 1                       # the NVDEC run already gave this one (CPU continuation below)
                continue
            last_t = t
            yield t, {"ticker": frame[:tk.h, :tk.w].copy(),
                      "feed": frame[tk.h:tk.h + fd.h, :fd.w].copy(),
                      "small": frame[tk.h + fd.h:, :].copy()}
            n += 1
    finally:
        try:
            proc.stdout.close()
        except OSError:
            pass
        rc = proc.wait(timeout=30) if proc.poll() is None else proc.returncode
        th.join(timeout=5)
    if rc not in (0, None) and n == 0:
        if hwaccel:
            logger.warning("[Frames] NVDEC pass failed (%s) -- retrying on the CPU", (tail[-1] if tail else rc))
            yield from keyframe_pass(path, width, height, rois, small_w, hwaccel=False, keyframes_only=keyframes_only, fps=fps,
                                     start_s=start_s, skip_upto=skip_upto)
            return
        raise FrameError("ffmpeg failed: " + " | ".join(tail[-3:]))
    if rc not in (0, None):
        # died part-way: ending quietly here would make detect take a cut pass for the whole file
        if hwaccel and last_t is not None:
            logger.warning("[Frames] NVDEC pass died at %.1fs (%s) -- the rest on the CPU", last_t, (tail[-1] if tail else rc))
            yield from keyframe_pass(path, width, height, rois, small_w, hwaccel=False, keyframes_only=keyframes_only, fps=fps,
                                     start_s=int(last_t), skip_upto=last_t)
            return
        raise FrameError(f"ffmpeg died after {n} frames (rc={rc}): " + " | ".join(tail[-3:]))
    logger.info("[Frames] %s frames=%d %.1fs (%s)", "keyframes" if keyframes_only else f"{fps} fps", n,
                time.monotonic() - t0, "nvdec" if hwaccel else "cpu")


class _Text:
    """Line iterator over a binary pipe (ffmpeg stderr)."""

    def __init__(self, stream):
        self.stream = stream

    def __iter__(self):
        for raw in self.stream:
            yield raw.decode("utf-8", "replace")


def window_pass(path: str, t_start: float, duration: float, roi: Roi, gray: bool = True, hwaccel: bool = True,
                fps: float | None = None):
    """Yields (t_s, roi image) at the full frame rate (or `fps`) for [t_start, t_start + duration)."""
    t_start = max(0.0, t_start)
    vf = f"crop={roi.w}:{roi.h}:{roi.x}:{roi.y}"
    if fps:
        vf += f",fps={fps}"
    vf += ",showinfo"
    pre = ["-v", "info", "-nostdin", "-nostats"]
    if hwaccel:
        pre += ["-hwaccel", "cuda"]
    pix = "gray" if gray else "bgr24"
    cmd = [_ffmpeg(), *pre, "-ss", f"{t_start:.3f}", "-t", f"{duration:.3f}", "-i", path, "-vf", vf, "-an",
           "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", pix, "pipe:1"]
    nbytes = roi.w * roi.h * (1 if gray else 3)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=False, creationflags=_NO_WINDOW,
                            bufsize=nbytes * 8)
    pts: list[float] = []
    tail: list[str] = []
    done = threading.Event()
    th = threading.Thread(target=_pts_reader, args=(_Text(proc.stderr), pts, done, tail), daemon=True)
    th.start()
    n = 0
    try:
        while True:
            raw = _read_exact(proc.stdout, nbytes)
            if raw is None or len(raw) < nbytes:
                break
            img = np.frombuffer(raw, np.uint8).reshape((roi.h, roi.w) if gray else (roi.h, roi.w, 3)).copy()
            waited = 0.0
            while len(pts) <= n and not done.is_set() and waited < 5.0:
                time.sleep(0.005)
                waited += 0.005
            rel = pts[n] if len(pts) > n else (pts[-1] if pts else 0.0)
            yield t_start + rel, img
            n += 1
    finally:
        try:
            proc.stdout.close()
        except OSError:
            pass
        rc = proc.wait(timeout=30) if proc.poll() is None else proc.returncode
        th.join(timeout=5)
    if rc not in (0, None) and n == 0:
        if hwaccel:
            yield from window_pass(path, t_start, duration, roi, gray, hwaccel=False, fps=fps)
            return
        raise FrameError("ffmpeg failed: " + " | ".join(tail[-3:]))


def grab_frame(path: str, t: float, max_w: int = 1280, quality: int = 4, timeout: float = 60.0) -> bytes:
    """One JPEG of the frame at t (scaled to max_w wide)."""
    cmd = [_ffmpeg(), "-v", "error", "-nostdin", "-ss", f"{max(0.0, t):.3f}", "-i", path, "-frames:v", "1",
           "-vf", f"scale='min({max_w},iw)':-2", "-pix_fmt", "yuvj420p", "-strict", "unofficial",
           "-f", "image2", "-c:v", "mjpeg", "-q:v", str(quality), "pipe:1"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise FrameError("ffmpeg timed out grabbing a frame")
    if r.returncode != 0 or not r.stdout:
        raise FrameError((r.stderr.decode("utf-8", "replace") or "ffmpeg failed").strip()[-200:])
    return r.stdout


def grab_crops(path: str, t: float, rois: dict[str, Roi], timeout: float = 60.0) -> dict[str, np.ndarray]:
    """Full-resolution BGR crops of every ROI at t (evidence images)."""
    import cv2
    cmd = [_ffmpeg(), "-v", "error", "-nostdin", "-ss", f"{max(0.0, t):.3f}", "-i", path, "-frames:v", "1",
           "-f", "image2", "-c:v", "png", "pipe:1"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise FrameError("ffmpeg timed out grabbing crops")
    if r.returncode != 0 or not r.stdout:
        raise FrameError((r.stderr.decode("utf-8", "replace") or "ffmpeg failed").strip()[-200:])
    frame = cv2.imdecode(np.frombuffer(r.stdout, np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise FrameError("could not decode the grabbed frame")
    out = {}
    for name, roi in rois.items():
        out[name] = frame[roi.y:roi.y + roi.h, roi.x:roi.x + roi.w].copy()
    return out
