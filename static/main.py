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


# --- Конфігурація ---
DOWNLOADS_DIR = os.getenv("DOWNLOADS_DIR", "./downloads")
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", "157286400"))  # 150MB
TEMP_DIR = os.getenv("TEMP_DIR", "./temp")
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8001"))

# кеши та TTL
SEARCH_CACHE_TTL = 600        # секунди (10 хв)
DOWNLOAD_CACHE_TTL = 24 * 3600  # секунди (24 години)
search_cache = {}   # key -> (timestamp, results_list)
download_cache = {} # video_id -> (timestamp, filepath)

# локальні блоки для потокобезпеки
search_lock = asyncio.Lock()
download_lock = asyncio.Lock()

# Створення директорій (включно зі static)
Path(DOWNLOADS_DIR).mkdir(parents=True, exist_ok=True)
Path(TEMP_DIR).mkdir(parents=True, exist_ok=True)
Path("static").mkdir(parents=True, exist_ok=True)

# Логування
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("music-finder")

# Ініціалізація FastAPI
app = FastAPI(
    title="Music Finder",
    description="Пошук і завантаження музики з YouTube",
    version="2.0.0"
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Монтування статичних файлів
app.mount("/static", StaticFiles(directory="static"), name="static")

# Pydantic моделі
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

# --- Допоміжні функції ---
def _safe_title(title: str, max_len: int = 120) -> str:
    # простий санітайзер і обрізка
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
    """Синхронний пошук (запускається в thread через asyncio.to_thread)."""
    try:
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": "in_playlist",
            "default_search": "ytsearch",
            "socket_timeout": 30,
            # деякі опції для швидшого парсингу:
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
    key = f"{query}|{max_results}"
    async with search_lock:
        cached = search_cache.get(key)
        if cached and _is_cache_valid(cached[0], SEARCH_CACHE_TTL):
            return cached[1]
    # виконуємо блокуючу роботу в окремому потоці
    results = await asyncio.to_thread(search_youtube, query, max_results)
    async with search_lock:
        search_cache[key] = (time.time(), results)
    return results

def download_audio_sync(video_id: str, title: str) -> Tuple[str, str]:
    """Блокуючий завантажувач. Працює в thread."""
    try:
        # Перевірка кеша файлової системи — це робиться в обгортці
        download_id = str(uuid.uuid4())
        safe_title = _safe_title(title)
        out_template = os.path.join(DOWNLOADS_DIR, f"{safe_title}_{download_id}.%(ext)s")
        # Параметри yt_dlp оптимізовані для швидшого отримання аудіо (без зайвого логування)
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
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
            },
            # параметри для кращої роботи з фрагментами (якщо відео HLS)
            "concurrent_fragment_downloads": 4,
            "fragment_retries": 3,
            "retries": 3,
            "noplaylist": True,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={video_id}"])

        # знайдемо створений mp3
        final_file = None
        for f in os.listdir(DOWNLOADS_DIR):
            if download_id in f and f.lower().endswith(".mp3"):
                final_file = os.path.join(DOWNLOADS_DIR, f)
                break

        if not final_file:
            raise Exception("Не знайдено кінцевий MP3 файл після завантаження/конвертації")

        file_size = os.path.getsize(final_file)
        if file_size > MAX_FILE_SIZE:
            os.remove(final_file)
            raise HTTPException(status_code=413, detail=f"Файл занадто великий ({file_size / 1024 / 1024:.2f} MB)")

        return final_file, os.path.basename(final_file)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("download_audio_sync error")
        raise

async def download_audio(video_id: str, title: str) -> Tuple[str, str]:
    """Асинх. обгортка для завантаження з кешуванням."""
    # перевіряємо кеш (за video_id)
    async with download_lock:
        cached = download_cache.get(video_id)
        if cached and _is_cache_valid(cached[0], DOWNLOAD_CACHE_TTL) and os.path.exists(cached[1]):
            # обновимо час кешу
            download_cache[video_id] = (time.time(), cached[1])
            return cached[1], os.path.basename(cached[1])

    # Інакше — завантажуємо в окремому потоці
    file_path, filename = await asyncio.to_thread(download_audio_sync, video_id, title)

    async with download_lock:
        download_cache[video_id] = (time.time(), file_path)

    return file_path, filename

