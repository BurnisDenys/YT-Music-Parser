import os
import asyncio
import logging
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from collections import defaultdict
from dataclasses import dataclass, field

import yt_dlp
from aiogram import Bot, Dispatcher, F, types
from aiogram.types import Message, CallbackQuery, FSInputFile
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# --- Configuration ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "7798471165:AAEEEeoqT9-Qra9lEu_iilDD68SjqZ_O1YY")
TRIBUTE_ACCOUNT = os.getenv("TRIBUTE_ACCOUNT", "your_tribute_account")
DOWNLOADS_DIR = Path(os.getenv("DOWNLOADS_DIR", "./downloads"))
TEMP_DIR = Path(os.getenv("TEMP_DIR", "./temp"))
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", "52428800"))  # 50MB

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
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# --- Data Models ---
@dataclass
class UserPlan:
    plan: str = "free"  # free, pro, premium
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
    user_id: int
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
        """Reset monthly limits if month has changed"""
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

@dataclass
class Cache:
    timestamp: float
    data: any
    
    def is_valid(self, ttl: int) -> bool:
        return (time.time() - self.timestamp) < ttl

# --- FSM States ---
class SearchStates(StatesGroup):
    waiting_for_query = State()

# --- Storage ---
class Storage:
    def __init__(self):
        self.user_stats: Dict[int, UserStats] = defaultdict(
            lambda: UserStats(user_id=0)
        )
        self.search_cache: Dict[str, Cache] = {}
        self.download_cache: Dict[str, Cache] = {}
        self.search_results: Dict[int, List[SearchResult]] = {}
        self.search_lock = asyncio.Lock()
        self.download_lock = asyncio.Lock()
    
    async def cleanup_old_files(self):
        """Cleanup old files and cache entries"""
        try:
            current_time = time.time()
            
            for filepath in DOWNLOADS_DIR.glob("*"):
                if filepath.is_file():
                    file_age = current_time - filepath.stat().st_mtime
                    if file_age > DOWNLOAD_CACHE_TTL:
                        filepath.unlink()
                        logger.info(f"Removed old file: {filepath}")
            
            expired_keys = [
                key for key, cache in self.download_cache.items()
                if not cache.is_valid(DOWNLOAD_CACHE_TTL) or 
                   not Path(cache.data).exists()
            ]
            for key in expired_keys:
                del self.download_cache[key]
                
        except Exception as e:
            logger.exception("Cleanup error")

storage = Storage()

