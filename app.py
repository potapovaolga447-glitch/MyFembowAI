import os
import io
import json
import random
import asyncio
import logging
import threading
from functools import wraps
from urllib.parse import quote

import requests
from PIL import Image, ImageDraw, ImageFont
from deep_translator import GoogleTranslator
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes,
)

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
# httpx пишет в логи адреса запросов вместе с токеном бота, поэтому глушим его
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

# ---------- Настройки (берутся из Environment на Render) ----------

BOT_TOKEN = os.environ.get("BOT_TOKEN")
POLLINATIONS_KEY = os.environ.get("POLLINATIONS_KEY", "").strip()
API_BASE = "https://gen.pollinations.ai"

def parse_list(raw, default):
    items = [x.strip() for x in (raw or "").split(",") if x.strip()]
    return items or default

# Модели пробуются по очереди: если первая не сработала, берётся следующая.
# Названия взяты из команды /models. Поменять можно в Render -> Environment
# (переменные IMAGE_MODELS и VIDEO_MODELS, через запятую).
IMAGE_MODELS = parse_list(os.environ.get("IMAGE_MODELS"), [
    "tongyi-mai/z-image-turbo",
    "black-forest-labs/flux.1-schnell",
    "prunaai/p-image",
    "zimage",
])
VIDEO_MODELS = parse_list(os.environ.get("VIDEO_MODELS"), [
    "alibaba/wan-2.2-fast",
    "prunaai/p-video",
    "bytedance/seedance-2.0-mini",
    "wan-fast",
])
VIDEO_SECONDS = 5

# Белый список: ID через запятую. Если пусто, бот доступен всем.
def parse_allowed_ids(raw: str):
    ids = set()
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids

ALLOWED_IDS = parse_allowed_ids(os.environ.get("ALLOWED_USER_IDS", ""))

def is_allowed(user_id: int) -> bool:
    return not ALLOWED_IDS or user_id in ALLOWED_IDS

LABEL_TEXT = "Фембой AI"
LABEL_COLOR = (255, 105, 180)   # розовый
OUTLINE_COLOR = (60, 0, 40)     # тёмная обводка
LABEL_SCALE = 0.5               # размер надписи: 1.0 = крупная, 0.5 = вдвое меньше

# Слова, которые бот сам добавляет к запросу для лучшего качества
QUALITY_SUFFIX = ", highly detailed, high quality, sharp focus, professional, 4k, beautiful lighting, well-formed anatomy"
VIDEO_SUFFIX = ", cinematic, smooth natural motion, highly detailed, high quality"

MAX_PROMPT_LEN = 500
BUSY_USERS = set()  # кто сейчас ждёт результат (по одному запросу за раз)

# ---------- Flask (чтобы Render видел открытый порт) ----------

app = Flask(__name__)

@app.route('/')
def index():
    return "Bot is running"

@app.route('/health')
def health():
    return "OK"

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)

# ---------- Белый список ----------

