# Video Downloader

A web app for downloading videos, powered by [yt-dlp](https://github.com/yt-dlp/yt-dlp) — supports thousands of sites including YouTube, Vimeo, Twitter/X, Instagram, and many more.

## Requirements

- Python 3.10+
- `ffmpeg` (required for merging video + audio streams)

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Install ffmpeg (macOS)
brew install ffmpeg

# 3. Run the server
uvicorn main:app --reload --port 8000
```

Then open **http://localhost:8000** in your browser.

## Features

- Paste any video URL and fetch info (title, thumbnail, duration)
- Choose from recommended quality presets or browse all available formats
- Real-time download progress bar with speed and ETA
- One-click save to your computer
- Supports thousands of sites via yt-dlp

## Project Structure

```
VideoDownloader/
├── main.py          # FastAPI backend
├── requirements.txt
├── static/
│   └── index.html   # Frontend (single-file UI)
└── downloads/       # Created automatically at runtime
```
