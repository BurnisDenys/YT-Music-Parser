import os
import logging
import asyncio
import time
import uuid
from pathlib import Path
from typing import List, Tuple, Optional, Dict
from datetime import datetime, timedelta
from collections import defaultdict
from dataclasses import dataclass, field

import yt_dlp
from flask import Flask, request, send_from_directory
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse

# --- Configuration ---
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "YOUR_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "YOUR_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")
TRIBUTE_ACCOUNT = os.getenv("TRIBUTE_ACCOUNT", "your_tribute_account")
DOWNLOADS_DIR = Path(os.getenv("DOWNLOADS_DIR", "./downloads"))
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", "16777216"))  # 16MB for WhatsApp
TEMP_DIR = Path(os.getenv("TEMP_DIR", "./temp"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://your-domain.com")
PORT = int(os.getenv("PORT", "5000"))

# Plans limits
FREE_PLAN = {
    "searches_per_month": 50,
    "downloads_per_month": 20,
    "name": "Free"
}

PRO_PLAN = {
    "searches_per_month": 500,
    "downloads_per_month": 200,
    "name": "Pro"
}

PREMIUM_PLAN = {
    "searches_per_month": float('inf'),
    "downloads_per_month": float('inf'),
    "name": "Premium"
}

SEARCH_CACHE_TTL = 600  # 10 minutes
DOWNLOAD_CACHE_TTL = 24 * 3600  # 24 hours

# Create directories
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR.mkdir(parents=True, exist_ok=True)

# Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Flask app
app = Flask(__name__)

# Twilio client
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# --- Data Models ---
@dataclass
class UserPlan:
    plan: str = "free"
    expires_at: Optional[datetime] = None
    
    def is_active(self) -> bool:
        if self.plan == "premium":
            return True
        if self.plan == "pro" and self.expires_at:
            return datetime.now() < self.expires_at
        return self.plan == "free"
    
    def get_limits(self) -> dict:
        if not self.is_active():
            return FREE_PLAN
        
        if self.plan == "premium":
            return PREMIUM_PLAN
        elif self.plan == "pro":
            return PRO_PLAN
        else:
            return FREE_PLAN

@dataclass
class UserStats:
    phone: str
    searches: int = 0
    downloads: int = 0
    searches_this_month: int = 0
    downloads_this_month: int = 0
    last_activity: Optional[datetime] = None
    month_reset_date: datetime = field(default_factory=datetime.now)
    plan: UserPlan = field(default_factory=UserPlan)
    
    def update_activity(self):
        self.last_activity = datetime.now()
        self._reset_monthly_if_needed()
    
    def _reset_monthly_if_needed(self):
        if (datetime.now() - self.month_reset_date).days >= 30:
            self.searches_this_month = 0
            self.downloads_this_month = 0
            self.month_reset_date = datetime.now()
    
    def can_search(self) -> bool:
        self._reset_monthly_if_needed()
        limits = self.plan.get_limits()
        return self.searches_this_month < limits["searches_per_month"]
    
    def can_download(self) -> bool:
        self._reset_monthly_if_needed()
        limits = self.plan.get_limits()
        return self.downloads_this_month < limits["downloads_per_month"]
    
    def add_search(self):
        self.searches += 1
        self.searches_this_month += 1
        self.update_activity()
    
    def add_download(self):
        self.downloads += 1
        self.downloads_this_month += 1
        self.update_activity()
    
    def get_remaining(self) -> dict:
        self._reset_monthly_if_needed()
        limits = self.plan.get_limits()
        return {
            "searches": limits["searches_per_month"] - self.searches_this_month,
            "downloads": limits["downloads_per_month"] - self.downloads_this_month,
            "limits": limits
        }

@dataclass
class SearchResult:
    id: str
    title: str
    artist: str
    duration: int
    thumbnail: str
    url: str

# --- Storage ---
user_stats = {}
search_cache = {}
download_cache = {}
user_sessions = {}

# --- Utilities ---
def _safe_filename(title: str, max_len: int = 100) -> str:
    """Sanitize filename"""
    safe = "".join(c for c in title if c.isalnum() or c in (" ", "-", "_")).rstrip()
    safe = safe[:max_len].strip()
    return safe.replace(" ", "_") if safe else "track"

def _is_cache_valid(entry_timestamp: float, ttl: int) -> bool:
    """Check cache validity"""
    return (time.time() - entry_timestamp) < ttl

def _format_duration(seconds: int) -> str:
    """Format duration"""
    if not seconds:
        return "N/A"
    seconds = int(seconds)
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}:{secs:02d}"

def get_user_stats(phone: str) -> UserStats:
    """Get or create user stats"""
    if phone not in user_stats:
        user_stats[phone] = UserStats(phone=phone)
    return user_stats[phone]

# --- YouTube Operations ---
class YouTubeService:
    @staticmethod
    def search(query: str, max_results: int = 5) -> List[SearchResult]:
        """Search YouTube"""
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
                info = ydl.extract_info(
                    f"ytsearch{max_results}:{query}",
                    download=False
                )
            
            results = []
            for video in info.get("entries", [])[:max_results]:
                results.append(SearchResult(
                    id=video.get("id"),
                    title=video.get("title", "Unknown"),
                    artist=video.get("uploader", "Unknown Artist"),
                    duration=video.get("duration", 0) or 0,
                    thumbnail=video.get("thumbnail", ""),
                    url=f"https://www.youtube.com/watch?v={video.get('id')}"
                ))
            return results
        except Exception as e:
            logger.exception("YouTube search error")
            raise
    
    @staticmethod
    def download_audio(video_id: str, title: str) -> Tuple[Path, str]:
        """Download audio from YouTube"""
        try:
            cached = download_cache.get(video_id)
            if cached and _is_cache_valid(cached[0], DOWNLOAD_CACHE_TTL):
                filepath = Path(cached[1])
                if filepath.exists():
                    download_cache[video_id] = (time.time(), cached[1])
                    return filepath, filepath.name
            
            download_id = str(uuid.uuid4())
            safe_title = YouTubeService._safe_filename(title)
            output_path = DOWNLOADS_DIR / f"{safe_title}_{download_id}.%(ext)s"
            
            ydl_opts = {
                "format": "bestaudio/best",
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "128",
                }],
                "outtmpl": str(output_path),
                "quiet": True,
                "no_warnings": True,
                "socket_timeout": 60,
                "http_headers": {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
                },
                "concurrent_fragment_downloads": 4,
                "fragment_retries": 3,
                "retries": 3,
                "noplaylist": True,
            }
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
            
            for filepath in DOWNLOADS_DIR.glob("*.mp3"):
                if download_id in filepath.name:
                    file_size = filepath.stat().st_size
                    if file_size > MAX_FILE_SIZE:
                        filepath.unlink()
                        raise Exception(
                            f"File too large ({file_size / 1024 / 1024:.2f} MB)"
                        )
                    download_cache[video_id] = (time.time(), str(filepath))
                    return filepath, filepath.name
            
            raise Exception("Downloaded file not found")
        except Exception as e:
            logger.exception("Download error")
            raise

