#!/usr/bin/env python3
"""Meniscus: a liquid-glass download queue and media library for gallery-dl + yt-dlp.

Standard library only (Pillow is used for thumbnails when present, ffmpeg for video).
Run it with the Python that has gallery-dl installed, so links can be recognised
with gallery-dl's own extractor list.
"""
from __future__ import annotations

import json
import mimetypes
import os
import queue
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

try:
    from PIL import Image, ImageOps
except ImportError:  # thumbnails for images fall back to the original file
    Image = None

try:
    from gallery_dl import extractor as gdl_extractor
except ImportError:
    gdl_extractor = None

import encode as ENC

# ---------------------------------------------------------------- paths ----
ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
BIN = Path(sys.executable).parent
XDG_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
XDG_CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
XDG_DATA = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
APP_CONFIG = XDG_CONFIG / "meniscus"
APP_DATA = XDG_DATA / "meniscus"
THUMBS = XDG_CACHE / "meniscus" / "thumbs"
GDL_CONFIG = XDG_CONFIG / "gallery-dl" / "config.json"
for d in (APP_CONFIG, APP_DATA, THUMBS):
    d.mkdir(parents=True, exist_ok=True)


def which(name: str) -> str | None:
    return shutil.which(name, path=str(BIN)) or shutil.which(name)


GALLERY_DL = which("gallery-dl")
YT_DLP = which("yt-dlp")
FFMPEG = which("ffmpeg")
FFPROBE = which("ffprobe")


def read_gdl_config() -> dict:
    try:
        return json.loads(GDL_CONFIG.read_text())
    except (OSError, ValueError):
        return {}


def base_directory() -> Path:
    cfg = read_gdl_config().get("extractor", {})
    return Path(os.path.expanduser(cfg.get("base-directory", "~/Downloads/gdl"))).resolve()


BASE = base_directory()
BASE.mkdir(parents=True, exist_ok=True)

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif", ".jxl", ".heic", ".tif", ".tiff"}
VIDEO_EXT = {".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi", ".flv", ".gifv"}
AUDIO_EXT = {".mp3", ".m4a", ".opus", ".ogg", ".flac", ".wav", ".aac"}
MEDIA_EXT = IMAGE_EXT | VIDEO_EXT | AUDIO_EXT
URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.I)

# ------------------------------------------------------------- settings ----
SETTINGS_PATH = APP_CONFIG / "settings.json"
DEFAULT_SETTINGS = {"clipboard": True, "auto_absorb": False, "concurrency": 2,
                    "encode": "original", "export_dir": "~/Downloads/discord-ready"}


def load_settings() -> dict:
    try:
        return {**DEFAULT_SETTINGS, **json.loads(SETTINGS_PATH.read_text())}
    except (OSError, ValueError):
        return dict(DEFAULT_SETTINGS)


SETTINGS = load_settings()


def save_settings() -> None:
    SETTINGS_PATH.write_text(json.dumps(SETTINGS, indent=2))


# ------------------------------------------------------------ event bus ----
class Bus:
    def __init__(self) -> None:
        self.subs: list[queue.Queue] = []
        self.lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=500)
        with self.lock:
            self.subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            if q in self.subs:
                self.subs.remove(q)

    def publish(self, event: str, data) -> None:
        msg = f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()
        with self.lock:
            for q in list(self.subs):
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    pass


BUS = Bus()

# ------------------------------------------------------------- database ----
DB_PATH = APP_DATA / "library.sqlite3"
_db_lock = threading.RLock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA journal_mode=WAL")
_conn.executescript(
    """
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY,
  path TEXT UNIQUE NOT NULL,
  rel TEXT, site TEXT, creator TEXT, kind TEXT, ext TEXT,
  size INTEGER, mtime REAL, added REAL,
  w INTEGER, h INTEGER, duration REAL, color TEXT,
  title TEXT, tags TEXT, source TEXT, job TEXT,
  fav INTEGER DEFAULT 0, thumb INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS items_added ON items(added DESC);
CREATE INDEX IF NOT EXISTS items_site ON items(site);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, data TEXT NOT NULL, created REAL
);
CREATE TABLE IF NOT EXISTS exports (
  id INTEGER PRIMARY KEY, item INTEGER, preset TEXT, path TEXT, size INTEGER, note TEXT, created REAL
);
"""
)
_conn.commit()


def db(sql: str, args=(), many=False):
    with _db_lock:
        cur = _conn.executemany(sql, args) if many else _conn.execute(sql, args)
        _conn.commit()
        return cur


def rows(sql: str, args=()) -> list[dict]:
    with _db_lock:
        return [dict(r) for r in _conn.execute(sql, args).fetchall()]


# ------------------------------------------------------------- metadata ----
def kind_of(ext: str) -> str:
    if ext == ".gif":
        return "gif"
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    return "audio"


def sidecar_for(path: Path) -> dict:
    meta = path.parent / ".meta"
    for cand in (meta / (path.name + ".json"), meta / (path.stem + ".info.json"),
                 path.with_suffix(".info.json")):
        if cand.is_file():
            try:
                return json.loads(cand.read_text(errors="replace"))
            except (OSError, ValueError):
                return {}
    return {}


