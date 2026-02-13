from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import List, Optional, Tuple
import yt_dlp
import os
from datetime import datetime
import asyncio
from pathlib import Path
import uuid
import time
import logging
from contextlib import asynccontextmanager

# --- Configuration ---
DOWNLOADS_DIR = os.getenv("DOWNLOADS_DIR", "./downloads")
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", "157286400"))  # 150MB
TEMP_DIR = os.getenv("TEMP_DIR", "./temp")
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8001"))

# Cache and TTL
SEARCH_CACHE_TTL = 600        # 10 minutes
DOWNLOAD_CACHE_TTL = 24 * 3600  # 24 hours
search_cache = {}   # key -> (timestamp, results_list)
download_cache = {} # video_id -> (timestamp, filepath)

# Locks for thread safety
search_lock = asyncio.Lock()
download_lock = asyncio.Lock()

# Ensure directories exist
Path(DOWNLOADS_DIR).mkdir(parents=True, exist_ok=True)
Path(TEMP_DIR).mkdir(parents=True, exist_ok=True)
Path("static").mkdir(parents=True, exist_ok=True)

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("music-finder")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: start background cleanup task
    cleanup_task = asyncio.create_task(periodic_cleanup())
    # Run one cleanup immediately
    await asyncio.to_thread(cleanup_old_files_once)
    logger.info("Startup complete. Background cleanup started.")
    yield
    # Shutdown: cancel task
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass
    logger.info("Shutdown complete.")

