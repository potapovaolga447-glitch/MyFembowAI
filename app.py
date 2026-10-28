import os
import io
import sys
import json
import time
import random
import asyncio
import logging
import secrets
import threading
from functools import wraps, lru_cache
from urllib.parse import quote

import requests
from PIL import Image, ImageDraw, ImageFont
from deep_translator import GoogleTranslator
from flask import Flask, Response, abort
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

def int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default

# Автоперезапуск: раз в сколько минут перезапускать бота (0 = выключить)
RESTART_EVERY_MIN = int_env("RESTART_EVERY_MIN", 20)
# Самопроверка, чтобы бесплатный Render не засыпал: раз в сколько минут (0 = выключить)
KEEP_ALIVE_MIN = int_env("KEEP_ALIVE_MIN", 10)

# Модели для фото по описанию (пробуются по очереди). Названия из команды /models.
IMAGE_MODELS = parse_list(os.environ.get("IMAGE_MODELS"), [
    "tongyi-mai/z-image-turbo",
    "black-forest-labs/flux.1-schnell",
    "prunaai/p-image",
    "zimage",
])

# Модели для изменения готового фото (пробуются по очереди).
EDIT_MODELS = parse_list(os.environ.get("EDIT_MODELS"), [
    "black-forest-labs/flux.2-klein-4b",
    "prunaai/p-image-edit",
    "black-forest-labs/flux.1-kontext-pro",
])