def _tag_list(v) -> list[str]:
    if not v:
        return []
    if isinstance(v, str):
        return v.split()
    if isinstance(v, dict):
        out = []
        for sub in v.values():
            out += _tag_list(sub)
        return out
    out = []
    for t in v:
        if isinstance(t, str):
            out.append(t)
        elif isinstance(t, dict):
            out.append(str(t.get("name") or t.get("tag") or t.get("title") or ""))
    return out


def describe(path: Path) -> dict:
    rel = path.relative_to(BASE)
    parts = rel.parts
    site, creator = (parts[0] if len(parts) > 1 else "loose"), (parts[1] if len(parts) > 2 else "")
    if site == "yt-dlp" and len(parts) > 2:  # yt-dlp/<extractor>/<uploader>/file
        site, creator = parts[1].lower(), (parts[2] if len(parts) > 3 else "")
    meta = sidecar_for(path)
    tags: list[str] = []
    for key in ("tags", "tag_string", "hashtags", "categories", "tag_string_general",
                "tag_string_character", "tag_string_copyright", "tag_string_artist"):
        tags += _tag_list(meta.get(key))
    seen, clean = set(), []
    for t in tags:
        t = t.strip().lower().replace(" ", "_").lstrip("#")
        if t and t not in seen and len(t) < 64:
            seen.add(t)
            clean.append(t)
    title = ""
    for key in ("title", "description", "content", "caption", "text", "desc"):
        v = meta.get(key)
        if isinstance(v, str) and v.strip():
            title = re.sub(r"\s+", " ", v).strip()[:300]
            break
    creator = str(meta.get("uploader") or meta.get("author_name") or meta.get("username")
                  or (meta.get("author") if isinstance(meta.get("author"), str) else "")
                  or (meta.get("user") if isinstance(meta.get("user"), str) else "")
                  or creator)
    source = meta.get("webpage_url") or meta.get("post_url") or meta.get("permalink") or ""
    if not source and isinstance(meta.get("url"), str) and site not in ("directlink",):
        source = meta["url"]
    w, h = meta.get("width"), meta.get("height")
    st = path.stat()
    ext = path.suffix.lower()
    return {
        "path": str(path), "rel": str(rel), "site": site.lower(), "creator": creator[:120],
        "kind": kind_of(ext), "ext": ext.lstrip("."), "size": st.st_size, "mtime": st.st_mtime,
        "w": w if isinstance(w, int) else None, "h": h if isinstance(h, int) else None,
        "duration": meta.get("duration") if isinstance(meta.get("duration"), (int, float)) else None,
        "title": title, "tags": " ".join(clean[:200]), "source": str(source)[:1000],
    }


def is_media(path: Path) -> bool:
    return path.suffix.lower() in MEDIA_EXT and not any(p.startswith(".") for p in path.relative_to(BASE).parts)


def index_file(path: Path, job: str | None = None) -> int | None:
    try:
        path = path.resolve()
        if not path.is_file() or BASE not in path.parents or not is_media(path):
            return None
        d = describe(path)
    except (OSError, ValueError):
        return None
    existing = rows("SELECT id, mtime FROM items WHERE path=?", (d["path"],))
    if existing:
        if abs((existing[0]["mtime"] or 0) - d["mtime"]) < 1e-3:
            return existing[0]["id"]
        cols = ", ".join(f"{k}=?" for k in d)
        db(f"UPDATE items SET {cols}, thumb=0 WHERE id=?", (*d.values(), existing[0]["id"]))
        THUMB_Q.put(existing[0]["id"])
        return existing[0]["id"]
    d["added"] = d["mtime"] if job is None else time.time()
    d["job"] = job
    cur = db(f"INSERT INTO items ({', '.join(d)}) VALUES ({', '.join('?' * len(d))})", tuple(d.values()))
    THUMB_Q.put(cur.lastrowid)
    return cur.lastrowid


def scan_library() -> None:
    BUS.publish("scan", {"state": "start"})
    known = {r["path"]: r["id"] for r in rows("SELECT id, path FROM items")}
    found = set()
    added = 0
    for dirpath, dirnames, filenames in os.walk(BASE):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for f in filenames:
            p = Path(dirpath) / f
            if p.suffix.lower() not in MEDIA_EXT:
                continue
            found.add(str(p.resolve()))
            if str(p.resolve()) not in known:
                if index_file(p):
                    added += 1
    gone = [i for path, i in known.items() if path not in found]
    if gone:
        db("DELETE FROM items WHERE id=?", [(i,) for i in gone], many=True)
    for r in rows("SELECT id FROM items WHERE thumb=0"):
        THUMB_Q.put(r["id"])
    BUS.publish("scan", {"state": "done", "added": added, "removed": len(gone)})
    if added or gone:
        BUS.publish("library", {"added": added, "removed": len(gone)})


# ----------------------------------------------------------- thumbnails ----
THUMB_Q: queue.Queue = queue.Queue()