# Initialize FastAPI
app = FastAPI(
    title="Music Finder",
    description="Search and download music from YouTube",
    version="2.1.0",
    lifespan=lifespan
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# Pydantic Models
class SearchQuery(BaseModel):
    query: str = Field(..., min_length=1, max_length=200)
    limit: int = Field(default=10, ge=1, le=50)

class SearchResult(BaseModel):
    id: str
    title: str
    artist: str
    duration: int
    thumbnail: str
    url: str

class DownloadRequest(BaseModel):
    video_id: str
    title: str

class SearchResponse(BaseModel):
    query: str
    total_results: int
    results: List[SearchResult]
    timestamp: datetime

# --- Helper Functions ---
def _safe_title(title: str, max_len: int = 120) -> str:
    """Sanitize and trim title for filenames."""
    safe = "".join(c for c in title if c.isalnum() or c in (" ", "-", "_")).rstrip()
    safe = safe[:max_len].strip()
    if not safe:
        safe = "track"
    return safe.replace(" ", "_")

def _is_cache_valid(entry_timestamp: float, ttl: int) -> bool:
    return (time.time() - entry_timestamp) < ttl

def _build_search_result(video: dict) -> SearchResult:
    return SearchResult(
        id=video.get("id"),
        title=video.get("title", "Unknown"),
        artist=video.get("uploader", "Unknown Artist"),
        duration=video.get("duration", 0) or 0,
        thumbnail=video.get("thumbnail", ""),
        url=f"https://www.youtube.com/watch?v={video.get('id')}"
    )

def search_youtube(query: str, max_results: int = 10) -> List[SearchResult]:
    """Synchronous search function (run in thread via asyncio.to_thread)."""
    try:
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": "in_playlist",
            "default_search": "ytsearch",
            "socket_timeout": 30,
            "skip_download": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch{max_results}:{query}", download=False)
        results = []
        for video in info.get("entries", [])[:max_results]:
            results.append(_build_search_result(video))
        return results
    except Exception as e:
        logger.exception("search_youtube error")
        raise

async def cached_search(query: str, max_results: int = 10) -> List[SearchResult]:
    """Async wrapper for search with caching."""
    key = f"{query}|{max_results}"
    async with search_lock:
        cached = search_cache.get(key)
        if cached and _is_cache_valid(cached[0], SEARCH_CACHE_TTL):
            return cached[1]
    
    # Perform blocking work in a separate thread
    results = await asyncio.to_thread(search_youtube, query, max_results)
    
    async with search_lock:
        search_cache[key] = (time.time(), results)
    return results

def download_audio_sync(video_id: str, title: str) -> Tuple[str, str]:
    """Blocking audio downloader. Runs in thread."""
    try:
        download_id = str(uuid.uuid4())
        safe_title = _safe_title(title)
        out_template = os.path.join(DOWNLOADS_DIR, f"{safe_title}_{download_id}.%(ext)s")
        
        ydl_opts = {
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
            "outtmpl": out_template,
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 60,
            "nocheckcertificate": True,
            "geo_bypass": True,
            "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
            "concurrent_fragment_downloads": 4,
            "fragment_retries": 15,
            "retries": 15,
            "noplaylist": True,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={video_id}"])

        # Find the created MP3 file
        final_file = None
        for f in os.listdir(DOWNLOADS_DIR):
            if download_id in f and f.lower().endswith(".mp3"):
                final_file = os.path.join(DOWNLOADS_DIR, f)
                break

        if not final_file:
            raise Exception("MP3 file not found after download/conversion")

        file_size = os.path.getsize(final_file)
        if file_size > MAX_FILE_SIZE:
            os.remove(final_file)
            raise HTTPException(status_code=413, detail=f"File too large ({file_size / 1024 / 1024:.2f} MB)")

        return final_file, os.path.basename(final_file)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("download_audio_sync error")
        raise

async def download_audio(video_id: str, title: str) -> Tuple[str, str]:
    """Async wrapper for download with caching."""
    async with download_lock:
        cached = download_cache.get(video_id)
        if cached and _is_cache_valid(cached[0], DOWNLOAD_CACHE_TTL) and os.path.exists(cached[1]):
            # Refresh cache timestamp
            download_cache[video_id] = (time.time(), cached[1])
            return cached[1], os.path.basename(cached[1])

    # Download in a separate thread
    file_path, filename = await asyncio.to_thread(download_audio_sync, video_id, title)

    async with download_lock:
        download_cache[video_id] = (time.time(), file_path)

    return file_path, filename

def cleanup_old_files_once():
    """Cleanup old files and invalid cache entries."""
    try:
        current_time = time.time()
        # Clean filesystem
        for filename in os.listdir(DOWNLOADS_DIR):
            filepath = os.path.join(DOWNLOADS_DIR, filename)
            if os.path.isfile(filepath):
                file_age = current_time - os.path.getmtime(filepath)
                if file_age > DOWNLOAD_CACHE_TTL:
                    try:
                        os.remove(filepath)
                        logger.info(f"Removed old file: {filepath}")
                    except Exception:
                        logger.exception(f"Failed to remove {filepath}")
        
        # Clean cache entries (doing it outside of async context for simplicity since it's a thread)
        # Note: We don't use the lock here as it's a thread and we want to avoid deadlocks.
        # We just iterate over a copy of the items.
        for vid, (ts, path) in list(download_cache.items()):
            if not os.path.exists(path) or not _is_cache_valid(ts, DOWNLOAD_CACHE_TTL):
                download_cache.pop(vid, None)
    except Exception:
        logger.exception("cleanup_old_files_once error")

async def periodic_cleanup():
    """Periodically run cleanup every hour."""
    while True:
        try:
            await asyncio.to_thread(cleanup_old_files_once)
        except Exception:
            logger.exception("periodic_cleanup error")
        await asyncio.sleep(3600)  # 1 hour

# --- API Endpoints ---
@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return JSONResponse(content="")

@app.get("/")
async def root():
    """Serves static/index.html or a fallback page."""
    index_path = Path("static") / "index.html"
    if index_path.exists():
        content = index_path.read_text(encoding="utf-8")
        return HTMLResponse(content=content)
    return HTMLResponse("""
    <html>
      <head><meta charset="utf-8"><title>Music Finder</title></head>
      <body>
        <h3>Music Finder</h3>
        <p>Service is running. Please add <code>static/index.html</code>.</p>
      </body>
    </html>
    """)

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "downloads_dir": DOWNLOADS_DIR
    }

@app.post("/search", response_model=SearchResponse)
async def search_music(request: SearchQuery):
    """Search for music on YouTube."""
    try:
        results = await cached_search(request.query, request.limit)
        return SearchResponse(
            query=request.query,
            total_results=len(results),
            results=results,
            timestamp=datetime.now()
        )
    except Exception as e:
        logger.exception("search_music error")
        raise HTTPException(status_code=500, detail=f"Search failed: {str(e)}")

@app.post("/download")
async def download_music(request: DownloadRequest):
    """Download audio in MP3 format."""
    try:
        file_path, filename = await download_audio(request.video_id, request.title)
        return FileResponse(
            file_path,
            media_type="audio/mpeg",
            filename=f"{request.title}.mp3"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("download_music error")
        raise HTTPException(status_code=500, detail=f"Download failed: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8001))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
