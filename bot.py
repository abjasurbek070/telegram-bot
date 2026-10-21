import asyncio
import logging
import os
import re
import uuid
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.types import (
    Message,
    FSInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from aiogram.enums import ParseMode, ChatAction
from aiogram.client.default import DefaultBotProperties

from config import (
    BOT_TOKEN,
    SUPPORTED_DOMAINS,
    DOWNLOAD_DIR,
    TELEGRAM_UPLOAD_LIMIT_BYTES,
    ADMIN_ID,
    ADMIN_PROFILE_URL,
)
from downloader import download_video, download_audio, search_music, get_video_info
from effects import apply_audio_effect, EFFECT_NAMES
from video_processor import process_large_video
from voice_recognizer import recognize_voice_message
from content_filter import is_inappropriate_content
from security import (
    ThrottlingMiddleware,
    GLOBAL_SEMAPHORE,
    periodic_temp_cleaner_task,
)
from cache_db import (
    save_url,
    get_url,
    save_audio_info,
    get_audio_info,
    save_search_results,
    get_search_results,
)

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# URL regex pattern
URL_PATTERN = re.compile(r'https?://(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}(?:/[^\s]*)?')

# In-memory routing
ADMIN_FORWARD_MAP: dict[int, int] = {}  # admin_msg_id -> user_id for reply routing
USER_WAITING_ADMIN_MSG: set[int] = set()

dp = Dispatcher()

# Fast responsive rate limiting
dp.message.middleware(ThrottlingMiddleware(rate_limit=0.8))
dp.callback_query.middleware(ThrottlingMiddleware(rate_limit=0.2))


def is_supported_url(url: str) -> bool:
    url_lower = url.lower()
    return any(domain in url_lower for domain in SUPPORTED_DOMAINS)


def get_audio_effects_keyboard(audio_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🏛 Konsert", callback_data=f"fx:concert:{audio_id}"),
                InlineKeyboardButton(text="🎧 8D Audio", callback_data=f"fx:8d:{audio_id}"),
                InlineKeyboardButton(text="🔊 Remix / Bass", callback_data=f"fx:remix:{audio_id}"),
            ]
        ]
    )


def get_quality_selection_keyboard(video_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🎬 480p", callback_data=f"vq:480p:{video_id}"),
                InlineKeyboardButton(text="🎬 720p (HD)", callback_data=f"vq:720p:{video_id}"),
                InlineKeyboardButton(text="🎬 1080p (FHD)", callback_data=f"vq:1080p:{video_id}"),
            ],
            [
                InlineKeyboardButton(text="⚡️ Asl Sifat (Tezkor)", callback_data=f"vq:best:{video_id}"),
                InlineKeyboardButton(text="🎵 Faqat Audio (MP3)", callback_data=f"vq:mp3:{video_id}"),
            ]
        ]
    )


@dp.message(CommandStart())
async def handle_start(message: Message, bot: Bot):
    bot_info = await bot.get_me()
    bot_username = bot_info.username or "FastSaveUzb_bot"
    user_name = message.from_user.first_name if message.from_user else "Foydalanuvchi"

    text = (
        f"👋 <b>Assalomu alaykum, {user_name}!</b>\n\n"
        f"Men <b>@{bot_username}</b> — universal va tezkor media yuklovchi botiman 🚀\n\n"
        "<b>🌟 Asosiy imkoniyatlar:</b>\n"
        "• 🎬 <b>Video yuklash (480p, 720p, 1080p):</b> Havolani yuboring va sifatni tanlang (100MB+ videolar ham qo'llab-quvvatlanadi).\n"
        "• 🎙 <b>Ovozli xabar orqali qidirish:</b> Qo'shiqchi yoki qo'shiq nomini mikrofonga ayting — bot darhol topib beradi!\n"
        "• 🔍 <b>Qo'shiqchi nomi bilan qidirish:</b> Shunchaki qo'shiqchi yoki musiqa nomini yozib yuboring (masalan: <i>Konsta</i>).\n"
        "• 🎧 <b>Maxsus Audio Effektlar:</b> Har qanday qo'shiqni <b>Konsert</b>, <b>8D Audio</b> yoki <b>Remix & Bass</b> ga aylantiring!\n"
        "• ⚡️ <b>Tezkorlik:</b> Barcha javoblar 5 soniyada beriladi.\n"
        "• 🛡 <b>Xavfsizlik:</b> Nomaqbul (18+) kontentlar filtri o'rnatilgan.\n\n"
        "<b>Boshlash uchun:</b> Havola yuboring, qo'shiqchi nomini yozing yoki ovozli xabar qoldiring! 📥"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👤 Admin profili (Murojaat)", url=ADMIN_PROFILE_URL)],
            [InlineKeyboardButton(text="💬 Adminga bot orqali yozish", callback_data="contact_admin")],
            [InlineKeyboardButton(text="📖 Qo'llanma", callback_data="open_help")]
        ]
    )
    await message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)