# --- YouTube Operations ---
class YouTubeService:
    @staticmethod
    def search(query: str, max_results: int = 10) -> List[SearchResult]:
        """Search YouTube for videos"""
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
        """Download audio from YouTube video"""
        try:
            download_id = str(uuid.uuid4())
            safe_title = YouTubeService._safe_filename(title)
            output_path = DOWNLOADS_DIR / f"{safe_title}_{download_id}.%(ext)s"
            
            ydl_opts = {
                "format": "bestaudio/best",
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
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
                    return filepath, filepath.name
            
            raise Exception("Downloaded file not found")
        except Exception as e:
            logger.exception("Download error")
            raise
    
    @staticmethod
    def _safe_filename(title: str, max_len: int = 100) -> str:
        """Sanitize filename"""
        safe = "".join(
            c for c in title if c.isalnum() or c in (" ", "-", "_")
        ).rstrip()
        safe = safe[:max_len].strip()
        return safe.replace(" ", "_") if safe else "track"

# --- Cache Manager ---
class CacheManager:
    @staticmethod
    async def get_search(query: str, max_results: int = 10) -> List[SearchResult]:
        """Get cached search results"""
        key = f"{query}|{max_results}"
        
        async with storage.search_lock:
            cached = storage.search_cache.get(key)
            if cached and cached.is_valid(SEARCH_CACHE_TTL):
                return cached.data
        
        results = await asyncio.to_thread(
            YouTubeService.search, query, max_results
        )
        
        async with storage.search_lock:
            storage.search_cache[key] = Cache(time.time(), results)
        
        return results
    
    @staticmethod
    async def get_download(
        video_id: str, title: str
    ) -> Tuple[Path, str]:
        """Get cached or download audio"""
        async with storage.download_lock:
            cached = storage.download_cache.get(video_id)
            if cached and cached.is_valid(DOWNLOAD_CACHE_TTL):
                file_path = Path(cached.data)
                if file_path.exists():
                    storage.download_cache[video_id] = Cache(
                        time.time(), str(file_path)
                    )
                    return file_path, file_path.name
        
        file_path, filename = await asyncio.to_thread(
            YouTubeService.download_audio, video_id, title
        )
        
        async with storage.download_lock:
            storage.download_cache[video_id] = Cache(
                time.time(), str(file_path)
            )
        
        return file_path, filename

# --- Keyboards ---
class Keyboards:
    @staticmethod
    def main_menu() -> types.ReplyKeyboardMarkup:
        keyboard = [
            [
                types.KeyboardButton(text="🔍 Search"),
                types.KeyboardButton(text="📊 Stats")
            ],
            [
                types.KeyboardButton(text="❓ Help"),
                types.KeyboardButton(text="⚙️ Settings")
            ],
            [
                types.KeyboardButton(text="🎲 Random"),
                types.KeyboardButton(text="🔥 Top Tracks")
            ]
        ]
        return types.ReplyKeyboardMarkup(
            keyboard=keyboard, resize_keyboard=True
        )
    
    @staticmethod
    def search_results(results: List[SearchResult]) -> types.InlineKeyboardMarkup:
        keyboard = []
        for idx, result in enumerate(results):
            title_short = result.title[:30] if len(result.title) > 30 else result.title
            
            keyboard.append([
                types.InlineKeyboardButton(
                    text=f"⬇️ {title_short}",
                    callback_data=f"dl:{idx}"
                )
            ])
        keyboard.append([
            types.InlineKeyboardButton(
                text="🔄 New Search",
                callback_data="new_search"
            )
        ])
        return types.InlineKeyboardMarkup(inline_keyboard=keyboard)
    
    @staticmethod
    def premium_menu() -> types.InlineKeyboardMarkup:
        keyboard = [
            [types.InlineKeyboardButton(
                text="💎 Buy Pro Plan (30 days)",
                url=f"https://www.tribute.co/@{TRIBUTE_ACCOUNT}?amount=4.99"
            )],
            [types.InlineKeyboardButton(
                text="👑 Buy Premium (Lifetime)",
                url=f"https://www.tribute.co/@{TRIBUTE_ACCOUNT}?amount=19.99"
            )],
            [types.InlineKeyboardButton(
                text="🔙 Back",
                callback_data="back_menu"
            )]
        ]
        return types.InlineKeyboardMarkup(inline_keyboard=keyboard)
    
    @staticmethod
    def settings() -> types.InlineKeyboardMarkup:
        keyboard = [
            [types.InlineKeyboardButton(
                text="💎 Upgrade Plan",
                callback_data="show_premium"
            )],
            [types.InlineKeyboardButton(
                text="🔙 Back",
                callback_data="settings:back"
            )]
        ]
        return types.InlineKeyboardMarkup(inline_keyboard=keyboard)

# --- Utilities ---
def format_duration(seconds: int) -> str:
    """Format duration in seconds"""
    if not seconds:
        return "N/A"
    seconds = int(seconds)
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}:{secs:02d}"

def format_search_results(results: List[SearchResult]) -> str:
    """Format search results as text"""
    text = f"🎵 <b>Found {len(results)} tracks</b>\n\n"
    for idx, result in enumerate(results, 1):
        artist = result.artist[:30] + "..." if len(result.artist) > 30 else result.artist
        text += f"<b>{idx}.</b> 👤 {artist}\n"
    return text