def allowed_only(func):
    """Пускает к команде только пользователей из белого списка."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.message is None or update.effective_user is None:
            return
        user_id = update.effective_user.id
        if not is_allowed(user_id):
            await update.message.reply_text(
                f"У тебя нет доступа к этому боту.\nТвой ID: {user_id}"
            )
            return
        await func(update, context)
    return wrapper

# ---------- Шрифт и надпись ----------

def load_font(size: int):
    """Ищет шрифт с поддержкой кириллицы."""
    candidates = []
    try:
        import matplotlib
        candidates.append(os.path.join(matplotlib.get_data_path(), "fonts", "ttf", "DejaVuSans-Bold.ttf"))
    except Exception:
        pass
    candidates.append("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()

def add_label(image_bytes: bytes) -> bytes:
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    draw = ImageDraw.Draw(img)

    font_size = max(14, int(img.width / 14 * LABEL_SCALE))
    font = load_font(font_size)
    stroke = max(1, round(img.width / 300 * LABEL_SCALE))
    margin = max(8, int(img.width / 40 * LABEL_SCALE * 2))

    left, top, right, bottom = draw.textbbox((0, 0), LABEL_TEXT, font=font, stroke_width=stroke)
    text_w = right - left
    text_h = bottom - top
    x = img.width - text_w - margin - left
    y = img.height - text_h - margin - top

    draw.text(
        (x, y),
        LABEL_TEXT,
        font=font,
        fill=LABEL_COLOR,
        stroke_width=stroke,
        stroke_fill=OUTLINE_COLOR,
    )
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=92)
    return out.getvalue()

# ---------- Запросы к Pollinations ----------

def translate_to_english(text: str) -> str:
    try:
        return GoogleTranslator(source="auto", target="en").translate(text) or text
    except Exception:
        return text

def short_error(status: int, body: str) -> str:
    """Достаёт из ответа сервиса короткую понятную причину ошибки."""
    msg = ""
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            err = data.get("error", data)
            if isinstance(err, dict):
                msg = err.get("message") or ""
            elif isinstance(err, str):
                msg = err
            if not msg:
                msg = data.get("message") or ""
    except Exception:
        pass
    if not msg:
        msg = (body or "").strip()[:120]
    return f"HTTP {status}: {str(msg)[:140]}"

def http_get_media(url, params, expected_type, timeout, label):
    """Скачивает файл. Возвращает (байты, '') или (None, причина ошибки)."""
    headers = {}
    if POLLINATIONS_KEY and url.startswith(API_BASE):
        headers["Authorization"] = f"Bearer {POLLINATIONS_KEY}"
    try:
        r = requests.get(url, params=params, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        log.warning("%s: ошибка запроса: %s", label, e)
        return None, f"ошибка сети ({type(e).__name__})"
    ctype = r.headers.get("content-type", "")
    if r.status_code == 200 and ctype.startswith(expected_type):
        return r.content, ""
    log.warning("%s: HTTP %s, тип %s, ответ: %s", label, r.status_code, ctype, r.text[:300])
    return None, short_error(r.status_code, r.text)

def make_image(prompt: str):
    """Возвращает (картинка или None, список ошибок по моделям)."""
    english = translate_to_english(prompt) + QUALITY_SUFFIX
    seed = random.randint(1, 10_000_000)
    data = None
    errors = []

    if POLLINATIONS_KEY:
        url = f"{API_BASE}/image/{quote(english, safe='')}"
        for model in IMAGE_MODELS:
            params = {"model": model, "width": 1024, "height": 1024, "seed": seed}
            data, err = http_get_media(url, params, "image", 90, f"фото:{model}")
            if data:
                break
            errors.append(f"{model}: {err}")
    else:
        # Запасной вариант без ключа: старый бесплатный сервис (одна слабая модель)
        url = f"https://image.pollinations.ai/prompt/{quote(english, safe='')}"
        params = {"model": "flux", "width": 1024, "height": 1024, "nologo": "true", "seed": seed}
        data, err = http_get_media(url, params, "image", 90, "фото:legacy")
        if not data:
            errors.append(f"legacy: {err}")

    if not data:
        return None, errors
    try:
        return add_label(data), []
    except Exception:
        log.exception("Не удалось добавить надпись")
        return data, []

def make_video(prompt: str):
    """Возвращает (видео или None, список ошибок по моделям)."""
    english = translate_to_english(prompt) + VIDEO_SUFFIX
    url = f"{API_BASE}/video/{quote(english, safe='')}"
    errors = []
    for model in VIDEO_MODELS:
        params = {"model": model, "duration": VIDEO_SECONDS}
        data, err = http_get_media(url, params, "video", 240, f"видео:{model}")
        if data:
            return data, []
        errors.append(f"{model}: {err}")
    return None, errors

def format_errors(errors) -> str:
    if not errors:
        return "Подробностей нет, смотри логи в Render."
    lines = [f"— {e}" for e in errors[:6]]
    return ("Что ответил сервис:\n" + "\n".join(lines))[:3500]

def fetch_models_text() -> str:
    lines = [
        f"Сейчас для фото: {' → '.join(IMAGE_MODELS)}",
        f"Сейчас для видео: {' → '.join(VIDEO_MODELS)}",
        f"Ключ Pollinations: {'есть' if POLLINATIONS_KEY else 'НЕ задан'}",
        "",
    ]
    try:
        r = requests.get(f"{API_BASE}/image/models", timeout=20)
        data = r.json()
        items = data if isinstance(data, list) else (data.get("data", []) if isinstance(data, dict) else [])
        names = []
        for it in items:
            if isinstance(it, str):
                names.append(it)
            elif isinstance(it, dict):
                name = it.get("name") or it.get("id")
                if name:
                    names.append(str(name))
        if names:
            lines.append("Доступные модели (фото и видео):")
            lines.append(", ".join(names[:80]))
        else:
            lines.append("Список моделей: " + r.text[:400])
    except Exception as e:
        lines.append(f"Не удалось получить список моделей: {e}")
    lines.append("")
    lines.append("Поменять модели можно в Render → Environment: IMAGE_MODELS и VIDEO_MODELS (через запятую).")
    return "\n".join(lines)[:3900]

# ---------- Общая логика генерации ----------

async def do_generate(bot, chat_id: int, user_id: int, kind: str, prompt: str):
    prompt = prompt.strip()[:MAX_PROMPT_LEN]

    if kind == "video" and not POLLINATIONS_KEY:
        await bot.send_message(
            chat_id,
            "Видео пока не настроено: нужно добавить переменную POLLINATIONS_KEY в Render (Environment).",
        )
        return

    if user_id in BUSY_USERS:
        await bot.send_message(chat_id, "Подожди, предыдущий запрос ещё делается.")
        return

    BUSY_USERS.add(user_id)
    status = None
    try:
        if kind == "video":
            status = await bot.send_message(chat_id, "Делаю видео, это займёт 1-3 минуты...")
            await bot.send_chat_action(chat_id, "upload_video")
            video, errors = await asyncio.to_thread(make_video, prompt)
            if video:
                await bot.send_video(
                    chat_id,
                    video=InputFile(video, filename="video.mp4"),
                    caption=prompt[:200],
                    supports_streaming=True,
                    read_timeout=120,
                    write_timeout=120,
                )
            else:
                await bot.send_message(chat_id, "Не получилось сделать видео.\n\n" + format_errors(errors))
        else:
            status = await bot.send_message(chat_id, "Рисую, подожди 10-30 секунд...")
            await bot.send_chat_action(chat_id, "upload_photo")
            image, errors = await asyncio.to_thread(make_image, prompt)
            if image:
                await bot.send_photo(chat_id, photo=image, caption=prompt[:200])
            else:
                await bot.send_message(chat_id, "Не получилось создать картинку.\n\n" + format_errors(errors))
    except Exception:
        log.exception("Ошибка при генерации")
        await bot.send_message(chat_id, "Что-то пошло не так, попробуй ещё раз.")
    finally:
        BUSY_USERS.discard(user_id)
        if status is not None:
            try:
                await status.delete()
            except Exception:
                pass

# ---------- Команды бота ----------

async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает ID пользователя. Доступна всем."""
    if update.message is None or update.effective_user is None:
        return
    await update.message.reply_text(f"Твой ID: {update.effective_user.id}")