def vivid_color(img) -> str:
    """Pick a saturated, representative colour rather than a muddy average."""
    small = img.convert("RGB").resize((24, 24))
    best, score_best = (90, 110, 160), -1.0
    buckets: dict = {}
    for r, g, b in small.getdata():
        mx, mn = max(r, g, b), min(r, g, b)
        sat = (mx - mn) / (mx + 1)
        key = (r // 32, g // 32, b // 32)
        cnt, sr, sg, sb, ss = buckets.get(key, (0, 0, 0, 0, 0.0))
        buckets[key] = (cnt + 1, sr + r, sg + g, sb + b, ss + sat)
    for cnt, sr, sg, sb, ss in buckets.values():
        lum = (sr + sg + sb) / (3 * cnt)
        score = cnt * (0.35 + ss / cnt) * (0.4 if lum < 30 or lum > 235 else 1.0)
        if score > score_best:
            score_best, best = score, (sr // cnt, sg // cnt, sb // cnt)
    return "#%02x%02x%02x" % best


def probe_video(path: str) -> tuple:
    if not FFPROBE:
        return None, None, None
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height:format=duration", "-of", "json", path],
            capture_output=True, text=True, timeout=20).stdout
        j = json.loads(out or "{}")
        s = (j.get("streams") or [{}])[0]
        return s.get("width"), s.get("height"), float(j.get("format", {}).get("duration") or 0) or None
    except (subprocess.SubprocessError, ValueError, OSError):
        return None, None, None


def make_thumb(item_id: int) -> None:
    r = rows("SELECT * FROM items WHERE id=?", (item_id,))
    if not r:
        return
    it = r[0]
    out = THUMBS / f"{item_id}.webp"
    w, h, dur, color, ok = it["w"], it["h"], it["duration"], it["color"], -1
    try:
        src = it["path"]
        if it["kind"] == "video" and FFMPEG:
            pw, ph, pd = probe_video(src)
            w, h, dur = pw or w, ph or h, pd or dur
            frame = THUMBS / f"{item_id}.frame.jpg"
            seek = "1" if (dur or 0) > 2 else "0"
            subprocess.run([FFMPEG, "-y", "-v", "error", "-ss", seek, "-i", src, "-frames:v", "1",
                            "-vf", "scale=520:-2", str(frame)], timeout=60, capture_output=True)
            src = str(frame) if frame.exists() else None
        if src and Image and it["kind"] != "audio":
            with Image.open(src) as im:
                if it["kind"] != "video":
                    w, h = im.size
                im = ImageOps.exif_transpose(im).convert("RGB")
                color = vivid_color(im)
                im.thumbnail((520, 1300))
                im.save(out, "WEBP", quality=78)
                ok = 1
        if it["kind"] == "video":
            (THUMBS / f"{item_id}.frame.jpg").unlink(missing_ok=True)
    except Exception as e:  # noqa: BLE001 - one bad file must not stop the worker
        print(f"thumb {item_id} failed: {e}", file=sys.stderr)
    db("UPDATE items SET thumb=?, w=?, h=?, duration=?, color=? WHERE id=?", (ok, w, h, dur, color, item_id))
    BUS.publish("thumb", {"id": item_id, "ok": ok, "w": w, "h": h, "color": color})


def thumb_worker() -> None:
    while True:
        item_id = THUMB_Q.get()
        try:
            make_thumb(item_id)
        finally:
            THUMB_Q.task_done()


# ------------------------------------------------------------------ jobs ----
JOBS: dict[str, dict] = {}
JOB_Q: queue.Queue = queue.Queue()
PROCS: dict[str, subprocess.Popen] = {}
_jobs_lock = threading.Lock()
PROGRESS_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)%")


def classify(url: str) -> dict:
    site, supported = urlparse(url).hostname or "link", False
    if gdl_extractor is not None:
        try:
            ex = gdl_extractor.find(url)
            if ex is not None and ex.category not in ("directlink",):
                site, supported = ex.category, True
            elif ex is not None:
                site, supported = "direct", True
        except Exception:  # noqa: BLE001
            pass
    site = re.sub(r"^www\.", "", site)
    return {"site": site, "supported": supported}


def job_public(j: dict) -> dict:
    return {k: v for k, v in j.items() if k not in ("log",)} | {"log": j["log"][-30:]}


def persist_job(j: dict) -> None:
    db("INSERT OR REPLACE INTO jobs (id, data, created) VALUES (?, ?, ?)",
       (j["id"], json.dumps(job_public(j)), j["created"]))


def emit_job(j: dict, persist=False) -> None:
    BUS.publish("job", job_public(j))
    if persist:
        persist_job(j)


def enqueue(url: str, mode: str = "auto") -> dict:
    info = classify(url)
    j = {"id": uuid.uuid4().hex[:10], "url": url, "mode": mode, "site": info["site"],
         "supported": info["supported"], "status": "queued", "created": time.time(),
         "started": None, "finished": None, "files": 0, "skipped": 0, "bytes": 0,
         "progress": None, "speed": "", "error": "", "items": [], "log": []}
    with _jobs_lock:
        JOBS[j["id"]] = j
    emit_job(j, persist=True)
    JOB_Q.put(j["id"])
    return j


def ytdlp_cookie_args() -> list[str]:
    """Mirror gallery-dl's extractor.cookies setting so both engines use the same session."""
    c = read_gdl_config().get("extractor", {}).get("cookies")
    if isinstance(c, list) and c:
        spec = str(c[0])
        if len(c) > 1 and c[1]:
            spec += ":" + str(c[1])
        return ["--cookies-from-browser", spec]
    if isinstance(c, str) and c:
        return ["--cookies", os.path.expanduser(c)]
    return []


