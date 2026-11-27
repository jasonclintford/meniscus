"""Share-ready copies: ffmpeg presets that embed and play inline on Discord.

Discord plays MP4 (H.264 video, AAC audio) inline everywhere, including the iOS and Android
apps. Other codecs, such as HEVC, AV1 or VP9 in MP4, play on some clients and show as a bare file on others.
So every preset targets H.264 High / yuv420p / AAC in an MP4 with the index at the front
(faststart), and the Discord presets additionally fit the file under an upload limit.

The planner avoids work where it can:
  * a GIF already under the limit is copied as-is (Discord autoplays GIFs, but not MP4s),
  * an H.264/AAC MP4 under the limit is only remuxed (seconds, no quality loss),
  * otherwise it re-encodes, two-pass when a size target matters.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

MIB = 1024 * 1024

PRESETS: dict[str, dict] = {
    "discord20": {"label": "Discord · 20 MB (free)", "tag": "20MB", "limit_mb": 20},
    "discord50": {"label": "Discord · 50 MB (Nitro Basic)", "tag": "50MB", "limit_mb": 50},
    "discord500": {"label": "Discord · 500 MB (Nitro)", "tag": "500MB", "limit_mb": 500},
    "compat": {"label": "Compatible MP4 (H.264, any size)", "tag": "h264", "limit_mb": None},
}


class Cancelled(Exception):
    pass


class EncodeError(Exception):
    pass


def probe(ffprobe: str, path: str) -> dict:
    out = subprocess.run(
        [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    if out.returncode != 0:
        raise EncodeError(out.stderr.strip() or "ffprobe failed")
    j = json.loads(out.stdout or "{}")
    v = next((s for s in j.get("streams", []) if s.get("codec_type") == "video"
              and not (s.get("disposition") or {}).get("attached_pic")), None)
    a = next((s for s in j.get("streams", []) if s.get("codec_type") == "audio"), None)
    fmt = j.get("format", {})
    fps = 0.0
    if v:
        num, _, den = (v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/1").partition("/")
        try:
            fps = float(num) / float(den or 1)
        except (ValueError, ZeroDivisionError):
            fps = 0.0
    dur = float(fmt.get("duration") or (v or {}).get("duration") or 0)
    return {
        "has_video": v is not None,
        "vcodec": (v or {}).get("codec_name"),
        "pix_fmt": (v or {}).get("pix_fmt"),
        "width": (v or {}).get("width") or 0,
        "height": (v or {}).get("height") or 0,
        "fps": fps,
        "acodec": (a or {}).get("codec_name"),
        "duration": dur,
        "format": fmt.get("format_name", ""),
        "size": int(fmt.get("size") or os.path.getsize(path)),
    }


def plan(info: dict, preset: str) -> dict:
    p = PRESETS[preset]
    limit = int(p["limit_mb"] * MIB * 0.97) if p["limit_mb"] else None  # 3% headroom for the container
    if not info["has_video"]:
        raise EncodeError("no video stream to convert")
    is_gif = "gif" in info["format"] or info["vcodec"] == "gif"
    fits = limit is None or info["size"] <= limit

    if is_gif and fits:
        return {"action": "copy", "ext": "gif", "limit": limit,
                "note": "GIF kept as-is, so it autoplays in Discord"}
    compatible = (info["vcodec"] == "h264" and info["pix_fmt"] in ("yuv420p", "yuvj420p")
                  and info["acodec"] in (None, "aac") and ("mp4" in info["format"] or "mov" in info["format"]))
    if compatible and fits:
        return {"action": "remux", "ext": "mp4", "limit": limit, "note": "already H.264/AAC, remuxed without re-encoding"}

    audio = info["acodec"] is not None
    short = min(info["width"], info["height"]) or 720
    fps = info["fps"] or 30
    if limit is None:
        return {"action": "crf", "ext": "mp4", "limit": None, "crf": 20, "audio_k": 160 if audio else 0,
                "short": None, "fps": 60 if fps > 60 else None, "note": "H.264 CRF 20"}

    dur = max(info["duration"], 0.5)
    total_k = limit * 8 / 1000 / dur
    audio_k = 0 if not audio else 128 if total_k > 2500 else 96 if total_k > 900 else 64
    video_k = total_k - audio_k
    if video_k < 90:
        raise EncodeError(f"too long for {p['limit_mb']} MB: {dur / 60:.1f} min would need under 90 kb/s of video")
    # Plenty of room: encode for quality and only use the limit as a ceiling.
    if video_k > 9000:
        return {"action": "crf", "ext": "mp4", "limit": limit, "crf": 19, "audio_k": audio_k or 0,
                "maxrate_k": int(video_k), "short": 1080 if short > 1080 else None,
                "fps": 60 if fps > 60 else None, "note": "H.264 CRF 19, capped to the limit"}
    target_side = next(side for floor, side in ((4500, 1080), (2000, 720), (1000, 540), (500, 480), (0, 360))
                       if video_k >= floor)
    return {"action": "2pass", "ext": "mp4", "limit": limit, "video_k": int(video_k), "audio_k": audio_k,
            "short": target_side if short > target_side else None,
            "fps": 30 if (fps > 30 and video_k < 2500) else (60 if fps > 60 else None),
            "note": f"2-pass H.264 at {int(video_k)} kb/s"}


def _vf(pl: dict) -> str:
    f = []
    s = pl.get("short")
    if s:
        f.append(f"scale='if(lte(iw,ih),min(iw,{s}),-2)':'if(lte(iw,ih),-2,min(ih,{s}))':flags=lanczos")
    f.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")  # H.264 yuv420p needs even dimensions
    if pl.get("fps"):
        f.append(f"fps={pl['fps']}")
    f.append("format=yuv420p")
    return ",".join(f)


def _run(cmd: list[str], duration: float, lo: float, hi: float,
         on_progress: Callable[[float], None], on_proc: Callable[[subprocess.Popen], None]) -> None:
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                         errors="replace", start_new_session=True)
    on_proc(p)
    err_tail: list[str] = []

    def drain():
        for line in p.stderr:
            err_tail.append(line.strip())
            del err_tail[:-15]

    import threading
    t = threading.Thread(target=drain, daemon=True)
    t.start()
    for line in p.stdout:
        m = re.match(r"out_time_(?:us|ms)=(\d+)", line)
        if m and duration > 0:
            on_progress(lo + (hi - lo) * min(1.0, int(m.group(1)) / 1e6 / duration))
    p.wait()
    t.join(timeout=2)
    if p.returncode in (-15, -9, 255) and getattr(p, "_cancelled", False):
        raise Cancelled()
    if p.returncode != 0:
        if getattr(p, "_cancelled", False):
            raise Cancelled()
        raise EncodeError(next((l for l in reversed(err_tail) if l), f"ffmpeg exit {p.returncode}"))


def run(ffmpeg: str, src: str, dst: str, info: dict, pl: dict,
        on_progress: Callable[[float], None], on_proc: Callable[[subprocess.Popen], None]) -> dict:
    """Produce dst. Returns {size, note}. Retries smaller if a sized encode overshoots."""
    tmp = dst + ".part" + Path(dst).suffix
    base = [ffmpeg, "-y", "-hide_banner", "-nostdin", "-v", "error", "-progress", "pipe:1", "-nostats", "-i", src]
    dur = info["duration"]
    if pl["action"] == "copy":
        shutil.copy2(src, tmp)
    elif pl["action"] == "remux":
        _run(base + ["-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-movflags", "+faststart", tmp],
             dur, 0, 1, on_progress, on_proc)
    else:
        audio = ["-map", "0:a:0?", "-c:a", "aac", "-b:a", f"{pl['audio_k']}k", "-ac", "2"] if pl["audio_k"] else ["-an"]
        x264 = ["-c:v", "libx264", "-preset", "medium", "-profile:v", "high", "-pix_fmt", "yuv420p"]
        tail = ["-movflags", "+faststart", "-map_metadata", "-1", tmp]
        attempts = 0
        video_k = pl.get("video_k")
        while True:
            attempts += 1
            if pl["action"] == "crf":
                rate = ["-crf", str(pl["crf"])]
                if pl.get("maxrate_k"):
                    rate += ["-maxrate", f"{pl['maxrate_k']}k", "-bufsize", f"{pl['maxrate_k'] * 2}k"]
                _run(base + ["-map", "0:v:0", "-vf", _vf(pl)] + x264 + rate + audio + tail,
                     dur, 0, 1, on_progress, on_proc)
            else:
                logdir = tempfile.mkdtemp(prefix="meniscus-2pass-")
                log = os.path.join(logdir, "pass")
                try:
                    _run(base + ["-map", "0:v:0", "-vf", _vf(pl)] + x264 +
                         ["-b:v", f"{video_k}k", "-pass", "1", "-passlogfile", log, "-an", "-f", "null", os.devnull],
                         dur, 0, .45, on_progress, on_proc)
                    _run(base + ["-map", "0:v:0", "-vf", _vf(pl)] + x264 +
                         ["-b:v", f"{video_k}k", "-pass", "2", "-passlogfile", log] + audio + tail,
                         dur, .45, 1, on_progress, on_proc)
                finally:
                    shutil.rmtree(logdir, ignore_errors=True)
            size = os.path.getsize(tmp)
            if not pl["limit"] or size <= pl["limit"] or attempts >= 3:
                break
            # overshot: shrink the video budget in proportion and go again
            if pl["action"] == "crf":
                pl = {**pl, "action": "2pass", "video_k": int(pl["maxrate_k"] * .9)}
                video_k = pl["video_k"]
            video_k = int(video_k * pl["limit"] / size * .95)
            pl = {**pl, "video_k": video_k, "note": f"2-pass H.264 at {video_k} kb/s (retry {attempts})"}
    os.replace(tmp, dst)
    size = os.path.getsize(dst)
    if pl["limit"] and size > pl["limit"] / .97:
        raise EncodeError(f"result is {size / MIB:.1f} MB, still over the limit")
    return {"size": size, "note": pl["note"]}