def format_limit_message(remaining: dict, plan_name: str) -> str:
    """Format limit exceeded message"""
    return (
        f"⚠️ <b>Limit Exceeded</b>\n\n"
        f"You reached your monthly limit on <b>{plan_name}</b> plan\n\n"
        f"📊 <b>Your Current Limits:</b>\n"
        f"🔍 Searches: <b>{remaining['limits']['searches_per_month']}</b>/month\n"
        f"⬇️ Downloads: <b>{remaining['limits']['downloads_per_month']}</b>/month\n\n"
        f"💎 <b>Upgrade to get unlimited access!</b>\n\n"
        "Choose your plan:"
    )

# --- Handlers ---
async def cmd_start(message: Message, state: FSMContext) -> None:
    """Handle /start command"""
    user = message.from_user
    user_stats = storage.user_stats[user.id]
    user_stats.user_id = user.id
    user_stats.update_activity()
    
    text = (
        f"👋 <b>Hello, {user.first_name}!</b>\n\n"
        "🎵 I'm <b>Music Finder Bot</b> - your personal music assistant!\n\n"
        "✨ <b>What I can do:</b>\n"
        "• Search for music on YouTube\n"
        "• Download tracks in high quality (MP3 192kbps)\n"
        "• Show top tracks and random music\n"
        "• Track your statistics\n\n"
        "📋 <b>Free Plan Limits (per month):</b>\n"
        f"🔍 50 searches\n"
        f"⬇️ 20 downloads\n\n"
        "💎 <b>Upgrade to Pro or Premium for unlimited access!</b>\n\n"
        "🚀 <b>Get started:</b>\n"
        "Just send me a song name or artist!"
    )
    
    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=Keyboards.main_menu()
    )

async def cmd_help(message: Message) -> None:
    """Handle /help command"""
    text = (
        "📖 <b>Music Finder Bot Guide</b>\n\n"
        "🎯 <b>Main Features:</b>\n\n"
        "🔍 <b>Search Music:</b>\n"
        "• Send song name or artist\n"
        "• Use /search [query]\n"
        "• Press '🔍 Search' button\n\n"
        "⬇️ <b>Download:</b>\n"
        "• Select track from results\n"
        "• Get MP3 high quality file\n"
        "• Files are cached for speed\n\n"
        "📊 <b>Statistics:</b>\n"
        "• View your activity\n"
        "• Track searches and downloads\n\n"
        "💎 <b>Plans:</b>\n"
        "<b>Free:</b> 50 searches + 20 downloads/month\n"
        "<b>Pro:</b> 500 searches + 200 downloads/month (30 days)\n"
        "<b>Premium:</b> Unlimited access (Lifetime)\n\n"
        f"⚙️ <b>Technical Details:</b>\n"
        f"• Max file size: {MAX_FILE_SIZE / 1024 / 1024:.0f} MB\n"
        "• Format: MP3 (192 kbps)\n"
        "• Cache: 24 hours\n"
        "• Source: YouTube"
    )
    
    await message.answer(text, parse_mode="HTML")

async def cmd_stats(message: Message) -> None:
    """Handle /stats command"""
    user_id = message.from_user.id
    stats = storage.user_stats[user_id]
    remaining = stats.get_remaining()
    
    last_activity = (
        stats.last_activity.strftime("%d.%m.%Y %H:%M")
        if stats.last_activity else "No data"
    )
    
    expires_text = ""
    if stats.plan.plan == "pro" and stats.plan.expires_at:
        expires_text = f"\n⏰ Expires: {stats.plan.expires_at.strftime('%d.%m.%Y')}"
    
    text = (
        "📊 <b>Your Statistics</b>\n\n"
        f"👤 Plan: <b>{stats.plan.plan.upper()}</b>{expires_text}\n\n"
        f"🔍 Total Searches: <b>{stats.searches}</b>\n"
        f"⬇️ Total Downloads: <b>{stats.downloads}</b>\n\n"
        f"📈 <b>This Month:</b>\n"
        f"🔍 Searches: <b>{remaining['searches']}/{remaining['limits']['searches_per_month']}</b>\n"
        f"⬇️ Downloads: <b>{remaining['downloads']}/{remaining['limits']['downloads_per_month']}</b>\n\n"
        f"🕐 Last activity: <b>{last_activity}</b>\n\n"
        "💪 Keep it up!"
    )
    
    await message.answer(text, parse_mode="HTML")