@dp.message(Command("help"))
async def handle_help_cmd(message: Message):
    await show_help_message(message)


@dp.callback_query(F.data == "open_help")
async def handle_help_callback(callback: CallbackQuery):
    await callback.answer()
    await show_help_message(callback.message)


async def show_help_message(message: Message):
    text = (
        "📖 <b>Botdan to'liq foydalanish qo'llanmasi:</b>\n\n"
        "<b>1. Video yuklash (480p, 720p, 1080p):</b>\n"
        "• Instagram, TikTok yoki YouTube havolasini yuboring.\n"
        "• 1 soniyada chiqadigan menyudan video sifatini tanlang (480p, 720p, 1080p yoki MP3).\n"
        "• Hajmi 100MB dan katta videolar ham sifatli qilib yetkaziladi.\n\n"
        "<b>2. Qo'shiq qidirish va ovozli xabar:</b>\n"
        "• Shunchaki qo'shiqchi nomini yozib yuboring (masalan: <code>Konsta</code>).\n"
        "• Yoki <b>ovozli xabar (voice message)</b> yuborib qo'shiq nomini ayting — bot darhol topib beradi!\n\n"
        "<b>3. Konsert, 8D va Remix effektlari:</b>\n"
        "• Har qanday qo'shiq ostidagi <b>🏛 Konsert</b>, <b>🎧 8D Audio</b>, <b>🔊 Remix</b> tugmalarini bosing.\n"
        "• O'zingizdagi MP3 qo'shiqni botga yuborib ham effekt qo'shishingiz mumkin.\n\n"
        "<b>4. Xavfsizlik:</b>\n"
        "• Nomaqbul va 18+ kontentlar qat'iyan taqiqlangan.\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "💬 <b>Adminga murojaat:</b> Quyidagi tugma orqali to'g'ridan-to'g'ri admin profiliga kirishingiz mumkin."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👤 Admin profiliga o'tish", url=ADMIN_PROFILE_URL)],
            [InlineKeyboardButton(text="💬 Adminga bot orqali yozish", callback_data="contact_admin")]
        ]
    )
    await message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)


# --- ADMIN / MUROJAAT HANDLERS ---
@dp.callback_query(F.data == "contact_admin")
async def handle_contact_admin_btn(callback: CallbackQuery):
    await callback.answer()
    USER_WAITING_ADMIN_MSG.add(callback.from_user.id)
    await callback.message.answer(
        "✍️ <b>Adminga murojaat:</b>\n\n"
        "Iltimos, adminga yubormoqchi bo'lgan xabaringizni yozing (savol, taklif yoki shikoyat):",
        parse_mode=ParseMode.HTML
    )


@dp.message(Command("admin", "murojaat", "contact"))
async def handle_admin_command(message: Message, command: CommandObject, bot: Bot):
    text = (command.args or "").strip()
    if not text:
        USER_WAITING_ADMIN_MSG.add(message.from_user.id)
        await message.answer(
            "✍️ <b>Adminga xabar yuborish:</b>\n\n"
            "Xabaringizni yozing yoki: <code>/admin xabar matni</code> ko'rinishida yuboring.",
            parse_mode=ParseMode.HTML
        )
        return

    await forward_to_admin(message, text, bot)


