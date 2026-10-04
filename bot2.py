"""
🎵 MP3 CONVERTER BOT

Video va audio fayllarni MP3 formatiga aylantiradi (FFmpeg orqali).

requirements.txt:
    aiogram>=3.4
    python-dotenv
    imageio-ffmpeg      # ixtiyoriy: tizimda FFmpeg bo‘lmasa, shu paket o‘zining FFmpeg'ini beradi

.env (GitHub'ga yuklamang):
    BOT_TOKEN=...
    FFMPEG_PATH=ffmpeg   # ixtiyoriy: FFmpeg fayl yo‘li yoki nomi

Ishga tushirish:
    python bot.py
"""

# ============================================================
# IMPORTS
# ============================================================

import asyncio
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    ErrorEvent,
    FSInputFile,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from dotenv import load_dotenv


# ============================================================
# CONFIG
# ============================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN .env faylida topilmadi.")

TEMP_DIR = os.getenv("TEMP_DIR", "").strip() or None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("mp3bot")


# ============================================================
# CONSTANTS
# ============================================================

# Telegram Bot API (oddiy server) orqali bot yuklab ola oladigan maksimal hajm: 20 MB.
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
# Bot yubora oladigan maksimal hajm: 50 MB.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

DOWNLOAD_TIMEOUT = 180       # soniya
UPLOAD_TIMEOUT = 300         # soniya
PROBE_TIMEOUT = 30           # soniya
CONVERT_TIMEOUT = 900        # soniya

MAX_PARALLEL_CONVERSIONS = 2  # bir vaqtda ishlaydigan FFmpeg jarayonlari soni
MAX_ACTIVE_PER_USER = 2       # bitta foydalanuvchining bir vaqtdagi fayllari soni

TEMP_PREFIX = "mp3bot_"

MP3_BITRATE = "192k"
MP3_SAMPLE_RATE = "44100"

SUPPORTED_EXTENSIONS = {
    "mp4", "m4a", "wav", "ogg", "oga", "opus", "webm", "avi", "mov", "mkv",
    "mp3", "aac", "flac", "wma", "amr", "3gp", "m4v", "mpeg", "mpg", "mpga",
    "flv", "wmv", "ts", "aiff", "aif", "caf", "mka", "weba",
}

BTN_CONVERT = "🎵 MP3 ga aylantirish"
BTN_HELP = "ℹ️ Yordam"

MSG_START = (
    "🎵 <b>MP3 CONVERTER BOT</b>\n\n"
    "Assalomu alaykum!\n\n"
    "Men video va audio fayllarni MP3 formatiga aylantirib beraman.\n\n"
    "🎬 MP4 → MP3\n"
    "🎵 M4A → MP3\n"
    "🎵 WAV → MP3\n"
    "🎵 OGG → MP3\n"
    "📁 Boshqa qo‘llab-quvvatlanadigan media fayllar\n\n"
    "Faylni yuboring — men uni MP3 ga aylantirib beraman. 🚀"
)

MSG_HELP = (
    "🎵 <b>MP3 Converter yordam</b>\n\n"
    "1️⃣ Video yoki audio fayl yuboring.\n"
    "2️⃣ Bot faylni qabul qiladi.\n"
    "3️⃣ FFmpeg yordamida MP3 ga aylantiradi.\n"
    "4️⃣ Tayyor MP3 faylni qaytaradi.\n\n"
    "Qo‘llab-quvvatlanadigan formatlar:\n"
    "MP4, M4A, WAV, OGG, WEBM, AVI, MOV, MKV.\n\n"
    "ℹ️ Bot orqali 20 MB gacha bo‘lgan fayllarni yuklash mumkin."
)