@allowed_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я делаю фото и видео по описанию.\n\n"
        "/picture кот в космосе — фото\n"
        "/video кот бежит по лесу — видео\n\n"
        "Или просто напиши описание, и я спрошу, что создать."
    )

@allowed_only
async def picture_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(context.args).strip()
    if not prompt:
        await update.message.reply_text("Напиши описание после команды. Например: /picture закат над морем")
        return
    await do_generate(context.bot, update.message.chat_id, update.effective_user.id, "photo", prompt)

@allowed_only
async def video_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(context.args).strip()
    if not prompt:
        await update.message.reply_text("Напиши описание после команды. Например: /video кот бежит по лесу")
        return
    await do_generate(context.bot, update.message.chat_id, update.effective_user.id, "video", prompt)

@allowed_only
async def models_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await asyncio.to_thread(fetch_models_text)
    await update.message.reply_text(text)

@allowed_only
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Текст без команды: спрашиваем, что создать (фото или видео)."""
    prompt = (update.message.text or "").strip()
    if not prompt:
        return

    pending = context.application.bot_data.setdefault("pending", {})
    msg_id = update.message.message_id
    pending[(update.message.chat_id, msg_id)] = (update.effective_user.id, prompt)
    while len(pending) > 200:  # не копим старые запросы бесконечно
        pending.pop(next(iter(pending)))

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📷 Фото", callback_data=f"p:{msg_id}"),
        InlineKeyboardButton("🎬 Видео", callback_data=f"v:{msg_id}"),
    ]])
    await update.message.reply_text("Что создать: фото или видео?", reply_markup=keyboard)

async def choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q is None or q.message is None:
        return
    if not is_allowed(q.from_user.id):
        await q.answer("У тебя нет доступа", show_alert=True)
        return

    kind_code, msg_id = q.data.split(":", 1)
    pending = context.application.bot_data.get("pending", {})
    key = (q.message.chat_id, int(msg_id))
    item = pending.get(key)
    if item is None:
        await q.answer("Запрос устарел, отправь описание ещё раз.", show_alert=True)
        return

    owner_id, prompt = item
    if q.from_user.id != owner_id:
        await q.answer("Эти кнопки не для тебя.", show_alert=True)
        return

    pending.pop(key, None)
    await q.answer()
    try:
        await q.message.delete()
    except Exception:
        pass

    kind = "video" if kind_code == "v" else "photo"
    await do_generate(context.bot, q.message.chat_id, q.from_user.id, kind, prompt)

def main():
    if not BOT_TOKEN:
        raise RuntimeError("Переменная BOT_TOKEN не задана в Environment на Render")

    threading.Thread(target=run_flask, daemon=True).start()
    asyncio.set_event_loop(asyncio.new_event_loop())

    # concurrent_updates: пока одному человеку делается видео, бот отвечает остальным
    application = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(True).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("id", my_id))
    application.add_handler(CommandHandler(["picture", "photo", "image", "pic"], picture_command))
    application.add_handler(CommandHandler("video", video_command))
    application.add_handler(CommandHandler("models", models_command))
    application.add_handler(CallbackQueryHandler(choice_callback, pattern=r"^[pv]:\d+$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    application.run_polling()

if __name__ == '__main__':
    main()