async def cmd_settings(message: Message) -> None:
    """Handle /settings command"""
    text = "⚙️ <b>Settings</b>\n\nChoose an option:"
    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=Keyboards.settings()
    )

async def handle_text(message: Message, state: FSMContext) -> None:
    """Handle text messages"""
    text = message.text.strip()
    
    menu_handlers = {
        "🔍 Search": lambda: message.answer(
            "🎵 <b>Music Search</b>\n\n"
            "Send me a song name or artist!",
            parse_mode="HTML"
        ),
        "📊 Stats": lambda: cmd_stats(message),
        "❓ Help": lambda: cmd_help(message),
        "⚙️ Settings": lambda: cmd_settings(message),
        "🎲 Random": lambda: handle_search(
            message, "top hits 2024", state, max_results=15
        ),
        "🔥 Top Tracks": lambda: handle_search(
            message, "top music 2024", state, max_results=15
        ),
    }
    
    if text in menu_handlers:
        await menu_handlers[text]()
        return
    
    await handle_search(message, text, state)

async def handle_search(
    message: Message, query: str, state: FSMContext, max_results: int = 8
) -> None:
    """Execute search and show results"""
    user_id = message.from_user.id
    stats = storage.user_stats[user_id]
    
    # Check limits
    if not stats.can_search():
        remaining = stats.get_remaining()
        plan_name = stats.plan.plan.upper()
        await message.answer(
            format_limit_message(remaining, plan_name),
            parse_mode="HTML",
            reply_markup=Keyboards.premium_menu()
        )
        return
    
    status_msg = await message.answer(
        f"🔍 <b>Searching:</b> <i>{query}</i>\n\n⏳ Please wait...",
        parse_mode="HTML"
    )
    
    try:
        stats.add_search()
        
        results = await CacheManager.get_search(query, max_results=max_results)
        
        if not results:
            await status_msg.edit_text(
                "❌ <b>Nothing found</b>\n\n"
                "Try:\n"
                "• Change your query\n"
                "• Use English\n"
                "• Specify artist and song name",
                parse_mode="HTML"
            )
            return
        
        storage.search_results[user_id] = results
        
        text = format_search_results(results)
        await status_msg.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=Keyboards.search_results(results)
        )
        
    except Exception as e:
        logger.exception("Search error")
        await status_msg.edit_text(
            f"❌ <b>Search Error</b>\n\n"
            f"Details: {str(e)}",
            parse_mode="HTML"
        )