async def forward_to_admin(message: Message, text: str, bot: Bot):
    user = message.from_user
    user_info = f"@{user.username}" if user.username else f"{user.first_name}"
    user_id = user.id

    if not ADMIN_ID:
        await message.answer("⚠️ Admin sozlamalari belgilanmagan. Iltimos, keyinroq urinib ko'ring.")
        return

    admin_msg = (
        f"📩 <b>Yangi murojaat!</b>\n\n"
        f"👤 <b>Foydalanuvchi:</b> {user_info} (ID: <code>{user_id}</code>)\n"
        f"💬 <b>Xabar:</b>\n{text}\n\n"
        f"<i>Javob berish uchun ushbu xabarga «Reply» (Javob) qilib yozing.</i>"
    )
    try:
        sent = await bot.send_message(chat_id=int(ADMIN_ID), text=admin_msg, parse_mode=ParseMode.HTML)
        ADMIN_FORWARD_MAP[sent.message_id] = user_id
        await message.answer("✅ <b>Xabaringiz adminga yetkazildi!</b>\nTez orada javob olasiz.", parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Adminga jo'natishda xatolik: {e}")
        await message.answer("❌ Xabarni yetkazishda xatolik yuz berdi.")


# --- VOICE MESSAGE HANDLER ---
@dp.message(F.voice)
async def handle_voice_message(message: Message, bot: Bot):
    voice = message.voice
    if not voice:
        return

    status_msg = await message.reply("🎙 <i>Ovozli xabaringiz tinglanmoqda va tahlil qilinmoqda...</i>", parse_mode=ParseMode.HTML)
    temp_ogg = None

    try:
        unique_id = uuid.uuid4().hex[:8]
        temp_ogg = str(DOWNLOAD_DIR / f"voice_{unique_id}.ogg")

        await bot.download(file=voice.file_id, destination=temp_ogg)

        # Transcribe speech
        recognized_query = await recognize_voice_message(temp_ogg)

        if not recognized_query:
            await status_msg.edit_text(
                "❌ <b>Ovozni aniqlab bo'lmadi!</b>\n\n"
                "Iltimos, mikrofonga qo'shiqchi yoki qo'shiq nomini aniqroq ayting yoki matn shaklida yozing.",
                parse_mode=ParseMode.HTML
            )
            return

        if is_inappropriate_content(recognized_query):
            await status_msg.edit_text("⚠️ <b>Kechirasiz!</b> Nomaqbul yoki 18+ mazmundagi so'rovlar taqiqlangan.")
            return

        await status_msg.edit_text(
            f"🎙 <b>Eshitildi:</b> «<i>{recognized_query}</i>»\n"
            "🔍 Qo'shiq qidirilmoqda...",
            parse_mode=ParseMode.HTML
        )

        # Trigger search
        await execute_music_search(message, recognized_query, bot, edit_msg=status_msg)

    except Exception as e:
        logger.error(f"Ovozli xabarni ishlashda xatolik: {e}", exc_info=True)
        await status_msg.edit_text("❌ Ovozli xabarni qayta ishlashda xatolik yuz berdi.")
    finally:
        if temp_ogg and os.path.exists(temp_ogg):
            try:
                os.remove(temp_ogg)
            except OSError:
                pass


# --- MUSIC SEARCH (TEXT & COMMANDS) ---
@dp.message(Command("music", "song"))
async def handle_music_cmd(message: Message, command: CommandObject, bot: Bot):
    query = (command.args or "").strip()
    if not query:
        await message.answer("🔍 Qo'shiqchi yoki qo'shiq nomini yozing. Masalan: <code>/music Konsta</code>")
        return
    await execute_music_search(message, query, bot)


async def execute_music_search(message: Message, query: str, bot: Bot, edit_msg: Message = None):
    if is_inappropriate_content(query):
        warning = "⚠️ <b>Kechirasiz!</b> Nomaqbul yoki 18+ mazmundagi kontentlarni qidirish taqiqlangan."
        if edit_msg:
            await edit_msg.edit_text(warning, parse_mode=ParseMode.HTML)
        else:
            await message.answer(warning, parse_mode=ParseMode.HTML)
        return

    status_msg = edit_msg or await message.answer(f"🔍 <i>«{query[:50]}» bo'yicha qidirilmoqda...</i>", parse_mode=ParseMode.HTML)

    try:
        async with GLOBAL_SEMAPHORE:
            results = await search_music(query, limit=5)

        if not results:
            await status_msg.edit_text("❌ Hech narsa topilmadi. Boshqa nom bilan qidirib ko'ring.")
            return

        lines = [f"🎶 <b>«{query[:40]}» bo'yicha topilgan qo'shiqlar:</b>\n"]
        keyboard_buttons = []

        for idx, item in enumerate(results, start=1):
            title = item.get("title", "Nomsiz")
            duration_sec = item.get("duration", 0)
            mins, secs = divmod(duration_sec, 60)
            dur_str = f"{mins:02d}:{secs:02d}" if duration_sec > 0 else ""
            lines.append(f"<b>{idx}.</b> {title[:65]} <i>({dur_str})</i>")

            # Encode direct video ID into callback data (never expires!)
            vid = item.get("id") or ""
            keyboard_buttons.append(
                InlineKeyboardButton(text=f"🎵 {idx}", callback_data=f"dl_m:{vid}")
            )

        lines.append("\n<i>Yuklab olish uchun quyidagi raqamlardan birini bosing:</i>")
        text = "\n".join(lines)
        kb = InlineKeyboardMarkup(inline_keyboard=[keyboard_buttons])

        await status_msg.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)

    except Exception as e:
        logger.error(f"Musiqa qidirish xatosi: {e}", exc_info=True)
        await status_msg.edit_text("❌ Qidirishda xatolik yuz berdi.")