MSG_MODE_ON = "🎵 MP3 Converter rejimi yoqildi.\n\nVideo yoki audio fayl yuboring."
MSG_ACCEPTED = "⏳ Fayl qabul qilindi..."
MSG_DOWNLOADING = "⏳ Fayl yuklanmoqda..."
MSG_CONVERTING = "🔄 MP3 ga aylantirilmoqda..."
MSG_SENDING = "📤 MP3 yuborilmoqda..."
MSG_DONE = "✅ Tayyor!"
MSG_DONE_CAPTION = "✅ Tayyor!\n\n🎵 MP3 faylingiz:"
MSG_UNSUPPORTED = "❌ Bu fayl formatini qo‘llab-quvvatlamayman."
MSG_INVALID = "❌ Bu fayl buzilgan yoki media fayl emas."
MSG_NO_AUDIO = "❌ Bu faylda audio qismi topilmadi."
MSG_FAILED = (
    "❌ Faylni MP3 ga aylantirishda xatolik yuz berdi.\n\n"
    "Iltimos, boshqa fayl bilan urinib ko‘ring."
)
MSG_TOO_BIG = (
    "❌ Fayl juda katta.\n\n"
    "Telegram bot orqali 20 MB gacha bo‘lgan fayllarni qabul qilish mumkin."
)
MSG_RESULT_TOO_BIG = (
    "❌ Tayyor MP3 fayl Telegram limitidan (50 MB) katta bo‘ldi.\n\n"
    "Iltimos, qisqaroq fayl yuboring."
)
MSG_NO_FFMPEG = (
    "❌ Hozircha konvertatsiya xizmati ishlamayapti (FFmpeg topilmadi).\n\n"
    "Iltimos, keyinroq urinib ko‘ring."
)
MSG_TIMEOUT = "❌ Konvertatsiya juda uzoq davom etdi va to‘xtatildi. Qisqaroq fayl yuboring."
MSG_BUSY = "⏳ Avvalgi fayllaringiz tayyor bo‘lishini kuting, so‘ng yangisini yuboring."
MSG_SEND_FILE = "📎 Video yoki audio fayl yuboring — men uni MP3 ga aylantiraman."


# ============================================================
# KEYBOARDS
# ============================================================

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_CONVERT)],
        [KeyboardButton(text=BTN_HELP)],
    ],
    resize_keyboard=True,
)


# ============================================================
# GLOBAL STATE
# ============================================================

router = Router()
convert_semaphore = asyncio.Semaphore(MAX_PARALLEL_CONVERSIONS)
active_per_user: dict[int, int] = {}
ffmpeg_path: str | None = None


class ConversionError(Exception):
    """Foydalanuvchiga ko‘rsatiladigan xabar bilan xato."""

    def __init__(self, user_message: str):
        super().__init__(user_message)
        self.user_message = user_message


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def safe_stem(name: str | None, fallback: str = "audio") -> str:
    """Fayl nomidan xavfsiz (kengaytmasiz) nom yaratadi."""
    if not name:
        return fallback
    base = Path(name.replace("\\", "/")).name
    stem = Path(base).stem if "." in base else base
    stem = re.sub(r"[^\w\- ]+", "", stem, flags=re.UNICODE)
    stem = re.sub(r"\s+", "_", stem.strip()).strip("._-")
    return stem[:60] or fallback


def safe_extension(name: str | None, mime: str | None = None) -> str:
    """Faqat ruxsat etilgan kengaytmani qaytaradi, aks holda 'bin'."""
    if name and "." in name:
        ext = name.rsplit(".", 1)[-1].lower()
        if ext in SUPPORTED_EXTENSIONS:
            return ext
    mime_map = {
        "audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/x-m4a": "m4a",
        "audio/wav": "wav", "audio/x-wav": "wav", "audio/ogg": "ogg",
        "audio/webm": "webm", "audio/aac": "aac", "audio/flac": "flac",
        "video/mp4": "mp4", "video/webm": "webm", "video/quicktime": "mov",
        "video/x-matroska": "mkv", "video/x-msvideo": "avi",
    }
    return mime_map.get((mime or "").lower(), "bin")


