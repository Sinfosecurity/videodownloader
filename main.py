from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path
from typing import Dict, Optional

BASE_DIR = Path(__file__).parent

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI(title="Video Downloader")

# Use /tmp on Railway (ephemeral but writable)
DOWNLOAD_DIR = Path("/tmp/vdl_downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# In-memory store for task progress
tasks: Dict[str, dict] = {}


class URLRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    format_id: Optional[str] = None
    task_id: Optional[str] = None


def get_info_opts():
    return {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "skip_download": True,
        # android_vr + tv avoids YouTube SABR streaming restrictions entirely
        "extractor_args": {
            "youtube": {
                "player_client": ["android_vr", "tv", "web"],
            }
        },
    }


# Quality presets — ordered best to worst, all output as MP4
PRESETS = [
    {
        "format_id": "bestvideo[ext=mp4][height<=2160]+bestaudio[ext=m4a]/bestvideo[height<=2160]+bestaudio/bestvideo+bestaudio/best",
        "label": "4K / Best Available",
        "desc": "Highest possible resolution",
        "tag": "4K",
        "tag_color": "gold",
    },
    {
        "format_id": "bestvideo[ext=mp4][height<=1440]+bestaudio[ext=m4a]/bestvideo[height<=1440]+bestaudio/bestvideo+bestaudio/best",
        "label": "1440p (2K)",
        "desc": "Ultra-sharp, great for large screens",
        "tag": "2K",
        "tag_color": "purple",
    },
    {
        "format_id": "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
        "label": "1080p Full HD",
        "desc": "Best all-round quality",
        "tag": "FHD",
        "tag_color": "blue",
    },
    {
        "format_id": "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best[height<=720]/best",
        "label": "720p HD",
        "desc": "Good quality, smaller file",
        "tag": "HD",
        "tag_color": "green",
    },
    {
        "format_id": "bestvideo[ext=mp4][height<=480]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best[height<=480]/best",
        "label": "480p SD",
        "desc": "Compact size, decent quality",
        "tag": "SD",
        "tag_color": "orange",
    },
    {
        "format_id": "bestvideo[ext=mp4][height<=360]+bestaudio[ext=m4a]/bestvideo[height<=360]+bestaudio/best[height<=360]/best",
        "label": "360p Low",
        "desc": "Smallest video file",
        "tag": "LOW",
        "tag_color": "gray",
    },
    {
        "format_id": "bestaudio[ext=m4a]/bestaudio/best",
        "label": "Audio Only (M4A)",
        "desc": "Best audio quality, no video",
        "tag": "AAC",
        "tag_color": "teal",
    },
    {
        "format_id": "bestaudio/best --extract-audio --audio-format mp3",
        "label": "Audio Only (MP3)",
        "desc": "Universal MP3 audio, 320kbps",
        "tag": "MP3",
        "tag_color": "teal",
        "audio_only": True,
        "audio_format": "mp3",
    },
]


@app.get("/")
async def index():
    return FileResponse(str(BASE_DIR / "static" / "index.html"))


@app.post("/api/info")
async def get_video_info(req: URLRequest):
    try:
        with yt_dlp.YoutubeDL(get_info_opts()) as ydl:
            info = ydl.extract_info(req.url, download=False)
            info = ydl.sanitize_info(info)

        # Build available-format list for "All Formats" tab
        formats = []
        seen = set()
        for f in info.get("formats", []):
            fid = f.get("format_id", "")
            height = f.get("height")
            width = f.get("width")
            ext = f.get("ext", "")
            vcodec = f.get("vcodec", "none")
            acodec = f.get("acodec", "none")
            fps = f.get("fps")
            tbr = f.get("tbr")
            note = f.get("format_note", "")
            filesize = f.get("filesize") or f.get("filesize_approx")

            if vcodec == "none" and acodec == "none":
                continue

            has_video = vcodec != "none"
            has_audio = acodec != "none"

            if has_video and has_audio:
                res = f"{height}p" if height else ext
                fps_str = f" {int(fps)}fps" if fps and fps > 30 else ""
                label = f"{res}{fps_str} · {ext} · video+audio"
                kind = "combined"
            elif has_video:
                res = f"{height}p" if height else ext
                fps_str = f" {int(fps)}fps" if fps and fps > 30 else ""
                bitrate = f" · {int(tbr)}kbps" if tbr else ""
                label = f"{res}{fps_str}{bitrate} · {ext} · video"
                kind = "video"
            else:
                bitrate = f" · {int(tbr)}kbps" if tbr else ""
                label = f"{ext} audio{bitrate}" + (f" · {note}" if note else "")
                kind = "audio"

            key = (height, ext, kind, fps)
            if key in seen:
                continue
            seen.add(key)

            size_str = ""
            if filesize:
                mb = filesize / (1024 * 1024)
                size_str = f"{mb:.1f} MB"

            formats.append({
                "format_id": fid,
                "label": label,
                "kind": kind,
                "height": height,
                "fps": fps,
                "ext": ext,
                "size": size_str,
                "tbr": tbr,
            })

        # Sort: combined first, then by resolution desc, then fps desc
        formats.sort(key=lambda x: (
            {"combined": 0, "video": 1, "audio": 2}.get(x["kind"], 3),
            -(x["height"] or 0),
            -(x["fps"] or 0),
            -(x["tbr"] or 0),
        ))

        # Detect max available resolution to highlight supported presets
        max_height = max((f.get("height") or 0 for f in info.get("formats", [])), default=0)

        return {
            "title": info.get("title", "Unknown"),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration_string", ""),
            "uploader": info.get("uploader", ""),
            "view_count": info.get("view_count"),
            "like_count": info.get("like_count"),
            "max_height": max_height,
            "presets": PRESETS,
            "formats": formats[:60],
        }
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch video info: {str(e)}")


@app.post("/api/download/start")
async def start_download(req: DownloadRequest):
    task_id = req.task_id or str(uuid.uuid4())
    tasks[task_id] = {
        "status": "starting",
        "progress": 0,
        "filename": None,
        "error": None,
        "title": "",
        "speed": "",
        "eta": "",
        "filesize": "",
    }
    asyncio.create_task(_run_download(task_id, req.url, req.format_id or PRESETS[2]["format_id"]))
    return {"task_id": task_id}


async def _run_download(task_id: str, url: str, format_id: str):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _download_sync, task_id, url, format_id)