# --- VIDEO QUALITY CALLBACK & SELECTION ---
@dp.callback_query(F.data.startswith("vq:"))
async def handle_video_quality_callback(callback: CallbackQuery, bot: Bot):
    parts = callback.data.split(":")
    if len(parts) != 3:
        return

    quality, video_id = parts[1], parts[2]
    url = get_url(video_id)

    if not url:
        await callback.answer("⚠️ Havola eskirgan. Iltimos, havolani qaytadan yuboring.", show_alert=True)
        return

    if quality == "mp3":
        # Download audio directly
        await callback.answer("🎵 Qo'shiq tayyorlanmoqda...")
        status_msg = await callback.message.reply("⏳ <i>Qo'shiq (MP3) tayyorlanmoqda...</i>", parse_mode=ParseMode.HTML)
        file_path = None
        try:
            async with GLOBAL_SEMAPHORE:
                data = await download_audio(url)
            file_path = data.get("file_path")
            if not file_path or not os.path.exists(file_path):
                raise FileNotFoundError("Audio fayli topilmadi.")

            await bot.send_chat_action(chat_id=callback.message.chat.id, action=ChatAction.UPLOAD_DOCUMENT)
            audio_file = FSInputFile(file_path)
            title = data.get("title", "Qo'shiq")
            performer = data.get("performer", "")
            duration = int(data.get("duration") or 0)

            bot_info = await bot.get_me()
            bot_username = bot_info.username or "FastSaveUzb_bot"

            audio_id = uuid.uuid4().hex[:8]
            save_audio_info(audio_id, {
                "url": url,
                "title": title,
                "performer": performer,
                "file_path": file_path,
            })

            caption = (
                f"🎵 <b>{title[:100]}</b>\n"
                f"👤 <i>{performer}</i>\n\n"
                f"🤖 @{bot_username} orqali yuklandi\n"
                f"✨ <i>Qo'shiqqa effekt berish uchun tugmani bosing:</i>"
            )

            await callback.message.answer_audio(
                audio=audio_file,
                caption=caption,
                title=title,
                performer=performer if performer else None,
                duration=duration if duration > 0 else None,
                reply_markup=get_audio_effects_keyboard(audio_id),
                parse_mode=ParseMode.HTML
            )
            try:
                await status_msg.delete()
            except Exception:
                pass
        except Exception as e:
            logger.error(f"Audio yuklash xatosi: {e}", exc_info=True)
            await status_msg.edit_text("❌ Qo'shiqni yuklab bo'lmadi.")
        return

    # Video download with chosen quality (480p, 720p, 1080p, or best)
    await callback.answer(f"🎬 {quality} sifatda yuklanmoqda...")
    status_msg = await callback.message.reply(f"⏳ <b>{quality.upper()}</b> sifatda yuklanmoqda, iltimos kuting...", parse_mode=ParseMode.HTML)

    file_path = None
    extra_files_to_clean = []
    try:
        async with GLOBAL_SEMAPHORE:
            data = await download_video(url, quality=quality if quality != "best" else None)
        file_path = data.get("file_path")

        if not file_path or not os.path.exists(file_path):
            raise FileNotFoundError("Yuklangan fayl topilmadi.")

        file_size = os.path.getsize(file_path)
        mode = "single"
        processed_files = [file_path]

        # Check if video exceeds Telegram 49MB limit
        if file_size > TELEGRAM_UPLOAD_LIMIT_BYTES:
            mb_size = file_size / (1024 * 1024)
            await status_msg.edit_text(
                f"📦 <b>Video hajmi: {mb_size:.1f} MB (100MB+ yuklash tizimi)</b>\n"
                "Telegram limitiga moslab tayyorlanmoqda, iltimos biroz kuting...",
                parse_mode=ParseMode.HTML
            )
            mode, processed_files = await process_large_video(file_path, float(data.get("duration") or 0))
            for pf in processed_files:
                if pf != file_path:
                    extra_files_to_clean.append(pf)

        title = data.get("title", "Video")
        uploader = data.get("uploader", "")
        duration = int(data.get("duration") or 0)
        bot_info = await bot.get_me()
        bot_username = bot_info.username or "FastSaveUzb_bot"

        total_parts = len(processed_files)
        audio_btn = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🎵 Qo'shig'ini yuklab olish (MP3)", callback_data=f"audio:{video_id}")]
            ]
        )

        for p_idx, p_file in enumerate(processed_files, start=1):
            await bot.send_chat_action(chat_id=callback.message.chat.id, action=ChatAction.UPLOAD_VIDEO)
            p_video_file = FSInputFile(p_file)

            if total_parts > 1:
                caption = f"🎬 <b>{title[:65]} [{quality.upper()}] [{p_idx}/{total_parts}-qism]</b>\n"
            else:
                caption = f"🎬 <b>{title[:85]} [{quality.upper()}]</b>\n"

            if uploader:
                caption += f"👤 <i>Muallif: {uploader}</i>\n"

            if mode == "compressed":
                comp_mb = os.path.getsize(p_file) / (1024 * 1024)
                caption += f"⚡️ <i>Hajmi siqildi: {comp_mb:.1f} MB</i>\n"

            caption += f"\n🤖 @{bot_username} orqali yuklandi"
            reply_markup = audio_btn if p_idx == 1 else None

            await callback.message.answer_video(
                video=p_video_file,
                caption=caption,
                reply_markup=reply_markup,
                duration=duration if (duration > 0 and total_parts == 1) else None,
                parse_mode=ParseMode.HTML,
                supports_streaming=True
            )

        try:
            await status_msg.delete()
        except Exception:
            pass

    except Exception as e:
        logger.error(f"Sifat bo'yicha video yuklash xatosi: {e}", exc_info=True)
        await status_msg.edit_text(f"❌ Videoni {quality} sifatda yuklab bo'lmadi.")
    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError:
                pass
        for extra_f in extra_files_to_clean:
            if extra_f and os.path.exists(extra_f):
                try:
                    os.remove(extra_f)
                except OSError:
                    pass