# --- Message Handlers ---
def send_whatsapp_message(to: str, body: str, media_url: str = None):
    """Send WhatsApp message"""
    try:
        message_params = {
            "from_": TWILIO_WHATSAPP_NUMBER,
            "to": to,
            "body": body
        }
        
        if media_url:
            message_params["media_url"] = [media_url]
        
        message = twilio_client.messages.create(**message_params)
        logger.info(f"Message sent: {message.sid}")
        return message
    except Exception as e:
        logger.exception("Send message error")
        raise

def handle_start(from_number: str) -> str:
    """Handle start command"""
    stats = get_user_stats(from_number)
    stats.update_activity()
    
    return (
        "👋 *Привіт!*\n\n"
        "🎵 Я *Music Finder Bot* - твій музичний асистент у WhatsApp!\n\n"
        "✨ *Що я вмію:*\n"
        "• Шукати музику на YouTube\n"
        "• Завантажувати треки (MP3 128kbps)\n"
        "• Показувати топ треки\n"
        "• Зберігати статистику\n\n"
        "📋 *Free Plan (Per Month):*\n"
        "🔍 50 пошуків\n"
        "⬇️ 20 завантажень\n\n"
        "💎 *Upgrade to Pro або Premium для безлімітного доступу!*\n\n"
        "🚀 *Команди:*\n"
        "• Надішли назву пісні\n"
        "• `help` - довідка\n"
        "• `stats` - статистика\n"
        "• `top` - топ треки\n"
        "• `random` - випадкова музика\n"
        "• `premium` - купити преміум\n\n"
        "💡 _Просто надішли назву пісні!_"
    )