def cleanup_old_files_once():
    """Синхронна очистка старих файлів (запускається в потоках)."""
    try:
        current_time = time.time()
        for filename in os.listdir(DOWNLOADS_DIR):
            filepath = os.path.join(DOWNLOADS_DIR, filename)
            if os.path.isfile(filepath):
                file_age = current_time - os.path.getmtime(filepath)
                if file_age > DOWNLOAD_CACHE_TTL:
                    try:
                        os.remove(filepath)
                        logger.info(f"Removed old file: {filepath}")
                    except Exception as e:
                        logger.exception(f"Failed to remove {filepath}")
        # чистимо кеши, які вказують на неіснуючі файли або протерміновані
        with (download_lock._loop if hasattr(download_lock, "_loop") else dummy_context()):
            # заради простоти — оновимо словник у синхронному режимі
            to_del = []
            for vid, (ts, path) in list(download_cache.items()):
                if not os.path.exists(path) or not _is_cache_valid(ts, DOWNLOAD_CACHE_TTL):
                    to_del.append(vid)
            for vid in to_del:
                download_cache.pop(vid, None)
    except Exception:
        logger.exception("cleanup_old_files_once error")

async def periodic_cleanup():
    """Асинхронний цикл для фонового періодичного запуску cleanup."""
    while True:
        try:
            await asyncio.to_thread(cleanup_old_files_once)
        except Exception:
            logger.exception("periodic_cleanup error")
        await asyncio.sleep(3600)  # кожну годину

# --- API Endpoints ---
@app.get("/")
async def root():
    """Повертає static/index.html або fallback HTML."""
    index_path = Path("static") / "index.html"
    if index_path.exists():
        content = index_path.read_text(encoding="utf-8")
        return HTMLResponse(content=content)
    # fallback (якщо нема index.html)
    return HTMLResponse("""
    <html>
      <head><meta charset="utf-8"><title>Music Finder</title></head>
      <body>
        <h3>Music Finder</h3>
        <p>Сервіс працює. Додайте static/index.html у папку <code>static/</code> щоб замінити цю сторінку.</p>
      </body>
    </html>
    """)

@app.get("/health")
async def health_check():
    """Перевірка здоров'я сервісу"""
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "downloads_dir": DOWNLOADS_DIR
    }

@app.post("/search", response_model=SearchResponse)
async def search_music(request: SearchQuery):
    """Пошук музики на YouTube (з кешуванням)."""
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
        raise HTTPException(status_code=500, detail=f"Помилка пошуку: {str(e)}")

@app.post("/download")
async def download_music(request: DownloadRequest):
    """Завантажує музику в MP3 з YouTube (не блокує event loop)."""
    try:
        file_path, filename = await download_audio(request.video_id, request.title)
        # Повертаємо файл без негайного видалення — фонова очистка позбавиться старих файлів
        return FileResponse(
            file_path,
            media_type="audio/mpeg",
            filename=f"{request.title}.mp3"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("download_music error")
        raise HTTPException(status_code=500, detail=f"Помилка завантаження: {str(e)}")

@app.post("/batch-download")
async def batch_download(requests: List[DownloadRequest]):
    """Завантажує кілька музичних файлів одночасно (до 5)."""
    if len(requests) > 5:
        raise HTTPException(status_code=400, detail="Максимум 5 файлів за один запит")
    results = []
    errors = []
    for req in requests:
        try:
            file_path, filename = await download_audio(req.video_id, req.title)
            results.append({
                "title": req.title,
                "status": "success",
                "filename": filename,
                "path": file_path
            })
        except Exception as e:
            logger.exception("batch item error")
            errors.append({
                "title": req.title,
                "status": "error",
                "error": str(e)
            })
    return {
        "successful": len(results),
        "failed": len(errors),
        "results": results,
        "errors": errors
    }

# Фонові події
@app.on_event("startup")
async def startup_event():
    # запуск періодичної очистки в фоні
    asyncio.create_task(periodic_cleanup())
    # одинразова негайна очистка (щоб позбутися старих файлів при старті)
    await asyncio.to_thread(cleanup_old_files_once)
    logger.info("Startup complete. Background cleanup started.")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
