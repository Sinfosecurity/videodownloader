from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import Dict, Optional

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR     = Path(__file__).parent
DOWNLOAD_DIR = Path("/tmp/vdl_downloads")
COOKIES_FILE = Path("/tmp/vdl_cookies.txt")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI()

tasks: Dict[str, dict] = {}


# ── Optional cookies (set env vars on Railway to enable) ──────────────────────
def _init_cookies() -> bool:
    # COOKIES_CONTENT  — full Netscape cookies.txt content
    # INSTAGRAM_SESSION_ID — Instagram sessionid value
    # YOUTUBE_SESSION_ID   — YouTube __Secure-3PSID value
    lines = ["# Netscape HTTP Cookie File\n"]
    found = False

    full = os.environ.get("COOKIES_CONTENT", "").strip()
    if full:
        COOKIES_FILE.write_text(full)
        return True

    ig = os.environ.get("INSTAGRAM_SESSION_ID", "").strip()
    if ig:
        lines.append(f".instagram.com\tTRUE\t/\tTRUE\t2147483647\tsessionid\t{ig}\n")
        found = True

    yt = os.environ.get("YOUTUBE_SESSION_ID", "").strip()
    if yt:
        lines.append(f".youtube.com\tTRUE\t/\tTRUE\t2147483647\t__Secure-3PSID\t{yt}\n")
        found = True

    if found:
        COOKIES_FILE.write_text("".join(lines))
    return found


HAS_COOKIES = _init_cookies()
PROXY_URL   = os.environ.get("PROXY_URL", "").strip() or None

# COOKIES_FROM_BROWSER=chrome  — use local Chrome cookies (best for local dev)
# Leave unset on Railway and use INSTAGRAM_SESSION_ID / COOKIES_CONTENT instead
COOKIES_FROM_BROWSER = os.environ.get("COOKIES_FROM_BROWSER", "").strip().lower() or None


# ── Models ─────────────────────────────────────────────────────────────────────
class URLRequest(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    url: str
    format_id: Optional[str] = None
    task_id:   Optional[str] = None
    whatsapp:  Optional[bool] = False


# ── Quality presets ────────────────────────────────────────────────────────────
PRESETS = [
    {
        "format_id":  "bestvideo[ext=mp4][height<=2160]+bestaudio[ext=m4a]/bestvideo[height<=2160]+bestaudio/bestvideo+bestaudio/best",
        "label": "4K / Best",     "desc": "Highest possible resolution",
        "tag":   "4K",            "tag_color": "gold",
    },
    {
        "format_id":  "bestvideo[ext=mp4][height<=1440]+bestaudio[ext=m4a]/bestvideo[height<=1440]+bestaudio/best",
        "label": "1440p (2K)",    "desc": "Ultra-sharp, great for large screens",
        "tag":   "2K",            "tag_color": "purple",
    },
    {
        "format_id":  "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
        "label": "1080p Full HD", "desc": "Best all-round quality",
        "tag":   "FHD",           "tag_color": "blue",
    },
    {
        "format_id":  "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best[height<=720]/best",
        "label": "720p HD",       "desc": "Good quality, smaller file",
        "tag":   "HD",            "tag_color": "green",
    },
    {
        "format_id":  "bestvideo[ext=mp4][height<=480]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best[height<=480]/best",
        "label": "480p SD",       "desc": "Compact size, decent quality",
        "tag":   "SD",            "tag_color": "orange",
    },
    {
        "format_id":  "bestvideo[ext=mp4][height<=360]+bestaudio[ext=m4a]/bestvideo[height<=360]+bestaudio/best[height<=360]/best",
        "label": "360p Low",      "desc": "Smallest video file",
        "tag":   "LOW",           "tag_color": "gray",
    },
    {
        "format_id":  "bestaudio[ext=m4a]/bestaudio/best",
        "label": "Audio Only (M4A)", "desc": "Best audio, no video",
        "tag":   "AAC",           "tag_color": "teal",
    },
    {
        "format_id":  "bestaudio/best",
        "label": "Audio Only (MP3)", "desc": "Universal MP3, 320kbps",
        "tag":   "MP3",           "tag_color": "teal",
        "audio_only": True,
    },
]


def _common_opts() -> dict:
    opts: dict = {
        "quiet":          True,
        "no_warnings":    True,
        "extractor_args": {"youtube": {"player_client": ["android_vr", "tv_simply"]}},
    }
    if COOKIES_FROM_BROWSER:
        opts["cookiesfrombrowser"] = (COOKIES_FROM_BROWSER,)
    elif HAS_COOKIES:
        opts["cookiefile"] = str(COOKIES_FILE)
    if PROXY_URL:
        opts["proxy"] = PROXY_URL
    return opts


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.get("/")
async def index():
    return FileResponse(str(BASE_DIR / "static" / "index.html"))


@app.post("/api/info")
async def get_info(req: URLRequest):
    opts = {**_common_opts(), "skip_download": True}
    try:
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, _fetch_info, req.url, opts)
        return data
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(400, detail=str(e))
    except Exception as e:
        raise HTTPException(500, detail=str(e))