def handle_help() -> str:
    """Handle help command"""
    return (
        "📖 *Повний гід по Music Finder Bot*\n\n"
        "🎯 *Як користуватися:*\n\n"
        "1️⃣ Надішли назву пісні або виконавця\n"
        "2️⃣ Отримай список з 5 треків\n"
        "3️⃣ Відповідь номером треку (1-5)\n"
        "4️⃣ Отримай MP3 файл\n\n"
        "💡 *Приклади:*\n"
        "• `Imagine Dragons Believer`\n"
        "• `The Weeknd`\n"
        "• `Coldplay Paradise`\n\n"
        "⚙️ *Команди:*\n"
        "• `start` - головне меню\n"
        "• `help` - ця довідка\n"
        "• `stats` - твоя статистика\n"
        "• `top` - топ треки\n"
        "• `random` - випадкова музика\n"
        "• `premium` - плани підписки\n"
        "• `cancel` - скасувати пошук\n\n"
        f"📊 *Обмеження:*\n"
        f"• Макс. розмір: {MAX_FILE_SIZE / 1024 / 1024:.0f} MB\n"
        "• Формат: MP3 (128 kbps)\n"
        "• Результатів: 5 треків"
    )

def handle_stats(from_number: str) -> str:
    """Handle stats command"""
    stats = get_user_stats(from_number)
    remaining = stats.get_remaining()
    
    last_activity = (
        stats.last_activity.strftime("%d.%m.%Y %H:%M")
        if stats.last_activity else "No data"
    )
    
    expires_text = ""
    if stats.plan.plan == "pro" and stats.plan.expires_at:
        expires_text = f"\n⏰ Expires: {stats.plan.expires_at.strftime('%d.%m.%Y')}"
    
    return (
        "📊 *Твоя статистика*\n\n"
        f"👤 Plan: *{stats.plan.plan.upper()}*{expires_text}\n\n"
        f"🔍 Всього пошуків: *{stats.searches}*\n"
        f"⬇️ Всього завантажень: *{stats.downloads}*\n\n"
        f"📈 *Цього місяця:*\n"
        f"🔍 Пошуків: *{remaining['searches']}/{remaining['limits']['searches_per_month']}*\n"
        f"⬇️ Завантажень: *{remaining['downloads']}/{remaining['limits']['downloads_per_month']}*\n\n"
        f"🕐 Остання активність: _{last_activity}_\n\n"
        "💪 Продовжуй у тому ж дусі!"
    )

def handle_premium(from_number: str) -> str:
    """Handle premium command"""
    stats = get_user_stats(from_number)
    remaining = stats.get_remaining()
    
    return (
        "💎 *Преміум Плани*\n\n"
        f"👤 Твій план: *{stats.plan.plan.upper()}*\n\n"
        f"📊 *Поточні ліміти:*\n"
        f"🔍 Пошуків: *{remaining['searches']}/{remaining['limits']['searches_per_month']}*\n"
        f"⬇️ Завантажень: *{remaining['downloads']}/{remaining['limits']['downloads_per_month']}*\n\n"
        "🔄 *Доступні плани:*\n\n"
        "💎 *Pro Plan - $4.99* (30 днів)\n"
        "   • 500 пошуків/місяць\n"
        "   • 200 завантажень/місяць\n"
        f"   🔗 Купити: https://www.tribute.co/@{TRIBUTE_ACCOUNT}?amount=4.99\n\n"
        "👑 *Premium Plan - $19.99* (Lifetime)\n"
        "   • Безлімітні пошуки\n"
        "   • Безлімітні завантаження\n"
        f"   🔗 Купити: https://www.tribute.co/@{TRIBUTE_ACCOUNT}?amount=19.99\n\n"
        "📝 _Після оплати напиши `verify` для активації_"
    )