def build_cmd(j: dict) -> list[str]:
    if j["mode"] == "video" and YT_DLP:
        out = str(BASE / "yt-dlp" / "%(extractor)s" / "%(uploader,channel,uploader_id|unknown)s"
                  / "%(title).120B [%(id)s].%(ext)s")
        meta = str(BASE / "yt-dlp" / "%(extractor)s" / "%(uploader,channel,uploader_id|unknown)s"
                   / ".meta" / "%(title).120B [%(id)s].%(ext)s")
        # When share-ready copies are wanted, ask the site for H.264/AAC first so the copy is
        # usually a quick remux instead of a full re-encode.
        prefer = []
        preset = SETTINGS.get("encode", "original")
        if preset in ENC.PRESETS:
            cap = {"discord20": ",res:720", "discord50": ",res:1080"}.get(preset, "")
            prefer = ["-S", f"vcodec:h264,acodec:aac{cap}", "--merge-output-format", "mp4"]
        return [YT_DLP, "--newline", "--progress", "--no-colors", *ytdlp_cookie_args(), *prefer,
                "--write-info-json", "-o", out, "-o", f"infojson:{meta}",
                "--progress-template", "download:MNSC %(progress._percent_str)s|%(progress._speed_str)s",
                "--print", "after_move:filepath", j["url"]]
    return [GALLERY_DL or "gallery-dl", "-o", "downloader.progress=0", j["url"]]


def run_job(job_id: str) -> None:
    j = JOBS.get(job_id)
    if not j or j["status"] != "queued":
        return
    j.update(status="running", started=time.time(), error="", progress=None)
    emit_job(j)
    cmd = build_cmd(j)
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "COLUMNS": "200"}
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(BASE),
                             env=env, start_new_session=True)
    except OSError as e:
        j.update(status="error", error=str(e), finished=time.time())
        emit_job(j, persist=True)
        return
    PROCS[job_id] = p
    last_emit = [0.0]

    def throttled():
        if time.time() - last_emit[0] > 0.25:
            last_emit[0] = time.time()
            emit_job(j)

    def read_stderr():
        buf = b""
        while True:
            chunk = p.stderr.read1(4096) if hasattr(p.stderr, "read1") else p.stderr.read(4096)
            if not chunk:
                break
            buf += chunk
            parts = re.split(rb"[\r\n]", buf)
            buf = parts.pop()
            for raw in parts:
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                m = PROGRESS_RE.search(line)
                if m and ("MB" in line or "KB" in line or "B/s" in line or "MiB" in line):
                    j["progress"] = min(float(m.group(1)) / 100, 1.0)
                    throttled()
                    continue
                if "[cookies]" in line:
                    continue
                j["log"].append(line[:400])
                if "error" in line.lower():
                    j["error"] = line[:400]
                throttled()

    t = threading.Thread(target=read_stderr, daemon=True)
    t.start()
    fresh: list[int] = []  # items this run actually downloaded (not already-held skips)
    for raw in p.stdout:
        line = raw.decode(errors="replace").rstrip("\n")
        if not line.strip():
            continue
        if line.startswith("MNSC "):
            pct, _, speed = line[5:].partition("|")
            m = PROGRESS_RE.search(pct)
            if m:
                j["progress"] = min(float(m.group(1)) / 100, 1.0)
            j["speed"] = speed.strip()
            throttled()
            continue
        skipped = line.startswith("# ")
        fpath = Path(line[2:] if skipped else line.strip())
        if fpath.is_absolute() and fpath.exists():
            iid = index_file(fpath, job=job_id)
            if skipped:
                j["skipped"] += 1
            else:
                j["files"] += 1
                try:
                    j["bytes"] += fpath.stat().st_size
                except OSError:
                    pass
            if iid:
                j["items"].append(iid)
                j["items"] = j["items"][-400:]
                if not skipped:
                    fresh.append(iid)
            j["progress"] = None if j["mode"] != "video" else j["progress"]
            emit_job(j)
            BUS.publish("library", {"added": 1, "id": iid, "job": job_id})
        else:
            j["log"].append(line[:400])
    p.wait()
    t.join(timeout=2)
    PROCS.pop(job_id, None)
    if j["status"] == "cancelled":
        pass
    elif p.returncode == 0 or (j["files"] + j["skipped"]) > 0:
        j["status"] = "done"
        if p.returncode != 0:
            j["error"] = j["error"] or f"finished with exit code {p.returncode}"
        j["progress"] = 1.0
    else:
        j["status"] = "error"
        if not j["error"]:
            j["error"] = (j["log"][-1] if j["log"] else f"exit code {p.returncode}")
    j["finished"] = time.time()
    emit_job(j, persist=True)
    preset = SETTINGS.get("encode", "original")
    if j["status"] == "done" and preset in ENC.PRESETS:
        for iid in fresh:
            it = rows("SELECT kind FROM items WHERE id=?", (iid,))
            if it and it[0]["kind"] in ("video", "gif"):
                enqueue_encode(iid, preset)