def _fetch_info(url: str, opts: dict) -> dict:
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.sanitize_info(ydl.extract_info(url, download=False))

    formats, seen = [], set()
    for f in info.get("formats", []):
        vc = f.get("vcodec", "none")
        ac = f.get("acodec", "none")
        if vc == "none" and ac == "none":
            continue
        h   = f.get("height")
        ext = f.get("ext", "")
        fps = f.get("fps")
        tbr = f.get("tbr")
        sz  = f.get("filesize") or f.get("filesize_approx")

        has_v, has_a = vc != "none", ac != "none"
        if has_v and has_a:
            fps_s = f" {int(fps)}fps" if fps and fps > 30 else ""
            label, kind = f"{h}p{fps_s} · {ext} · video+audio", "combined"
        elif has_v:
            fps_s = f" {int(fps)}fps" if fps and fps > 30 else ""
            label, kind = f"{h}p{fps_s} · {ext} · video", "video"
        else:
            btr_s = f" · {int(tbr)}kbps" if tbr else ""
            label, kind = f"{ext} audio{btr_s}", "audio"

        key = (h, ext, kind, fps)
        if key in seen:
            continue
        seen.add(key)
        formats.append({
            "format_id": f.get("format_id", ""), "label": label, "kind": kind,
            "height": h, "fps": fps, "ext": ext, "tbr": tbr,
            "size": f"{sz/(1024*1024):.1f} MB" if sz else "",
        })

    formats.sort(key=lambda x: (
        {"combined": 0, "video": 1, "audio": 2}.get(x["kind"], 3),
        -(x["height"] or 0), -(x["fps"] or 0), -(x["tbr"] or 0),
    ))
    max_h = max((f.get("height") or 0 for f in info.get("formats", [])), default=0)

    return {
        "title":      info.get("title", ""),
        "thumbnail":  info.get("thumbnail", ""),
        "duration":   info.get("duration_string", ""),
        "uploader":   info.get("uploader", ""),
        "view_count": info.get("view_count"),
        "max_height": max_h,
        "presets":    PRESETS,
        "formats":    formats[:60],
    }


@app.post("/api/download/start")
async def start_download(req: DownloadRequest):
    task_id = req.task_id or str(uuid.uuid4())
    tasks[task_id] = {
        "status": "starting", "progress": 0,
        "filename": None, "error": None,
        "title": "", "speed": "", "eta": "", "filesize": "",
    }
    fmt = req.format_id or PRESETS[2]["format_id"]
    asyncio.create_task(_run(task_id, req.url, fmt, bool(req.whatsapp)))
    return {"task_id": task_id}


async def _run(task_id: str, url: str, fmt: str, whatsapp: bool):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _download, task_id, url, fmt, whatsapp)


def _download(task_id: str, url: str, fmt: str, whatsapp: bool):
    work_dir = DOWNLOAD_DIR / task_id
    work_dir.mkdir(exist_ok=True)

    # MP3 preset uses audio-only format
    is_mp3 = PRESETS[-1]["format_id"] == fmt and PRESETS[-1].get("audio_only")

    if whatsapp:
        dl_fmt = "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best[height<=720]/best"
    elif is_mp3:
        dl_fmt = "bestaudio/best"
    else:
        dl_fmt = fmt

    def on_progress(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done  = d.get("downloaded_bytes", 0)
            pct   = round(done / total * 80, 1) if total else 0
            tasks[task_id].update({
                "status":   "downloading",
                "progress": pct,
                "speed":    d.get("_speed_str", "").strip(),
                "eta":      d.get("_eta_str",   "").strip(),
                "filesize": f"{total/1048576:.1f} MB" if total else "",
            })
        elif d["status"] == "finished":
            tasks[task_id].update({"status": "encoding", "progress": 80, "speed": "", "eta": ""})
        elif d["status"] == "error":
            tasks[task_id].update({"status": "error", "error": "Download failed"})

    def on_postprocess(d):
        if   d["status"] == "started":  tasks[task_id].update({"status": "encoding", "progress": 85})
        elif d["status"] == "finished": tasks[task_id].update({"status": "encoding", "progress": 98})

    opts = {
        **_common_opts(),
        "format":               dl_fmt,
        "outtmpl":              str(work_dir / "%(title).120s.%(ext)s"),
        "progress_hooks":       [on_progress],
        "postprocessor_hooks":  [on_postprocess],
        "merge_output_format":  "mp4",
        "postprocessors":       [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}],
        "postprocessor_args":   {
            "ffmpeg": [
                "-vcodec", "libx264",
                "-acodec", "aac",
                "-crf",    "28" if whatsapp else "23",
                "-preset", "fast",
                "-vf",     "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                "-movflags", "+faststart",
            ] + (["-b:a", "128k"] if whatsapp else [])
        },
        "prefer_ffmpeg": True,
        "keepvideo":     False,
    }

    if is_mp3:
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"}]
        opts.pop("merge_output_format", None)
        del opts["postprocessor_args"]

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            tasks[task_id]["title"] = info.get("title", "")

        files = [f for f in work_dir.glob("*") if f.suffix not in (".part", ".ytdl")]
        if files:
            best = max(files, key=lambda f: f.stat().st_size)
            tasks[task_id].update({
                "status":   "done",
                "progress": 100,
                "filename": best.name,
                "filesize": f"{best.stat().st_size/1048576:.1f} MB",
            })
        else:
            tasks[task_id].update({"status": "error", "error": "No file produced"})
    except Exception as e:
        tasks[task_id].update({"status": "error", "error": str(e)})


@app.get("/api/download/status/{task_id}")
async def get_status(task_id: str):
    if task_id not in tasks:
        raise HTTPException(404, "Task not found")
    return tasks[task_id]


@app.get("/api/download/file/{task_id}")
async def get_file(task_id: str):
    task = tasks.get(task_id)
    if not task or task["status"] != "done":
        raise HTTPException(400, "File not ready")
    path = DOWNLOAD_DIR / task_id / task["filename"]
    if not path.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(str(path), filename=task["filename"], media_type="application/octet-stream")


app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