def handle_search(from_number: str, query: str) -> Optional[str]:
    """Handle search"""
    stats = get_user_stats(from_number)
    
    # Check limits
    if not stats.can_search():
        remaining = stats.get_remaining()
        return (
            f"⚠️ *Ліміт перевищено*\n\n"
            f"Ти досяг ліміту пошуків на цьому місяці\n\n"
            f"🔍 Пошуків: *{remaining['searches']}/{remaining['limits']['searches_per_month']}*\n\n"
            "💎 *Upgrade до Pro або Premium для безлімітного доступу!*\n\n"
            f"Pro: $4.99 (30 днів) - https://www.tribute.co/@{TRIBUTE_ACCOUNT}?amount=4.99\n"
            f"Premium: $19.99 (Lifetime) - https://www.tribute.co/@{TRIBUTE_ACCOUNT}?amount=19.99"
        )
    
    try:
        stats.add_search()
        
        # Check cache
        key = f"{query}|5"
        cached = search_cache.get(key)
        if cached and _is_cache_valid(cached[0], SEARCH_CACHE_TTL):
            results = cached[1]
        else:
            results = YouTubeService.search(query, max_results=5)
            search_cache[key] = (time.time(), results)
        
        if not results:
            return (
                "❌ *Нічого не знайдено*\n\n"
                "Спробуй:\n"
                "• Змінити запит\n"
                "• Використати англійську\n"
                "• Вказати виконавця та назву\n\n"
                "💡 _Приклад: Imagine Dragons_"
            )
        
        # Store session
        user_sessions[from_number] = {
            "results": results,
            "query": query,
            "timestamp": time.time()
        }
        
        # Format message
        message = f"🎵 *Знайдено {len(results)} треків*\n🔍 Запит: _{query}_\n\n"
        
        for idx, result in enumerate(results, 1):
            duration = _format_duration(result.duration)
            title_short = result.title[:50] + "..." if len(result.title) > 50 else result.title
            artist_short = result.artist[:30] + "..." if len(result.artist) > 30 else result.artist
            
            message += (
                f"*{idx}.* {title_short}\n"
                f"   👤 {artist_short}\n"
                f"   ⏱ {duration}\n\n"
            )
        
        message += "\n📝 *Відповідь номером треку (1-5) для завантаження*"
        
        return message
        
    except Exception as e:
        logger.exception("Search error")
        return f"❌ *Помилка пошуку*\n\n{str(e)}"

def handle_download(from_number: str, track_number: int) -> Optional[str]:
    """Handle download"""
    stats = get_user_stats(from_number)
    
    # Check limits
    if not stats.can_download():
        remaining = stats.get_remaining()
        return (
            f"⚠️ *Ліміт перевищено*\n\n"
            f"Ти досяг ліміту завантажень на цьому місяці\n\n"
            f"⬇️ Завантажень: *{remaining['downloads']}/{remaining['limits']['downloads_per_month']}*\n\n"
            "💎 *Upgrade до Pro або Premium!*\n\n"
            f"Pro: $4.99 (30 днів) - https://www.tribute.co/@{TRIBUTE_ACCOUNT}?amount=4.99\n"
            f"Premium: $19.99 (Lifetime) - https://www.tribute.co/@{TRIBUTE_ACCOUNT}?amount=19.99"
        )
    
    try:
        session = user_sessions.get(from_number)
        if not session:
            return (
                "❌ *Немає активного пошуку*\n\n"
                "Спочатку надішли назву пісні!"
            )
        
        # Check session timeout (15 minutes)
        if time.time() - session["timestamp"] > 900:
            del user_sessions[from_number]
            return (
                "⏰ *Сесія завершилась*\n\n"
                "Виконай новий пошук!"
            )
        
        results = session["results"]
        
        if track_number < 1 or track_number > len(results):
            return f"❌ *Невірний номер*\n\nВибери від 1 до {len(results)}"
        
        track = results[track_number - 1]
        
        # Send download notification
        send_whatsapp_message(
            from_number,
            f"⏳ *Завантажую:*\n_{track.title}_\n\n"
            "Це займе 30-60 секунд..."
        )
        
        # Download file
        file_path, filename = YouTubeService.download_audio(track.id, track.title)
        
        file_size = file_path.stat().st_size
        file_size_mb = file_size / 1024 / 1024
        
        # Update stats
        stats.add_download()
        
        # Prepare media URL
        media_url = f"{WEBHOOK_URL}/downloads/{filename}"
        
        # Send file
        caption = (
            f"🎵 *{track.title}*\n\n"
            f"📦 Розмір: {file_size_mb:.1f} MB\n"
            f"🎼 Якість: 128 kbps MP3\n"
            f"⏰ {datetime.now().strftime('%H:%M')}\n\n"
            "✨ Насолоджуйся музикою!"
        )
        
        send_whatsapp_message(from_number, caption, media_url)
        
        return None
        
    except Exception as e:
        logger.exception("Download error")
        return (
            f"❌ *Помилка завантаження*\n\n"
            f"{str(e)}\n\n"
            "Можливі причини:\n"
            "• Файл занадто великий\n"
            "• Проблеми з YouTube\n"
            "• Тимчасова помилка\n\n"
            "💡 Спробуй інший трек"
        )