def job_worker() -> None:
    while True:
        job_id = JOB_Q.get()
        try:
            run_job(job_id)
        except Exception as e:  # noqa: BLE001
            j = JOBS.get(job_id)
            if j:
                j.update(status="error", error=f"internal: {e}", finished=time.time())
                emit_job(j, persist=True)
        finally:
            JOB_Q.task_done()


def cancel_job(job_id: str) -> None:
    j = JOBS.get(job_id)
    if not j:
        return
    if j["status"] == "queued":
        j["status"] = "cancelled"
    elif j["status"] == "running":
        j["status"] = "cancelled"
        p = PROCS.get(job_id)
        if p:
            p._cancelled = True  # type: ignore[attr-defined]  # read by encode._run
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except OSError:
                pass

            def hard_kill(proc=p):  # ffmpeg flushes its encoder on SIGTERM, which can take a while
                time.sleep(3)
                if proc.poll() is None:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except OSError:
                        pass
            threading.Thread(target=hard_kill, daemon=True).start()
    j["finished"] = time.time()
    emit_job(j, persist=True)


# ------------------------------------------------------- share-ready copies ----
ENCODE_Q: queue.Queue = queue.Queue()


def export_dir() -> Path:
    d = Path(os.path.expanduser(SETTINGS.get("export_dir") or DEFAULT_SETTINGS["export_dir"]))
    d.mkdir(parents=True, exist_ok=True)
    return d


def enqueue_encode(item_id: int, preset: str) -> dict | None:
    it = rows("SELECT id, rel FROM items WHERE id=?", (item_id,))
    if not it or preset not in ENC.PRESETS:
        return None
    j = {"id": uuid.uuid4().hex[:10], "kind": "encode", "url": it[0]["rel"].split("/")[-1], "mode": preset,
         "site": "→ " + ENC.PRESETS[preset]["label"].split(" (")[0].lower(), "supported": True,
         "status": "queued", "created": time.time(), "started": None, "finished": None, "files": 0,
         "skipped": 0, "bytes": 0, "progress": None, "speed": "", "error": "", "items": [item_id],
         "note": "", "export": None, "log": []}
    with _jobs_lock:
        JOBS[j["id"]] = j
    emit_job(j, persist=True)
    ENCODE_Q.put(j["id"])
    return j