def extract_media(message: Message) -> dict | None:
    """Xabardan media ma'lumotlarini oladi. Qo‘llab-quvvatlanmasa None qaytaradi."""
    if message.video:
        obj = message.video
        return {"file_id": obj.file_id, "size": obj.file_size,
                "name": obj.file_name or "video.mp4", "mime": obj.mime_type}
    if message.audio:
        obj = message.audio
        name = obj.file_name or obj.title or "audio"
        return {"file_id": obj.file_id, "size": obj.file_size,
                "name": name, "mime": obj.mime_type}
    if message.voice:
        obj = message.voice
        return {"file_id": obj.file_id, "size": obj.file_size,
                "name": "voice_message.ogg", "mime": obj.mime_type}
    if message.video_note:
        obj = message.video_note
        return {"file_id": obj.file_id, "size": obj.file_size,
                "name": "video_note.mp4", "mime": "video/mp4"}
    if message.animation:
        obj = message.animation
        return {"file_id": obj.file_id, "size": obj.file_size,
                "name": obj.file_name or "animation.mp4", "mime": obj.mime_type}
    if message.document:
        obj = message.document
        name = obj.file_name or ""
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        mime = (obj.mime_type or "").lower()
        if ext in SUPPORTED_EXTENSIONS or mime.startswith(("audio/", "video/")):
            return {"file_id": obj.file_id, "size": obj.file_size,
                    "name": name or "file", "mime": obj.mime_type}
    return None


async def safe_edit(status: Message, text: str) -> None:
    try:
        await status.edit_text(text)
    except TelegramAPIError as e:
        logger.debug("Status xabarini tahrirlab bo‘lmadi: %s", e)


def cleanup_stale_temp_dirs(max_age_seconds: int = 3600) -> None:
    """Oldingi ishga tushirishdan qolib ketgan vaqtinchalik papkalarni tozalaydi."""
    root = Path(TEMP_DIR or tempfile.gettempdir())
    try:
        now = time.time()
        for path in root.glob(f"{TEMP_PREFIX}*"):
            if path.is_dir() and now - path.stat().st_mtime > max_age_seconds:
                shutil.rmtree(path, ignore_errors=True)
    except OSError as e:
        logger.warning("Temp papkani tozalab bo‘lmadi: %s", e)


# ============================================================
# FFMPEG CHECK
# ============================================================

def resolve_ffmpeg() -> str | None:
    """FFmpeg'ni topadi: FFMPEG_PATH -> tizimdagi 'ffmpeg' -> imageio-ffmpeg (ixtiyoriy)."""
    custom = os.getenv("FFMPEG_PATH", "").strip()
    if custom:
        if Path(custom).is_dir():
            found = shutil.which("ffmpeg", path=custom)
        else:
            found = shutil.which(custom)
        if found:
            return found
        logger.warning("FFMPEG_PATH noto‘g‘ri yoki fayl topilmadi: %s", custom)

    found = shutil.which("ffmpeg")
    if found:
        return found

    try:
        import imageio_ffmpeg  # ixtiyoriy paket

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).exists():
            return exe
    except Exception:
        pass
    return None


def check_ffmpeg() -> bool:
    """Startup paytida FFmpeg mavjudligini tekshiradi. Bot baribir ishlashda davom etadi."""
    global ffmpeg_path
    ffmpeg_path = resolve_ffmpeg()
    if not ffmpeg_path:
        logger.error("FFmpeg topilmadi.")
        logger.error(
            "FFMPEG_PATH o‘zgaruvchisini sozlang yoki 'imageio-ffmpeg' paketini "
            "requirements.txt ga qo‘shing."
        )
        return False

    try:
        version = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-version"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15, check=True,
        ).stdout.splitlines()[0]
        encoders = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-encoders"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError) as e:
        logger.error("FFmpeg topilmadi.")
        logger.error("FFmpeg ishga tushmadi (%s): %s", ffmpeg_path, e)
        ffmpeg_path = None
        return False

    logger.info("FFmpeg topildi: %s (%s)", ffmpeg_path, version)
    if "libmp3lame" not in encoders:
        logger.warning("Bu FFmpeg'da libmp3lame kodeki yo‘q. MP3 konvertatsiya ishlamasligi mumkin.")
    return True