# --- SEARCH RESULT SELECTION CALLBACK ---
@dp.callback_query(F.data.startswith("dl_m:"))
async def handle_search_download_callback(callback: CallbackQuery, bot: Bot):
    video_id = callback.data.split(":", 1)[1]
    if not video_id:
        await callback.answer("⚠️ Qidiruv natijasi eskirgan. Qaytadan qidiring.", show_alert=True)
        return

    url = f"https://www.youtube.com/watch?v={video_id}"
    await callback.answer("🎵 Qo'shiq yuklanmoqda...")
    status_msg = await callback.message.reply(
        "⏳ <b>Qo'shiq (MP3) tayyorlanmoqda...</b>",
        parse_mode=ParseMode.HTML
    )

    file_path = None
    try:
        async with GLOBAL_SEMAPHORE:
            data = await download_audio(url)
        file_path = data.get("file_path")

        if not file_path or not os.path.exists(file_path):
            raise FileNotFoundError("Audio fayli topilmadi.")

        await bot.send_chat_action(chat_id=callback.message.chat.id, action=ChatAction.UPLOAD_DOCUMENT)

        audio_file = FSInputFile(file_path)
        title = data.get("title", "Qo'shiq")
        performer = data.get("performer", "")
        duration = int(data.get("duration") or 0)

        bot_info = await bot.get_me()
        bot_username = bot_info.username or "FastSaveUzb_bot"

        audio_id = uuid.uuid4().hex[:8]
        save_audio_info(audio_id, {
            "url": url,
            "title": title,
            "performer": performer,
            "file_path": file_path,
        })

        caption = (
            f"🎵 <b>{title[:100]}</b>\n"
            f"👤 <i>{performer}</i>\n\n"
            f"🤖 @{bot_username} orqali yuklandi\n"
            f"✨ <i>Quyidagi tugmalar orqali qo'shiqqa effekt bering:</i>"
        )

        await callback.message.answer_audio(
            audio=audio_file,
            caption=caption,
            title=title,
            performer=performer if performer else None,
            duration=duration if duration > 0 else None,
            reply_markup=get_audio_effects_keyboard(audio_id),
            parse_mode=ParseMode.HTML
        )

        try:
            await status_msg.delete()
        except Exception:
            pass

    except Exception as e:
        logger.error(f"Search download callback xatosi: {e}", exc_info=True)
        await status_msg.edit_text("❌ Qo'shiqni yuklab bo'lmadi.")