async def handle_download(callback: CallbackQuery) -> None:
    """Handle download callback"""
    user_id = callback.from_user.id
    stats = storage.user_stats[user_id]
    
    # Check limits
    if not stats.can_download():
        remaining = stats.get_remaining()
        plan_name = stats.plan.plan.upper()
        await callback.message.answer(
            format_limit_message(remaining, plan_name),
            parse_mode="HTML",
            reply_markup=Keyboards.premium_menu()
        )
        await callback.answer("Download limit exceeded", show_alert=True)
        return
    
    try:
        parts = callback.data.split(":")
        if len(parts) != 2 or parts[0] != "dl":
            await callback.answer("Invalid request", show_alert=True)
            return
        
        idx = int(parts[1])
        results = storage.search_results.get(user_id)
        
        if not results or idx >= len(results):
            await callback.answer("Result not found", show_alert=True)
            return
        
        result = results[idx]
        video_id = result.id
        title = result.title
        
    except (ValueError, IndexError):
        await callback.answer("Invalid request", show_alert=True)
        return
    
    progress_msg = await callback.message.answer(
        f"⏳ <b>Downloading:</b>\n<i>{title}</i>\n\n🔄 Preparing... 0%",
        parse_mode="HTML"
    )
    
    try:
        await progress_msg.edit_text(
            f"⏳ <b>Downloading:</b>\n<i>{title}</i>\n\n"
            "⬇️ Downloading from YouTube... 30%",
            parse_mode="HTML"
        )
        
        file_path, filename = await CacheManager.get_download(video_id, title)
        
        await progress_msg.edit_text(
            f"⏳ <b>Downloading:</b>\n<i>{title}</i>\n\n"
            "🎵 Converting to MP3... 70%",
            parse_mode="HTML"
        )
        
        file_size = file_path.stat().st_size
        file_size_mb = file_size / 1024 / 1024
        
        await progress_msg.edit_text(
            f"⏳ <b>Downloading:</b>\n<i>{title}</i>\n\n"
            f"📤 Uploading ({file_size_mb:.1f} MB)... 90%",
            parse_mode="HTML"
        )
        
        audio = FSInputFile(str(file_path))
        await callback.message.answer_audio(
            audio,
            title=title,
            caption=(
                f"🎵 <b>{title}</b>\n\n"
                f"📦 Size: {file_size_mb:.1f} MB\n"
                f"🎼 Quality: 192 kbps MP3"
            ),
            parse_mode="HTML"
        )
        
        stats.add_download()
        
        await progress_msg.delete()
        
    except Exception as e:
        logger.exception("Download error")
        await progress_msg.edit_text(
            f"❌ <b>Download Error</b>\n\n"
            f"Details: {str(e)}",
            parse_mode="HTML"
        )

async def handle_callback(callback: CallbackQuery, state: FSMContext) -> None:
    """Handle callback queries"""
    try:
        data = callback.data
        
        if data == "new_search":
            await callback.message.answer("🔍 Send me a song name!")
        elif data.startswith("dl:"):
            await handle_download(callback)
        elif data == "show_premium":
            await callback.message.answer(
                format_limit_message(
                    storage.user_stats[callback.from_user.id].get_remaining(),
                    storage.user_stats[callback.from_user.id].plan.plan.upper()
                ),
                parse_mode="HTML",
                reply_markup=Keyboards.premium_menu()
            )
        elif data == "back_menu" or data == "settings:back":
            await callback.message.delete()
        
        await callback.answer()
        
    except Exception as e:
        logger.exception("Callback error")
        await callback.answer("Error occurred", show_alert=True)

async def periodic_cleanup(dp: Dispatcher) -> None:
    """Periodic cleanup task"""
    while True:
        await asyncio.sleep(3600)  # Every hour
        await storage.cleanup_old_files()
        logger.info("Cleanup completed")

async def main():
    """Main bot entry point"""
    if TELEGRAM_TOKEN == "YOUR_BOT_TOKEN_HERE":
        logger.error("❌ Set TELEGRAM_TOKEN in environment variables!")
        return
    
    bot = Bot(token=TELEGRAM_TOKEN)
    storage_fsm = MemoryStorage()
    dp = Dispatcher(storage=storage_fsm)
    
    # Register handlers
    dp.message.register(cmd_start, Command("start"))
    dp.message.register(cmd_help, Command("help"))
    dp.message.register(cmd_stats, Command("stats"))
    dp.message.register(cmd_settings, Command("settings"))
    dp.message.register(handle_text, F.text)
    dp.callback_query.register(handle_callback)
    
    # Start cleanup task
    asyncio.create_task(periodic_cleanup(dp))
    
    logger.info("🚀 Bot started!")
    
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())