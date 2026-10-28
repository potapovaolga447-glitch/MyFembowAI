import os
import io
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
            data, e = http_get_media(url, params, "image", 120, f"правка:{model}")
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
        data, err = http_get_media(url, params, "video", 300, f"видео:{model}")
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
            data, e = http_get_media(url, params, "video", 300,