# ============================================================
# CONVERSION
# ============================================================

def probe_media(src: Path) -> float | None:
    """Faylni FFmpeg orqali ochib ko‘radi. Audio davomiyligini (soniya) qaytaradi."""
    try:
        result = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-nostdin",
             "-protocol_whitelist", "file", "-i", str(src)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=PROBE_TIMEOUT, check=False,
        )
    except subprocess.TimeoutExpired:
        raise ConversionError(MSG_INVALID)
    except OSError as e:
        logger.error("FFmpeg ishga tushmadi: %s", e)
        raise ConversionError(MSG_NO_FFMPEG)

    info = result.stderr or ""
    if "Input #0" not in info:
        logger.warning("Probe: fayl ochilmadi. stderr: %s", info[-800:])
        raise ConversionError(MSG_INVALID)
    if not re.search(r"Stream #\d+:\d+.*Audio:", info):
        raise ConversionError(MSG_NO_AUDIO)

    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", info)
    if match:
        h, m, s = match.groups()
        return int(h) * 3600 + int(m) * 60 + float(s)
    return None


def convert_to_mp3(src: Path, dst: Path) -> float | None:
    """Sinxron konvertatsiya (asyncio.to_thread ichida chaqiriladi)."""
    duration = probe_media(src)

    command = [
        ffmpeg_path,
        "-hide_banner", "-nostdin", "-y",
        "-protocol_whitelist", "file",
        "-i", str(src),
        "-vn",                    # video oqimi olinmaydi
        "-map", "0:a:0",          # faqat birinchi audio track
        "-c:a", "libmp3lame",
        "-b:a", MP3_BITRATE,
        "-ar", MP3_SAMPLE_RATE,
        "-map_metadata", "0",
        "-id3v2_version", "3",
        str(dst),
    ]
    try:
        result = subprocess.run(
            command,
            check=True, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=CONVERT_TIMEOUT,
        )
        logger.debug("FFmpeg stderr: %s", (result.stderr or "")[-500:])
    except subprocess.TimeoutExpired:
        logger.error("FFmpeg vaqt chegarasidan oshdi (%s soniya)", CONVERT_TIMEOUT)
        raise ConversionError(MSG_TIMEOUT)
    except subprocess.CalledProcessError as e:
        logger.error("FFmpeg xatosi (kod %s): %s", e.returncode, (e.stderr or "")[-1500:])
        raise ConversionError(MSG_FAILED)
    except OSError as e:
        logger.error("FFmpeg ishga tushmadi: %s", e)
        raise ConversionError(MSG_NO_FFMPEG)

    if not dst.exists() or dst.stat().st_size == 0:
        logger.error("FFmpeg chiqish faylini yaratmadi.")
        raise ConversionError(MSG_FAILED)
    return duration


# ============================================================
# /start HANDLER
# ============================================================

@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(MSG_START, reply_markup=MAIN_KB)


# ============================================================
# /help HANDLER
# ============================================================

@router.message(Command("help"))
@router.message(F.text == BTN_HELP)
async def cmd_help(message: Message):
    await message.answer(MSG_HELP, reply_markup=MAIN_KB)


@router.message(F.text == BTN_CONVERT)
async def on_convert_mode(message: Message):
    await message.answer(MSG_MODE_ON, reply_markup=MAIN_KB)


# ============================================================
# MEDIA HANDLER
# ============================================================