# --- AUDIO EFFECT CALLBACKS (8D, CONCERT, REMIX) ---
@dp.callback_query(F.data.startswith("fx:"))
async def handle_effect_callback(callback: CallbackQuery, bot: Bot):
    parts = callback.data.split(":")
    if len(parts) != 3:
        return

    effect_type, audio_id = parts[1], parts[2]
    audio_info = get_audio_info(audio_id)

    if not audio_info:
        await callback.answer("⚠️ Qo'shiq ma'lumotlari eskirgan. Iltimos, qaytadan yuklang.", show_alert=True)
        return

    effect_name = EFFECT_NAMES.get(effect_type, effect_type)
    await callback.answer(f"✨ {effect_name} tayyorlanmoqda...")
    status_msg = await callback.message.reply(
        f"🎧 <b>«{effect_name}»</b> effekti qo'shilmoqda, iltimos biroz kuting...",
        parse_mode=ParseMode.HTML
    )

    source_path = audio_info.get("file_path")
    created_source = False
    fx_output_path = None

    try:
        if not source_path or not os.path.exists(source_path):
            url = audio_info.get("url")
            if not url:
                raise FileNotFoundError("Asl audio manbasi topilmadi.")
            async with GLOBAL_SEMAPHORE:
                download_res = await download_audio(url)
            source_path = download_res.get("file_path")
            created_source = True

        if not source_path or not os.path.exists(source_path):
            raise FileNotFoundError("Asl audio faylini tiklab bo'lmadi.")

        async with GLOBAL_SEMAPHORE:
            fx_output_path, eff_title = await apply_audio_effect(source_path, effect_type)

        await bot.send_chat_action(chat_id=callback.message.chat.id, action=ChatAction.UPLOAD_DOCUMENT)

        audio_file = FSInputFile(fx_output_path)
        base_title = audio_info.get("title", "Qo'shiq")
        performer = audio_info.get("performer", "")

        bot_info = await bot.get_me()
        bot_username = bot_info.username or "FastSaveUzb_bot"

        full_title = f"{base_title} ({eff_title})"
        caption = (
            f"✨ <b>{full_title[:100]}</b>\n"
            f"👤 <i>{performer}</i>\n\n"
            f"🎧 <i>Maxsus {eff_title} versiyasi</i>\n"
            f"🤖 @{bot_username}"
        )

        await callback.message.answer_audio(
            audio=audio_file,
            caption=caption,
            title=full_title,
            performer=performer if performer else None,
            parse_mode=ParseMode.HTML
        )

        try:
            await status_msg.delete()
        except Exception:
            pass

    except Exception as e:
        logger.error(f"Effekt qo'llashda xatolik ({effect_type}): {e}", exc_info=True)
        await status_msg.edit_text("❌ Effektni qo'llashda xatolik yuz berdi.")
    finally:
        if fx_output_path and os.path.exists(fx_output_path):
            try:
                os.remove(fx_output_path)
            except OSError:
                pass
        if created_source and source_path and os.path.exists(source_path):
            try:
                os.remove(source_path)
            except OSError:
                pass