@app.route("/webhook", methods=['POST'])
def webhook():
    """Webhook for receiving messages"""
    try:
        incoming_msg = request.values.get('Body', '').strip()
        from_number = request.values.get('From', '')
        
        logger.info(f"Received message from {from_number}: {incoming_msg}")
        
        resp = MessagingResponse()
        msg = resp.message()
        
        incoming_lower = incoming_msg.lower()
        
        if incoming_lower in ['start', 'старт', 'почати']:
            response_text = handle_start(from_number)
        
        elif incoming_lower in ['help', 'допомога', 'довідка']:
            response_text = handle_help()
        
        elif incoming_lower in ['stats', 'статистика']:
            response_text = handle_stats(from_number)
        
        elif incoming_lower in ['premium', 'преміум', 'upgrade']:
            response_text = handle_premium(from_number)
        
        elif incoming_lower in ['top', 'топ']:
            response_text = handle_search(from_number, "top music 2024")
        
        elif incoming_lower in ['random', 'випадкова']:
            import random
            queries = ["chill music", "workout music", "relaxing piano", "electronic music"]
            response_text = handle_search(from_number, random.choice(queries))
        
        elif incoming_lower in ['cancel', 'скасувати']:
            if from_number in user_sessions:
                del user_sessions[from_number]
            response_text = "✅ Пошук скасовано. Надішли нову назву пісні!"
        
        elif incoming_msg.isdigit():
            track_number = int(incoming_msg)
            response_text = handle_download(from_number, track_number)
            if response_text is None:
                return str(resp)
        
        else:
            if len(incoming_msg) < 2:
                response_text = "❌ Запит занадто короткий. Надішли назву пісні!"
            else:
                response_text = handle_search(from_number, incoming_msg)
        
        if response_text:
            msg.body(response_text)
        
        return str(resp)
        
    except Exception as e:
        logger.exception("Webhook error")
        resp = MessagingResponse()
        msg = resp.message()
        msg.body("❌ Виникла помилка. Спробуй ще раз!")
        return str(resp)

@app.route("/downloads/<filename>")
def serve_file(filename):
    """Serve downloaded files"""
    return send_from_directory(DOWNLOADS_DIR, filename)

@app.route("/health")
def health():
    """Health check"""
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "downloads_dir": str(DOWNLOADS_DIR)
    }

def cleanup_old_files():
    """Cleanup old files"""
    try:
        current_time = time.time()
        for filepath in DOWNLOADS_DIR.glob("*"):
            if filepath.is_file():
                file_age = current_time - filepath.stat().st_mtime
                if file_age > DOWNLOAD_CACHE_TTL:
                    filepath.unlink()
                    logger.info(f"Removed old file: {filepath}")
    except Exception:
        logger.exception("Cleanup error")

if __name__ == "__main__":
    if TWILIO_ACCOUNT_SID == "YOUR_ACCOUNT_SID":
        logger.error("❌ Set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN!")
    else:
        logger.info("🚀 WhatsApp Music Bot started!")
        logger.info(f"📍 Webhook URL: {WEBHOOK_URL}/webhook")
        app.run(host="0.0.0.0", port=PORT, debug=False)