@router.message(F.video | F.audio | F.document | F.voice | F.video_note | F.animation)
async def handle_media(message: Message, bot: Bot):
    global ffmpeg_path

    media = extract_media(message)
    if media is None:
        await message.answer(MSG_UNSUPPORTED, reply_markup=MAIN_KB)
        return

    if not ffmpeg_path:
        ffmpeg_path = resolve_ffmpeg()
    if not ffmpeg_path:
        logger.error("FFmpeg topilmadi.")
        await message.answer(MSG_NO_FFMPEG, reply_markup=MAIN_KB)
        return

    size = media["size"]
    if size and size > MAX_DOWNLOAD_BYTES:
        await message.answer(MSG_TOO_BIG, reply_markup=MAIN_KB)
        return

    user_id = message.from_user.id if message.from_user else message.chat.id
    if active_per_user.get(user_id, 0) >= MAX_ACTIVE_PER_USER:
        await message.answer(MSG_BUSY, reply_markup=MAIN_KB)
        return

    active_per_user[user_id] = active_per_user.get(user_id, 0) + 1
    logger.info("Fayl qabul qilindi: user=%s, nom=%s, hajm=%s", user_id, media["name"], size)
    status = await message.answer(MSG_ACCEPTED)

    try:
        stem = safe_stem(media["name"])
        ext = safe_extension(media["name"], media["mime"])
        out_name = f"{stem}.mp3"

        with tempfile.TemporaryDirectory(prefix=TEMP_PREFIX, dir=TEMP_DIR) as workdir:
            src = Path(workdir) / f"input.{ext}"
            dst = Path(workdir) / "output.mp3"

            # 1) Yuklab olish (diskka, RAM'ga emas)
            await safe_edit(status, MSG_DOWNLOADING)
            try:
                await bot.download(media["file_id"], destination=src, timeout=DOWNLOAD_TIMEOUT)
            except TelegramBadRequest as e:
                if "too big" in str(e).lower():
                    await safe_edit(status, MSG_TOO_BIG)
                    return
                raise

            if not src.exists() or src.stat().st_size == 0:
                await safe_edit(status, MSG_INVALID)
                return

            # 2) Konvertatsiya (event loop'ni bloklamaydi)
            async with convert_semaphore:
                await safe_edit(status, MSG_CONVERTING)
                logger.info("Conversion boshlandi: user=%s, nom=%s", user_id, media["name"])
                duration = await asyncio.to_thread(convert_to_mp3, src, dst)
            logger.info("Conversion tugadi: user=%s, hajm=%s bayt", user_id, dst.stat().st_size)

            if dst.stat().st_size > MAX_UPLOAD_BYTES:
                await safe_edit(status, MSG_RESULT_TOO_BIG)
                return

            # 3) Yuborish
            await safe_edit(status, MSG_SENDING)
            try:
                await bot.send_chat_action(message.chat.id, ChatAction.UPLOAD_DOCUMENT)
            except TelegramAPIError:
                pass
            await message.answer_audio(
                audio=FSInputFile(dst, filename=out_name),
                caption=MSG_DONE_CAPTION,
                title=stem[:64],
                duration=int(duration) if duration else None,
                request_timeout=UPLOAD_TIMEOUT,
            )
            await safe_edit(status, MSG_DONE)

    except ConversionError as e:
        await safe_edit(status, e.user_message)
    except Exception:
        logger.exception("Faylni qayta ishlashda kutilmagan xato (user=%s)", user_id)
        await safe_edit(status, MSG_FAILED)
    finally:
        remaining = active_per_user.get(user_id, 1) - 1
        if remaining <= 0:
            active_per_user.pop(user_id, None)
        else:
            active_per_user[user_id] = remaining


# ============================================================
# FALLBACK HANDLERS
# ============================================================

@router.message(F.text)
async def on_text(message: Message):
    await message.answer(MSG_SEND_FILE, reply_markup=MAIN_KB)


@router.message()
async def on_other(message: Message):
    await message.answer(MSG_UNSUPPORTED, reply_markup=MAIN_KB)


@router.error()
async def on_error(event: ErrorEvent):
    logger.error("Handler xatosi: %s", event.exception, exc_info=event.exception)
    return True


# ============================================================
# MAIN
# ============================================================

async def main():
    cleanup_stale_temp_dirs()
    check_ffmpeg()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await bot.set_my_commands([
            BotCommand(command="start", description="Botni boshlash"),
            BotCommand(command="help", description="Yordam"),
        ])
        logger.info("Bot ishga tushdi...")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot to‘xtatildi.")