# --- AUDIO FROM VIDEO BUTTON CALLBACK ---
@dp.callback_query(F.data.startswith("audio:"))
async def handle_audio_from_video_callback(callback: CallbackQuery, bot: Bot):
    short_id = callback.data.split(":", 1)[1]
    url = get_url(short_id)

    if not url:
        await callback.answer("⚠️ Havola eskirgan. Iltimos, havolani qaytadan yuboring.", show_alert=True)
        return

    await callback.answer("🎵 Qo'shiq yuklanmoqda...")
    status_msg = await callback.message.reply("⏳ <i>Qo'shiq (MP3) tayyorlanmoqda...</i>", parse_mode=ParseMode.HTML)

    file_path = None
    try:
        async with GLOBAL_SEMAPHORE:
            data = await download_audio(url)
        file_path = data.get("file_path")

        if not file_path or not os.path.exists(file_path):
            raise FileNotFoundError("Audio fayli topilmadi.")

        await bot.send_chat_action(chat_id=callback.message.chat.id, action=ChatAction.UPLOAD_DOCUMENT)

        audio_file = FSInputFile(file_path)
        title = data.get("title", "Qo'shiq")
        performer = data.get("performer", "")
        duration = int(data.get("duration") or 0)

        bot_info = await bot.get_me()
        bot_username = bot_info.username or "FastSaveUzb_bot"

        audio_id = uuid.uuid4().hex[:8]
        save_audio_info(audio_id, {
            "url": url,
            "title": title,
            "performer": performer,
            "file_path": file_path,
        })

        caption = (
            f"🎵 <b>{title[:100]}</b>\n"
            f"👤 <i>{performer}</i>\n\n"
            f"🤖 @{bot_username} orqali yuklandi\n"
            f"✨ <i>Quyidagi tugmalar orqali qo'shiqqa effekt bering:</i>"
        )

        await callback.message.reply_audio(
            audio=audio_file,
            caption=caption,
            title=title,
            performer=performer if performer else None,
            duration=duration if duration > 0 else None,
            reply_markup=get_audio_effects_keyboard(audio_id),
            parse_mode=ParseMode.HTML
        )

        try:
            await status_msg.delete()
        except Exception:
            pass

    except Exception as e:
        logger.error(f"Audio callback xatosi: {e}", exc_info=True)
        await status_msg.edit_text("❌ Videoning qo'shig'ini yuklab bo'lmadi.")