def safe_name(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", s).strip(" .")
    return s[:110] or "clip"


def run_encode(job_id: str) -> None:
    j = JOBS.get(job_id)
    if not j or j["status"] != "queued":
        return
    j.update(status="running", started=time.time(), progress=0.0)
    emit_job(j)
    it = rows("SELECT * FROM items WHERE id=?", (j["items"][0],))
    if not it or not FFMPEG or not FFPROBE:
        j.update(status="error", error="item gone" if not it else "ffmpeg/ffprobe not found", finished=time.time())
        return emit_job(j, persist=True)
    it = it[0]
    last = [0.0]

    def progress(p: float) -> None:
        j["progress"] = p
        if time.time() - last[0] > 0.3:
            last[0] = time.time()
            emit_job(j)

    try:
        info = ENC.probe(FFPROBE, it["path"])
        pl = ENC.plan(info, j["mode"])
        tag = ENC.PRESETS[j["mode"]]["tag"]
        dst = export_dir() / f"{safe_name(Path(it['path']).stem)} [{tag}].{pl['ext']}"
        j["note"] = pl["note"]
        emit_job(j)
        res = ENC.run(FFMPEG, it["path"], str(dst), info, pl, progress, lambda p: PROCS.__setitem__(job_id, p))
        db("DELETE FROM exports WHERE path=?", (str(dst),))
        cur = db("INSERT INTO exports (item, preset, path, size, note, created) VALUES (?,?,?,?,?,?)",
                 (it["id"], j["mode"], str(dst), res["size"], res["note"], time.time()))
        j.update(status="done", progress=1.0, bytes=res["size"], files=1, note=res["note"], export=cur.lastrowid)
        BUS.publish("export", {"item": it["id"], "export": cur.lastrowid})
    except ENC.Cancelled:
        j["status"] = "cancelled"
    except Exception as e:  # noqa: BLE001
        if j["status"] != "cancelled":
            j.update(status="error", error=str(e)[:400])
    finally:
        PROCS.pop(job_id, None)
        for part in export_dir().glob("*.part.*"):  # one encode worker, so any leftover is ours
            part.unlink(missing_ok=True)
    j["finished"] = time.time()
    emit_job(j, persist=True)


def encode_worker() -> None:
    while True:
        job_id = ENCODE_Q.get()
        try:
            run_encode(job_id)
        finally:
            ENCODE_Q.task_done()


def exports_for(item_id) -> list[dict]:
    out = []
    for e in rows("SELECT * FROM exports WHERE item=? ORDER BY created DESC", (item_id,)):
        if Path(e["path"]).exists():
            e["name"] = Path(e["path"]).name
            e["label"] = ENC.PRESETS.get(e["preset"], {}).get("label", e["preset"])
            out.append(e)
        else:
            db("DELETE FROM exports WHERE id=?", (e["id"],))
    return out


def reveal(path: str) -> None:
    """Open the file manager with the file selected (FileManager1 D-Bus), else just the folder."""
    uri = Path(path).as_uri()
    r = subprocess.run(["gdbus", "call", "--session", "--dest", "org.freedesktop.FileManager1",
                        "--object-path", "/org/freedesktop/FileManager1",
                        "--method", "org.freedesktop.FileManager1.ShowItems", f"['{uri}']", ""],
                       capture_output=True, timeout=5)
    if r.returncode != 0:
        subprocess.Popen(["xdg-open", str(Path(path).parent)], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)


def copy_file_to_clipboard(path: str) -> bool:
    """Put the file on the clipboard as a file (text/uri-list), the way file managers do."""
    uri = Path(path).as_uri() + "\r\n"
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        cmd = ["wl-copy", "--type", "text/uri-list"]
    elif shutil.which("xclip"):
        cmd = ["xclip", "-selection", "clipboard", "-t", "text/uri-list"]
    else:
        return False
    subprocess.run(cmd, input=uri.encode(), timeout=5)
    return True


def load_job_history() -> None:
    for r in rows("SELECT data FROM jobs ORDER BY created DESC LIMIT 200"):
        j = json.loads(r["data"])
        if j["status"] in ("queued", "running"):
            j["status"], j["error"] = "error", "interrupted (Meniscus was closed)"
        j.setdefault("log", [])
        JOBS[j["id"]] = j


# ------------------------------------------------------------- clipboard ----
def read_clipboard() -> str:
    cmds = []
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-paste"):
        cmds.append(["wl-paste", "--no-newline", "--type", "text"])
    if shutil.which("xclip"):
        cmds.append(["xclip", "-o", "-selection", "clipboard"])
    if shutil.which("xsel"):
        cmds.append(["xsel", "--clipboard", "--output"])
    for c in cmds:
        try:
            r = subprocess.run(c, capture_output=True, timeout=2)
            if r.returncode == 0:
                return r.stdout.decode(errors="replace")[:20000]
        except (subprocess.SubprocessError, OSError):
            continue
    return ""


def clipboard_worker() -> None:
    seen = set(URL_RE.findall(read_clipboard()))  # ignore whatever was copied before launch
    last = None
    while True:
        time.sleep(0.9)
        if not SETTINGS.get("clipboard"):
            continue
        text = read_clipboard()
        if text == last:
            continue
        last = text
        for url in URL_RE.findall(text):
            url = url.rstrip(".,;)]}")
            host = urlparse(url).hostname or ""
            if url in seen or host in ("localhost", "127.0.0.1"):
                continue
            seen.add(url)
            info = classify(url)
            if SETTINGS.get("auto_absorb") and info["supported"]:
                j = enqueue(url)
                BUS.publish("clip", {"url": url, **info, "absorbed": True, "job": j["id"]})
            else:
                BUS.publish("clip", {"url": url, **info, "absorbed": False})


# ----------------------------------------------------------------- query ----
SORTS = {"new": "added DESC", "old": "added ASC", "big": "size DESC", "random": "RANDOM()",
         "name": "rel COLLATE NOCASE ASC"}
ITEM_COLS = "id, rel, site, creator, kind, ext, size, added, w, h, duration, color, title, tags, source, fav, thumb, job"


def search_items(q: str, sort: str, offset: int, limit: int) -> dict:
    where, args = [], []
    for tok in re.findall(r'(\w+:"[^"]*"|"[^"]*"|\S+)', q or ""):
        neg = tok.startswith("-") and len(tok) > 1
        tok = tok[1:] if neg else tok
        key, _, val = tok.partition(":") if re.match(r"^(site|by|tag|is|job|ext):", tok) else ("", "", tok)
        val = val.strip('"').lower()
        if not val:
            continue
        if key == "site":
            clause, a = "site = ?", [val]
        elif key == "by":
            clause, a = "lower(creator) LIKE ?", [f"%{val}%"]
        elif key == "tag":
            clause, a = "(' ' || tags || ' ') LIKE ?", [f"% {val} %"]
        elif key == "ext":
            clause, a = "ext = ?", [val]
        elif key == "job":
            clause, a = "job = ?", [val]
        elif key == "is":
            m = {"fav": "fav = 1", "video": "kind = 'video'", "image": "kind = 'image'",
                 "gif": "kind = 'gif'", "audio": "kind = 'audio'", "new": f"added > {time.time() - 86400}"}
            if val not in m:
                continue
            clause, a = m[val], []
        else:
            clause = "(lower(rel) LIKE ? OR lower(title) LIKE ? OR tags LIKE ? OR lower(creator) LIKE ? OR site LIKE ?)"
            a = [f"%{val}%"] * 5
        where.append(f"NOT {clause}" if neg else clause)
        args += a
    w = ("WHERE " + " AND ".join(where)) if where else ""
    order = SORTS.get(sort, SORTS["new"])
    total = rows(f"SELECT COUNT(*) AS n FROM items {w}", args)[0]["n"]
    items = rows(f"SELECT {ITEM_COLS} FROM items {w} ORDER BY {order}, id DESC LIMIT ? OFFSET ?",
                 (*args, limit, offset))
    return {"total": total, "items": items}


def facets() -> dict:
    sites = rows("SELECT site, COUNT(*) AS n, SUM(size) AS bytes, MAX(added) AS last FROM items GROUP BY site ORDER BY n DESC")
    for s in sites:
        s["sample"] = [r["id"] for r in rows(
            "SELECT id FROM items WHERE site=? AND thumb=1 AND kind!='audio' ORDER BY added DESC LIMIT 4", (s["site"],))]
        s["colors"] = [r["color"] for r in rows(
            "SELECT color FROM items WHERE site=? AND color IS NOT NULL ORDER BY added DESC LIMIT 6", (s["site"],))]
        s["creators"] = rows(
            "SELECT creator, COUNT(*) AS n FROM items WHERE site=? AND creator!='' GROUP BY creator ORDER BY n DESC LIMIT 12",
            (s["site"],))
    tag_counts: dict = {}
    for r in rows("SELECT tags FROM items WHERE tags != '' ORDER BY added DESC LIMIT 3000"):
        for t in r["tags"].split():
            tag_counts[t] = tag_counts.get(t, 0) + 1
    tags = sorted(tag_counts.items(), key=lambda kv: -kv[1])[:60]
    tot = rows("SELECT COUNT(*) AS n, COALESCE(SUM(size),0) AS bytes, SUM(fav) AS favs FROM items")[0]
    return {"sites": sites, "tags": [{"tag": t, "n": n} for t, n in tags], "total": tot}


def tool_versions() -> dict:
    out = {}
    for name, exe, flag in (("gallery-dl", GALLERY_DL, "--version"), ("yt-dlp", YT_DLP, "--version"),
                            ("ffmpeg", FFMPEG, "-version")):
        try:
            out[name] = subprocess.run([exe, flag], capture_output=True, text=True, timeout=10).stdout.splitlines()[0][:60] if exe else None
        except (subprocess.SubprocessError, OSError, IndexError):
            out[name] = None
    return out


VERSIONS: dict = {}

# ------------------------------------------------------------------ http ----
class Handler(BaseHTTPRequestHandler):
    server_version = "Meniscus/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet
        pass

    # helpers
    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def body_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except ValueError:
            return {}

    def send_file(self, path: Path, cache="no-store", ctype=None):
        try:
            size = path.stat().st_size
            f = open(path, "rb")
        except OSError:
            return self.send_error(404)
        ctype = ctype or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].split(",")[0].partition("-")
            if a:
                start = int(a)
                end = int(b) if b else size - 1
            else:
                start = max(0, size - int(b))
            end = min(end, size - 1)
            if start > end:
                f.close()
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        try:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = f.read(min(1 << 16, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            f.close()

    def item(self, iid: str) -> dict | None:
        r = rows("SELECT * FROM items WHERE id=?", (iid,))
        return r[0] if r else None

    # routes
    def do_GET(self):
        u = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        path = unquote(u.path)
        if path == "/api/events":
            return self.sse()
        if path == "/api/state":
            jobs = sorted(JOBS.values(), key=lambda j: -j["created"])[:120]
            return self.send_json({"jobs": [job_public(j) for j in jobs], "settings": SETTINGS,
                                   "base": str(BASE), "versions": VERSIONS,
                                   "gdl_config": str(GDL_CONFIG),
                                   "presets": [{"id": k, **v} for k, v in ENC.PRESETS.items()],
                                   "export_dir": str(export_dir())})
        if path == "/api/library":
            return self.send_json(search_items(qs.get("q", ""), qs.get("sort", "new"),
                                               int(qs.get("offset", 0)), min(int(qs.get("limit", 120)), 500)))
        if path == "/api/facets":
            return self.send_json(facets())
        m = re.match(r"^/api/items/(\d+)$", path)
        if m:
            it = self.item(m.group(1))
            return self.send_json({**it, "exports": exports_for(it["id"])}) if it else self.send_error(404)
        m = re.match(r"^/media/(\d+)", path)
        if m:
            it = self.item(m.group(1))
            return self.send_file(Path(it["path"]), cache="private, max-age=3600") if it else self.send_error(404)
        m = re.match(r"^/thumb/(\d+)", path)
        if m:
            t = THUMBS / f"{m.group(1)}.webp"
            if t.exists():
                return self.send_file(t, cache="private, max-age=86400", ctype="image/webp")
            it = self.item(m.group(1))
            if it and it["kind"] in ("image", "gif"):
                return self.send_file(Path(it["path"]), cache="private, max-age=600")
            return self.send_error(404)
        # static
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (WEB / rel).resolve()
        if WEB in target.parents and target.is_file():
            return self.send_file(target)
        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        b = self.body_json()
        if path == "/api/jobs":
            urls = b.get("urls") or URL_RE.findall(b.get("text", ""))
            mode = b.get("mode", "auto") if b.get("mode") in ("auto", "video") else "auto"
            jobs = [enqueue(u.rstrip(".,;)]}"), mode) for u in dict.fromkeys(urls)]
            return self.send_json({"jobs": [job_public(j) for j in jobs]})
        if path == "/api/classify":
            return self.send_json([{"url": u, **classify(u)} for u in (b.get("urls") or [])[:50]])
        m = re.match(r"^/api/jobs/(\w+)/(cancel|retry|remove)$", path)
        if m:
            jid, act = m.groups()
            j = JOBS.get(jid)
            if not j:
                return self.send_error(404)
            if act == "cancel":
                cancel_job(jid)
            elif act == "retry":
                nj = (enqueue_encode(j["items"][0], j["mode"]) if j.get("kind") == "encode"
                      else enqueue(j["url"], j["mode"]))
                if nj is None:
                    return self.send_json({"error": "item no longer in the library"}, 404)
                return self.send_json(job_public(nj))
            elif act == "remove" and j["status"] not in ("running",):
                JOBS.pop(jid, None)
                db("DELETE FROM jobs WHERE id=?", (jid,))
                BUS.publish("job_removed", {"id": jid})
            return self.send_json({"ok": True})
        if path == "/api/jobs/clear":
            for jid in [k for k, j in JOBS.items() if j["status"] in ("done", "cancelled", "error")]:
                JOBS.pop(jid, None)
                db("DELETE FROM jobs WHERE id=?", (jid,))
            BUS.publish("jobs_cleared", {})
            return self.send_json({"ok": True})
        m = re.match(r"^/api/exports/(\d+)/(reveal|copy|delete)$", path)
        if m:
            eid, act = m.groups()
            e = rows("SELECT * FROM exports WHERE id=?", (eid,))
            if not e or not Path(e[0]["path"]).exists():
                return self.send_json({"error": "that copy is gone"}, 404)
            if act == "reveal":
                reveal(e[0]["path"])
            elif act == "copy":
                if not copy_file_to_clipboard(e[0]["path"]):
                    return self.send_json({"error": "no clipboard tool (xclip / wl-copy)"}, 500)
            elif act == "delete":
                subprocess.run(["gio", "trash", e[0]["path"]], capture_output=True)
                db("DELETE FROM exports WHERE id=?", (eid,))
            return self.send_json({"ok": True})
        m = re.match(r"^/api/items/(\d+)/(fav|reveal|open|trash|encode)$", path)
        if m:
            iid, act = m.groups()
            it = self.item(iid)
            if not it:
                return self.send_error(404)
            if act == "encode":
                j = enqueue_encode(int(iid), b.get("preset", "discord20"))
                return self.send_json(job_public(j)) if j else self.send_json({"error": "unknown preset"}, 400)
            if act == "fav":
                v = 0 if it["fav"] else 1
                db("UPDATE items SET fav=? WHERE id=?", (v, iid))
                return self.send_json({"fav": v})
            if act in ("reveal", "open"):
                target = it["path"] if act == "open" else str(Path(it["path"]).parent)
                subprocess.Popen(["xdg-open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
                return self.send_json({"ok": True})
            if act == "trash":
                r = subprocess.run(["gio", "trash", it["path"]], capture_output=True, text=True)
                if r.returncode != 0 and Path(it["path"]).exists():
                    return self.send_json({"error": r.stderr.strip() or "could not move to trash"}, 500)
                db("DELETE FROM items WHERE id=?", (iid,))
                (THUMBS / f"{iid}.webp").unlink(missing_ok=True)
                BUS.publish("library", {"removed": 1, "id": int(iid)})
                return self.send_json({"ok": True})
        if path == "/api/settings":
            for k in ("clipboard", "auto_absorb"):
                if k in b:
                    SETTINGS[k] = bool(b[k])
            if "concurrency" in b:
                SETTINGS["concurrency"] = max(1, min(6, int(b["concurrency"])))
                ensure_workers()
            if b.get("encode") in ("original", *ENC.PRESETS):
                SETTINGS["encode"] = b["encode"]
            if isinstance(b.get("export_dir"), str) and b["export_dir"].strip():
                SETTINGS["export_dir"] = b["export_dir"].strip()
            save_settings()
            BUS.publish("settings", SETTINGS)
            return self.send_json(SETTINGS)
        if path == "/api/rescan":
            threading.Thread(target=scan_library, daemon=True).start()
            return self.send_json({"ok": True})
        if path == "/api/open-config":
            target = GDL_CONFIG if b.get("what") == "config" else BASE
            subprocess.Popen(["xdg-open", str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            return self.send_json({"ok": True})
        self.send_error(404)

    def sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = BUS.subscribe()
        try:
            self.wfile.write(b"event: hello\ndata: {}\n\n")
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=15)
                except queue.Empty:
                    msg = b": ping\n\n"
                self.wfile.write(msg)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            BUS.unsubscribe(q)
            self.close_connection = True


_workers: list[threading.Thread] = []


def ensure_workers() -> None:
    while len(_workers) < SETTINGS["concurrency"]:
        t = threading.Thread(target=job_worker, daemon=True)
        t.start()
        _workers.append(t)
    # Lowering concurrency takes effect on restart; extra idle workers are harmless.


def main() -> None:
    global VERSIONS
    port = int(os.environ.get("MENISCUS_PORT", "47821"))
    load_job_history()
    ensure_workers()
    for _ in range(2):
        threading.Thread(target=thumb_worker, daemon=True).start()
    threading.Thread(target=encode_worker, daemon=True).start()
    threading.Thread(target=clipboard_worker, daemon=True).start()
    threading.Thread(target=scan_library, daemon=True).start()
    VERSIONS = tool_versions()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    print(f"Meniscus on http://127.0.0.1:{port}  (library: {BASE})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