# Модели для видео (платные на Pollinations: нужен баланс на ключе).
VIDEO_MODELS = parse_list(os.environ.get("VIDEO_MODELS"), [
    "alibaba/wan-2.2-fast",
    "prunaai/p-video",
    "bytedance/seedance-2.0-mini",
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
DEFAULT_PHOTO_VIDEO_PROMPT = "bring this photo to life with natural subtle motion, cinematic"

MAX_PROMPT_LEN = 500
BUSY_USERS = set()  # кто сейчас ждёт результат (по одному запросу за раз)

# ---------- Временные картинки для работы с твоим фото ----------
# Чтобы ИИ мог получить твоё фото, бот на несколько минут выкладывает его
# по секретной одноразовой ссылке на своём сервисе и сразу удаляет после работы.
# Ссылку на файл из Telegram (в ней есть токен бота) наружу НЕ отдаём.

TEMP_FILES = {}
TEMP_TTL = 600  # секунд

def register_temp(data: bytes) -> str:
    now = time.time()
    for k in [k for k, (_, exp) in TEMP_FILES.items() if exp < now]:
        TEMP_FILES.pop(k, None)
    token = secrets.token_urlsafe(16)
    TEMP_FILES[token] = (data, now + TEMP_TTL)
    return token

def public_base_url() -> str:
    base = os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL") or ""
    return base.strip().rstrip("/")

# ---------- Flask (чтобы Render видел открытый порт) ----------

app = Flask(__name__)

@app.route('/')
def index():
    return "Bot is running"

@app.route('/health')
def health():
    return "OK"

@app.route('/t/<token>.jpg')
def temp_image(token):
    item = TEMP_FILES.get(token)
    if not item or item[1] < time.time():
        abort(404)
    return Response(item[0], mimetype="image/jpeg")

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)

# ---------- Самопроверка и автоперезапуск ----------

def keep_alive_loop():
    """Сам стучится в свой адрес, чтобы бесплатный Render не засыпал."""
    while True:
        time.sleep(KEEP_ALIVE_MIN * 60)
        base = public_base_url()
        if not base:
            continue
        try:
            requests.get(f"{base}/health", timeout=20)
        except requests.RequestException as e:
            log.warning("Самопроверка не удалась: %s", e)

def restart_loop():
    """Раз в N минут перезапускает бота, но только когда никто не ждёт результат."""
    while True:
        time.sleep(RESTART_EVERY_MIN * 60)
        waited = 0
        while BUSY_USERS and waited < 600:
            time.sleep(5)
            waited += 5
        if BUSY_USERS:
            log.info("Плановый перезапуск отложен: бот занят")
            continue
        log.info("Плановый перезапуск бота")
        logging.shutdown()
        os.execv(sys.executable, [sys.executable] + sys.argv)

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

@lru_cache(maxsize=8)
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

def label_margin(width: int) -> int:
    return max(8, int(width / 40 * LABEL_SCALE * 2))

def label_layer(base_width: int) -> Image.Image:
    """Прозрачный слой с розовой надписью, размер зависит от ширины картинки."""
    font = load_font(max(14, int(base_width / 14 * LABEL_SCALE)))
    stroke = max(1, round(base_width / 300 * LABEL_SCALE))
    probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    left, top, right, bottom = probe.textbbox((0, 0), LABEL_TEXT, font=font, stroke_width=stroke)
    layer = Image.new("RGBA", (right - left + 2, bottom - top + 2), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text(
        (-left + 1, -top + 1),
        LABEL_TEXT,
        font=font,
        fill=LABEL_COLOR + (255,),
        stroke_width=stroke,
        stroke_fill=OUTLINE_COLOR + (255,),
    )
    return layer

def add_label(image_bytes: bytes) -> bytes:
    """Ставит надпись в правый нижний угол фото."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    layer = label_layer(img.width)
    margin = label_margin(img.width)
    x = img.width - layer.width - margin
    y = img.height - layer.height - margin
    img.paste(layer, (x, y), layer)
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
    """Фото по описанию с надписью. Возвращает (байты или None, список ошибок)."""
    english = translate_to_english(prompt) + QUALITY_SUFFIX
    seed = random.randint(1, 10_000_000)
    data = None
    errors = []

    if POLLINATIONS_KEY:
        url = f"{API_BASE}/image/{quote(english, safe='')}"
        for model in IMAGE_MODELS:
            params = {"model": model, "width": 1024, "height": 1024, "seed": seed}
            label = f"фото:{model}"
            data, err = http_get_media(url, params, "image", 90, label)
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

# ---------- Работа с присланным фото ----------

def prepare_jpeg(photo_bytes: bytes, max_side: int = 1024) -> bytes:
    img = Image.open(io.BytesIO(photo_bytes)).convert("RGB")
    img.thumbnail((max_side, max_side))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=90)
    return out.getvalue()

def publish_temp_photo(photo_bytes: bytes):
    """Выкладывает фото на временную ссылку. Возвращает (ссылка, токен, ошибка)."""
    if not POLLINATIONS_KEY:
        return None, None, "нужна переменная POLLINATIONS_KEY в Render"
    base = public_base_url()
    if not base:
        return None, None, (
            "не знаю адрес сервиса: добавь в Render → Environment переменную PUBLIC_URL "
            "(адрес твоего сервиса, например https://имя.onrender.com)"
        )
    try:
        jpeg = prepare_jpeg(photo_bytes)
    except Exception:
        return None, None, "не удалось прочитать фото"
    token = register_temp(jpeg)
    return f"{base}/t/{token}.jpg", token, ""

def make_edit(prompt: str, photo_bytes: bytes):
    """Меняет присланное фото по описанию. Возвращает (байты или None, список ошибок)."""
    img_url, token, err = publish_temp_photo(photo_bytes)
    if not img_url:
        return None, [err]

    english = translate_to_english(prompt)
    seed = random.randint(1, 10_000_000)
    url = f"{API_BASE}/image/{quote(english, safe='')}"
    errors = []
    data = None
    try:
        for model in EDIT_MODELS:
            params = {"model": model, "image": img_url, "seed": seed}
            label = f"правка:{model}"
            data, e = http_get_media(url, params, "image", 120, label)
            if data:
                break
            errors.append(f"{model}: {e}")
    finally:
        TEMP_FILES.pop(token, None)  # сразу удаляем фото с временной ссылки

    if not data:
        return None, errors
    try:
        return add_label(data), []
    except Exception:
        log.exception("Не удалось добавить надпись")
        return data, []

# ---------- Видео через ИИ (платное: нужен баланс на ключе Pollinations) ----------

def make_video(prompt: str):
    """Видео по описанию. Возвращает (байты или None, список ошибок)."""
    if not POLLINATIONS_KEY:
        return None, ["для видео нужна переменная POLLINATIONS_KEY в Render"]
    english = translate_to_english(prompt) + VIDEO_SUFFIX
    url = f"{API_BASE}/video/{quote(english, safe='')}"
    errors = []
    for model in VIDEO_MODELS:
        params = {"model": model, "duration": VIDEO_SECONDS}
        label = f"видео:{model}"
        data, err = http_get_media(url, params, "video", 300, label)
        if data:
            return data, []
        errors.append(f"{model}: {err}")
    return None, errors

def make_photo_video(prompt: str, photo_bytes: bytes):
    """Видео из присланного фото. Возвращает (байты или None, список ошибок)."""
    img_url, token, err = publish_temp_photo(photo_bytes)
    if not img_url:
        return None, [err]

    english = translate_to_english(prompt or DEFAULT_PHOTO_VIDEO_PROMPT)
    url = f"{API_BASE}/video/{quote(english, safe='')}"
    errors = []
    try:
        for model in VIDEO_MODELS:
            params = {"model": model, "image": img_url, "duration": VIDEO_SECONDS}
            label = f"видео из фото:{model}"
            data, e = http_get_media(url, params, "video", 300, label)
            if data:
                return data, []
            errors.append(f"{model}: {e}")
    finally:
        TEMP_FILES.pop(token, None)
    return None, errors

def format_errors(errors) -> str:
    if not errors:
        return "Подробностей нет, смотри логи в Render."
    lines = [f"— {e}" for e in errors[:6]]
    return ("Что пошло не так:\n" + "\n".join(lines))[:3500]

def fetch_models_text() -> str:
    restart = f"каждые {RESTART_EVERY_MIN} мин" if RESTART_EVERY_MIN > 0 else "выключен"
    ping = f"каждые {KEEP_ALIVE_MIN} мин" if KEEP_ALIVE_MIN > 0 else "выключена"
    lines = [
        f"Фото по описанию: {' → '.join(IMAGE_MODELS)}",
        f"Изменение фото: {' → '.join(EDIT_MODELS)}",
        f"Видео: {' → '.join(VIDEO_MODELS)}",
        f"Ключ Pollinations: {'есть' if POLLINATIONS_KEY else 'НЕ задан'}",
        f"Адрес сервиса: {public_base_url() or 'не найден (нужна переменная PUBLIC_URL)'}",
        f"Автоперезапуск: {restart}",
        f"Самопроверка: {ping}",
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
            lines.append("Доступные модели:")
            lines.append(", ".join(names[:80]))
        else:
            lines.append("Список моделей: " + r.text[:400])
    except Exception as e:
        lines.append(f"Не удалось получить список моделей: {e}")
    lines.append("")
    lines.append("Менять модели можно в Render → Environment: IMAGE_MODELS, EDIT_MODELS, VIDEO_MODELS (через запятую).")
    return "\n".join(lines)[:3900]

# ---------- Общая логика генерации ----------

async def do_generate(bot, chat_id: int, user_id: int, kind: str, prompt: str = "", file_id: str = None):
    """kind: photo (по описанию), video (по описанию), edit (правка фото), photovideo (видео из фото)."""
    prompt = prompt.strip()[:MAX_PROMPT_LEN]

    if user_id in BUSY_USERS:
        await bot.send_message(chat_id, "Подожди, предыдущий запрос ещё делается.")
        return

    BUSY_USERS.add(user_id)
    status = None
    try:
        photo_bytes = None
        if kind in ("edit", "photovideo"):
            tg_file = await bot.get_file(file_id)
            photo_bytes = bytes(await tg_file.download_as_bytearray())

        if kind in ("video", "photovideo"):
            status = await bot.send_message(chat_id, "Делаю видео, это займёт 1-3 минуты...")
            await bot.send_chat_action(chat_id, "upload_video")
            if kind == "video":
                video, errors = await asyncio.to_thread(make_video, prompt)
            else:
                video, errors = await asyncio.to_thread(make_photo_video, prompt, photo_bytes)
            if video:
                await bot.send_video(
                    chat_id,
                    video=InputFile(video, filename="video.mp4"),
                    caption=prompt[:200] or None,
                    supports_streaming=True,
                    read_timeout=120,
                    write_timeout=120,
                )
            else:
                await bot.send_message(
                    chat_id,
                    "Не получилось сделать видео.\n\n" + format_errors(errors)
                    + "\n\nЕсли в ответе HTTP 402, видео платное: нужен баланс на enter.pollinations.ai.",
                )

        elif kind == "edit":
            status = await bot.send_message(chat_id, "Меняю фото, подожди 20-60 секунд...")
            await bot.send_chat_action(chat_id, "upload_photo")
            image, errors = await asyncio.to_thread(make_edit, prompt, photo_bytes)
            if image:
                await bot.send_photo(chat_id, photo=image, caption=prompt[:200])
            else:
                await bot.send_message(
                    chat_id,
                    "Не получилось изменить фото.\n\n" + format_errors(errors)
                    + "\n\nЕсли в ответе HTTP 402, эта модель платная для твоего ключа.",
                )

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
        "Можно просто написать описание, и я спрошу, что создать.\n"
        "Или пришли мне своё фото: я изменю его по описанию или сделаю из него видео."
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
    """Текст без команды: описание для правки фото или вопрос 'фото или видео?'."""
    prompt = (update.message.text or "").strip()
    if not prompt:
        return

    # Если до этого выбрали "Изменить фото" без подписи, этот текст и есть описание правки
    waiting_file = context.user_data.pop("edit_wait", None)
    if waiting_file:
        await do_generate(
            context.bot, update.message.chat_id, update.effective_user.id,
            "edit", prompt, file_id=waiting_file,
        )
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

@allowed_only
async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пользователь прислал фото: спрашиваем, что с ним сделать."""
    msg = update.message
    if msg.photo:
        file_id = msg.photo[-1].file_id
    elif msg.document and (msg.document.mime_type or "").startswith("image/"):
        file_id = msg.document.file_id
    else:
        return

    caption = (msg.caption or "").strip()
    context.user_data.pop("edit_wait", None)  # новое фото отменяет старое ожидание

    pending = context.application.bot_data.setdefault("pending_photos", {})
    msg_id = msg.message_id
    pending[(msg.chat_id, msg_id)] = (update.effective_user.id, file_id, caption)
    while len(pending) > 200:
        pending.pop(next(iter(pending)))

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🖼 Изменить фото", callback_data=f"e:{msg_id}"),
        InlineKeyboardButton("🎬 Видео из фото", callback_data=f"a:{msg_id}"),
    ]])
    await msg.reply_text("Что сделать с этим фото?", reply_markup=keyboard)