# --- DIRECT MP3 USER UPLOAD ---
@dp.message(F.audio)
async def handle_user_audio_upload(message: Message, bot: Bot):
    audio = message.audio
    if not audio:
        return

    status_msg = await message.reply("⏳ <i>Audio qabul qilinmoqda...</i>", parse_mode=ParseMode.HTML)
    temp_file_path = None
    try:
        unique_id = uuid.uuid4().hex[:8]
        temp_file_path = str(DOWNLOAD_DIR / f"user_{unique_id}.mp3")

        await bot.download(file=audio.file_id, destination=temp_file_path)

        audio_id = uuid.uuid4().hex[:8]
        title = audio.title or "Musiqa"
        performer = audio.performer or ""

        save_audio_info(audio_id, {
            "url": None,
            "title": title,
            "performer": performer,
            "file_path": temp_file_path,
        })

        await status_msg.edit_text(
            f"🎵 <b>{title[:80]}</b> qabul qilindi!\n\n"
            "Qo'shiqqa qanday effekt qo'shmoqchisiz? Quyidagi tugmalardan birini tanlang:",
            reply_markup=get_audio_effects_keyboard(audio_id),
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"Audio qabul qilishda xatolik: {e}", exc_info=True)
        await status_msg.edit_text("❌ Audioni yuklab olishda xatolik yuz berdi.")


# --- GENERAL TEXT & URL HANDLER ---
@dp.message(F.text)
async def handle_general_text(message: Message, bot: Bot):
    user_id = message.from_user.id
    text = (message.text or "").strip()

    # 1. Admin reply forwarding
    if message.reply_to_message and str(user_id) == ADMIN_ID:
        target_user_id = ADMIN_FORWARD_MAP.get(message.reply_to_message.message_id)
        if target_user_id:
            try:
                await bot.send_message(
                    chat_id=target_user_id,
                    text=f"👨‍💻 <b>Admin javobi:</b>\n\n{text}",
                    parse_mode=ParseMode.HTML
                )
                await message.reply("✅ Javob foydalanuvchiga yetkazildi!")
                return
            except Exception as e:
                await message.reply(f"❌ Foydalanuvchiga yetkazishda xatolik: {e}")
                return

    # 2. Check if user is sending message to admin
    if user_id in USER_WAITING_ADMIN_MSG:
        USER_WAITING_ADMIN_MSG.remove(user_id)
        await forward_to_admin(message, text, bot)
        return

    # 3. Check for inappropriate content
    if is_inappropriate_content(text):
        await message.answer(
            "⚠️ <b>Kechirasiz!</b> Nomaqbul yoki 18+ mazmundagi so'rovlarni qidirish va yuklash taqiqlangan.",
            parse_mode=ParseMode.HTML
        )
        return

    urls = URL_PATTERN.findall(text)

    # 4. If plain text without URL -> Singer/Song search!
    if not urls:
        if len(text) >= 2 and not text.startswith("/"):
            await execute_music_search(message, text, bot)
        else:
            await message.answer("Iltimos, video havolasini yuboring yoki qo'shiqchi nomini yozing.")
        return

    url = urls[0]
    if not is_supported_url(url):
        await message.answer(
            "⚠️ Ushbu havola qo'llab-quvvatlanmaydi.\n"
            "Faqat Instagram, YouTube yoki TikTok havolalarini yuboring."
        )
        return

    # 5. Instant sub-second response: Save URL and show Quality Selection Menu right away!
    video_id = uuid.uuid4().hex[:8]
    save_url(video_id, url)

    caption = (
        "🎬 <b>Media havolasi qabul qilindi!</b>\n\n"
        "⚡️ Kerakli sifat yoki formatni tanlang:\n"
        "<i>(Tanlaganingizdan so'ng yuklash darhol boshlanadi)</i>"
    )
    await message.answer(
        caption,
        reply_markup=get_quality_selection_keyboard(video_id),
        parse_mode=ParseMode.HTML
    )


async def main():
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )

    asyncio.create_task(periodic_temp_cleaner_task(interval_seconds=300))

    logger.info("Bot barcha optimizatsiyalar (Deno JS, sub-second menu, SQLite kesh) bilan ishga tushmoqda...")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