def _download_sync(task_id: str, url: str, format_id: str):
    output_path = DOWNLOAD_DIR / task_id
    output_path.mkdir(exist_ok=True)
    output_template = str(output_path / "%(title).120s [%(height)sp].%(ext)s")

    # Check if this is an MP3 preset (special handling)
    is_mp3 = "mp3" in format_id
    clean_format = "bestaudio/best" if is_mp3 else format_id

    def progress_hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            downloaded = d.get("downloaded_bytes", 0)
            percent = (downloaded / total * 100) if total else 0
            speed = d.get("_speed_str", "").strip()
            eta = d.get("_eta_str", "").strip()
            size = ""
            if total:
                mb = total / (1024 * 1024)
                size = f"{mb:.1f} MB"
            tasks[task_id].update({
                "status": "downloading",
                "progress": round(percent, 1),
                "speed": speed,
                "eta": eta,
                "filesize": size,
            })
        elif d["status"] == "finished":
            tasks[task_id].update({"status": "processing", "progress": 99, "eta": ""})
        elif d["status"] == "error":
            tasks[task_id].update({"status": "error", "error": "Download failed during transfer"})

    ydl_opts = {
        "format": clean_format,
        "outtmpl": output_template,
        "progress_hooks": [progress_hook],
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
        # android_vr + tv avoids YouTube SABR streaming restrictions entirely
        "extractor_args": {
            "youtube": {
                "player_client": ["android_vr", "tv", "web"],
            }
        },
        # ffmpeg: web-optimised MP4 (moov atom at front for fast streaming)
        "postprocessor_args": {
            "ffmpeg": ["-movflags", "+faststart"]
        },
        "prefer_ffmpeg": True,
        "keepvideo": False,
        "remux_video": "mp4",
    }

    if is_mp3:
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "320",  # 320kbps MP3
        }]
        ydl_opts.pop("merge_output_format", None)
        ydl_opts.pop("remux_video", None)
        # Use simpler output template for audio
        ydl_opts["outtmpl"] = str(output_path / "%(title).120s.%(ext)s")

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title", "video")
            tasks[task_id]["title"] = title

        files = [f for f in output_path.glob("*") if f.suffix not in (".part", ".ytdl")]
        if files:
            # Pick the largest file (the final merged output)
            best = max(files, key=lambda f: f.stat().st_size)
            tasks[task_id].update({
                "status": "done",
                "progress": 100,
                "filename": best.name,
                "filesize": f"{best.stat().st_size / (1024*1024):.1f} MB",
            })
        else:
            tasks[task_id].update({"status": "error", "error": "File not found after download"})
    except Exception as e:
        tasks[task_id].update({"status": "error", "error": str(e)})


@app.get("/api/download/status/{task_id}")
async def get_status(task_id: str):
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    return tasks[task_id]


@app.get("/api/download/file/{task_id}")
async def serve_file(task_id: str):
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Task not found")

    task = tasks[task_id]
    if task["status"] != "done" or not task.get("filename"):
        raise HTTPException(status_code=400, detail="File not ready")

    file_path = DOWNLOAD_DIR / task_id / task["filename"]
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found on disk")

    return FileResponse(
        path=str(file_path),
        filename=task["filename"],
        media_type="application/octet-stream",
    )


app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
