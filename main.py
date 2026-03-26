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

# Use /tmp on Railway (ephemeral but writable); falls back to local ./downloads
DOWNLOAD_DIR = Path("/tmp/vdl_downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# In-memory store for task progress
tasks: Dict[str, dict] = {}


class URLRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    format_id: Optional[str] = "bestvideo+bestaudio/best"
    task_id: Optional[str] = None


def sanitize_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", name)


def get_info_opts():
    return {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "skip_download": True,
    }


@app.get("/")
async def index():
    return FileResponse(str(BASE_DIR / "static" / "index.html"))


@app.post("/api/info")
async def get_video_info(req: URLRequest):
    try:
        with yt_dlp.YoutubeDL(get_info_opts()) as ydl:
            info = ydl.extract_info(req.url, download=False)
            info = ydl.sanitize_info(info)

        formats = []
        seen = set()
        for f in info.get("formats", []):
            fid = f.get("format_id", "")
            height = f.get("height")
            ext = f.get("ext", "")
            vcodec = f.get("vcodec", "none")
            acodec = f.get("acodec", "none")
            note = f.get("format_note", "")
            filesize = f.get("filesize") or f.get("filesize_approx")

            if vcodec == "none" and acodec == "none":
                continue

            has_video = vcodec != "none"
            has_audio = acodec != "none"

            if has_video and has_audio:
                label = f"{height}p {ext} (video+audio)" if height else f"{ext} (video+audio)"
                kind = "combined"
            elif has_video:
                label = f"{height}p {ext} (video only)" if height else f"{ext} (video only)"
                kind = "video"
            else:
                label = f"{ext} audio ({note})" if note else f"{ext} audio"
                kind = "audio"

            key = (height, ext, kind)
            if key in seen:
                continue
            seen.add(key)

            size_str = ""
            if filesize:
                mb = filesize / (1024 * 1024)
                size_str = f"~{mb:.1f} MB"

            formats.append({
                "format_id": fid,
                "label": label,
                "kind": kind,
                "height": height,
                "ext": ext,
                "size": size_str,
            })

        formats.sort(key=lambda x: (
            x["kind"] != "combined",
            x["kind"] != "video",
            -(x["height"] or 0),
        ))

        preset_formats = [
            {"format_id": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best", "label": "Best Quality (auto-merge)", "kind": "best", "height": None, "ext": "mp4", "size": ""},
            {"format_id": "bestvideo[height<=1080]+bestaudio/best[height<=1080]", "label": "1080p (best available)", "kind": "best", "height": 1080, "ext": "mp4", "size": ""},
            {"format_id": "bestvideo[height<=720]+bestaudio/best[height<=720]", "label": "720p (best available)", "kind": "best", "height": 720, "ext": "mp4", "size": ""},
            {"format_id": "bestvideo[height<=480]+bestaudio/best[height<=480]", "label": "480p (best available)", "kind": "best", "height": 480, "ext": "mp4", "size": ""},
            {"format_id": "bestaudio/best", "label": "Audio only (best quality)", "kind": "audio", "height": None, "ext": "m4a", "size": ""},
        ]

        return {
            "title": info.get("title", "Unknown"),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration_string", ""),
            "uploader": info.get("uploader", ""),
            "view_count": info.get("view_count"),
            "preset_formats": preset_formats,
            "formats": formats[:40],
        }
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch video info: {str(e)}")


@app.post("/api/download/start")
async def start_download(req: DownloadRequest):
    task_id = req.task_id or str(uuid.uuid4())
    tasks[task_id] = {"status": "starting", "progress": 0, "filename": None, "error": None, "title": ""}

    asyncio.create_task(_run_download(task_id, req.url, req.format_id))
    return {"task_id": task_id}


async def _run_download(task_id: str, url: str, format_id: str):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _download_sync, task_id, url, format_id)


def _download_sync(task_id: str, url: str, format_id: str):
    output_path = DOWNLOAD_DIR / task_id
    output_path.mkdir(exist_ok=True)
    output_template = str(output_path / "%(title).100s.%(ext)s")

    def progress_hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            downloaded = d.get("downloaded_bytes", 0)
            percent = (downloaded / total * 100) if total else 0
            speed = d.get("_speed_str", "")
            eta = d.get("_eta_str", "")
            tasks[task_id].update({
                "status": "downloading",
                "progress": round(percent, 1),
                "speed": speed,
                "eta": eta,
            })
        elif d["status"] == "finished":
            tasks[task_id].update({"status": "processing", "progress": 99})
        elif d["status"] == "error":
            tasks[task_id].update({"status": "error", "error": "Download failed"})

    ydl_opts = {
        "format": format_id,
        "outtmpl": output_template,
        "progress_hooks": [progress_hook],
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title", "video")
            tasks[task_id]["title"] = title

        files = list(output_path.glob("*"))
        if files:
            tasks[task_id].update({
                "status": "done",
                "progress": 100,
                "filename": files[0].name,
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