async def choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопки Фото / Видео после текстового описания."""
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

async def photo_choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопки 'Изменить фото' / 'Видео из фото' после присланного фото."""
    q = update.callback_query
    if q is None or q.message is None:
        return
    if not is_allowed(q.from_user.id):
        await q.answer("У тебя нет доступа", show_alert=True)
        return

    kind_code, msg_id = q.data.split(":", 1)
    pending = context.application.bot_data.get("pending_photos", {})
    key = (q.message.chat_id, int(msg_id))
    item = pending.get(key)
    if item is None:
        await q.answer("Фото устарело, отправь его ещё раз.", show_alert=True)
        return

    owner_id, file_id, caption = item
    if q.from_user.id != owner_id:
        await q.answer("Эти кнопки не для тебя.", show_alert=True)
        return

    pending.pop(key, None)
    await q.answer()
    try:
        await q.message.delete()
    except Exception:
        pass

    chat_id = q.message.chat_id
    user_id = q.from_user.id

    if kind_code == "a":
        # Видео из фото: подпись (если есть) описывает, что должно происходить в кадре
        await do_generate(context.bot, chat_id, user_id, "photovideo", caption, file_id=file_id)
        return

    # Изменить фото: нужно описание
    if caption:
        await do_generate(context.bot, chat_id, user_id, "edit", caption, file_id=file_id)
    else:
        context.user_data["edit_wait"] = file_id
        await context.bot.send_message(
            chat_id,
            "Напиши, что изменить на фото. Например: сделай в стиле аниме",
        )

def main():
    if not BOT_TOKEN:
        raise RuntimeError("Переменная BOT_TOKEN не задана в Environment на Render")

    threading.Thread(target=run_flask, daemon=True).start()
    if KEEP_ALIVE_MIN > 0:
        threading.Thread(target=keep_alive_loop, daemon=True).start()
    if RESTART_EVERY_MIN > 0:
        threading.Thread(target=restart_loop, daemon=True).start()
    asyncio.set_event_loop(asyncio.new_event_loop())

    # concurrent_updates: пока одному человеку делается видео, бот отвечает остальным
    application = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(True).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("id", my_id))
    application.add_handler(CommandHandler(["picture", "photo", "image", "pic"], picture_command))
    application.add_handler(CommandHandler("video", video_command))
    application.add_handler(CommandHandler("models", models_command))
    application.add_handler(CallbackQueryHandler(choice_callback, pattern=r"^[pv]:\d+$"))
    application.add_handler(CallbackQueryHandler(photo_choice_callback, pattern=r"^[ea]:\d+$"))
    application.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, photo_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    application.run_polling()

if __name__ == '__main__':
    main()
