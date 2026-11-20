import os
import io
import sys
import json
import time
import base64
import asyncio
import logging
import secrets
import threading
import tempfile
import subprocess
import re
import imageio_ffmpeg
from datetime import datetime, timezone
from functools import wraps, lru_cache

import requests
from gradio_client import Client, handle_file
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
# Cloudflare Workers AI: бесплатные фото (около 50-60 штук в день на весь бот)
CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "").strip()
CF_API_TOKEN = os.environ.get("CF_API_TOKEN", "").strip()
CF_MODEL = os.environ.get("CF_MODEL", "@cf/black-forest-labs/flux-1-schnell").strip()
CF_OK = bool(CF_ACCOUNT_ID and CF_API_TOKEN)

# Hugging Face: бесплатный публичный ZeroGPU Space (без Inference Providers)
HF_TOKEN = os.environ.get("HF_TOKEN", "").strip()
HF_SPACE = os.environ.get("HF_SPACE", "Lightricks/LTX-2.5").strip()

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
# Лимит генераций в день для каждого человека (0 = без лимита). Личный лимит админа главнее.
DEFAULT_DAILY_LIMIT = int_env("DEFAULT_DAILY_LIMIT", 0)

# Белый список: ID через запятую. Если пусто, бот доступен всем.
def parse_allowed_ids(raw: str):
    ids = set()
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids

ALLOWED_IDS = parse_allowed_ids(os.environ.get("ALLOWED_USER_IDS", ""))
# Админы: ID через запятую. Они обходят белый список и лимиты, управляют лимитами.
ADMIN_IDS = parse_allowed_ids(os.environ.get("ADMIN_IDS", ""))

def is_allowed(user_id: int) -> bool:
    return user_id in ADMIN_IDS or not ALLOWED_IDS or user_id in ALLOWED_IDS

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

# ---------- Лимиты и паузы (хранятся в файле, переживают самоперезапуск) ----------

STATE_FILE = os.environ.get("STATE_FILE", "/tmp/bot_state.json")
STATE_LOCK = threading.Lock()
STATE = {"limits": {}, "pauses": {}, "usage": {}}

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for key in STATE:
            STATE[key] = data.get(key, {})
    except FileNotFoundError:
        pass
    except Exception:
        log.exception("Не удалось прочитать файл с лимитами")

def save_state():
    """Сохраняет состояние. Вызывать, когда STATE_LOCK уже взят."""
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(STATE, f)
        os.replace(tmp, STATE_FILE)
    except Exception:
        log.exception("Не удалось сохранить файл с лимитами")

def today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")

def user_limit(user_id: int):
    """Лимит в день для человека или None, если лимита нет."""
    with STATE_LOCK:
        value = STATE["limits"].get(str(user_id))
    if value is not None:
        return int(value)
    return DEFAULT_DAILY_LIMIT if DEFAULT_DAILY_LIMIT > 0 else None

def used_today(user_id: int) -> int:
    with STATE_LOCK:
        info = STATE["usage"].get(str(user_id))
    if info and info.get("date") == today_str():
        return int(info.get("count", 0))
    return 0

def pause_left_seconds(user_id: int) -> int:
    with STATE_LOCK:
        until = float(STATE["pauses"].get(str(user_id), 0))
    return max(0, int(until - time.time()))

def minutes_text(seconds: int) -> str:
    return f"{max(1, (seconds + 59) // 60)} мин."

def check_access(user_id: int):
    """Можно ли человеку генерировать сейчас. Возвращает (можно, текст отказа)."""
    if user_id in ADMIN_IDS:
        return True, ""
    left = pause_left_seconds(user_id)
    if left > 0:
        return False, f"Тебе временно нельзя генерировать. Осталось примерно {minutes_text(left)}"
    limit = user_limit(user_id)
    if limit is not None and used_today(user_id) >= limit:
        return False, f"Дневной лимит исчерпан ({limit}). Он обновится завтра."
    return True, ""

def record_use(user_id: int):
    """Считает успешную генерацию в дневном лимите."""
    if user_id in ADMIN_IDS:
        return
    day = today_str()
    with STATE_LOCK:
        key = str(user_id)
        info = STATE["usage"].get(key)
        if not info or info.get("date") != day:
            info = {"date": day, "count": 0}
        info["count"] += 1
        STATE["usage"][key] = info
        for old in [k for k, v in STATE["usage"].items() if v.get("date") != day]:
            STATE["usage"].pop(old, None)
        save_state()

# ---------- Временные картинки для работы с твоим фото ----------
# Чтобы ИИ мог получить твоё фото, бот на несколько минут выкладывает его
# по секретной одноразовой ссылке на своём сервисе и сразу удаляет после работы.
# Ссылку на файл из Telegram (в ней есть токен бота) наружу НЕ отдаём.

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

# ---------- Белый список и админы ----------

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

def admin_only(func):
    """Пускает к команде только админов."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.message is None or update.effective_user is None:
            return
        if update.effective_user.id not in ADMIN_IDS:
            await update.message.reply_text("Эта команда только для администраторов.")
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

# ---------- Запросы к сервисам ----------

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

def fetch_cloudflare_image(english: str):
    """Фото через Cloudflare Workers AI. Возвращает (байты или None, причина ошибки)."""
    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/run/{CF_MODEL}"
    headers = {"Authorization": f"Bearer {CF_API_TOKEN}"}
    payload = {"prompt": english[:2000], "steps": 4}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=90)
    except requests.RequestException as e:
        log.warning("cloudflare: ошибка запроса: %s", e)
        return None, f"ошибка сети ({type(e).__name__})"

    try:
        data = r.json()
    except ValueError:
        if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
            return r.content, ""
        log.warning("cloudflare: HTTP %s, ответ: %s", r.status_code, r.text[:300])
        return None, f"HTTP {r.status_code}: {r.text[:120]}"

    if r.status_code == 200 and isinstance(data, dict):
        result = data.get("result")
        b64 = result.get("image") if isinstance(result, dict) else None
        if b64:
            try:
                return base64.b64decode(b64), ""
            except Exception:
                return None, "не удалось разобрать картинку"

    msg = ""
    errors = data.get("errors") if isinstance(data, dict) else None
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        msg = errors[0].get("message", "")
    log.warning("cloudflare: HTTP %s, ответ: %s", r.status_code, str(data)[:300])
    return None, f"HTTP {r.status_code}: {msg or str(data)[:120]}"[:200]

def make_image(prompt: str):
    """Создаёт фото только через настроенный Cloudflare Workers AI."""
    if not CF_OK:
        return None, ["генерация фото отключена: Cloudflare Workers AI не настроен"]
    english = translate_to_english(prompt) + QUALITY_SUFFIX
    data, err = fetch_cloudflare_image(english)
    if not data:
        return None, [f"cloudflare: {err}"]
    try:
        return add_label(data), []
    except Exception:
        log.exception("Не удалось добавить надпись")
        return data, []

# ---------- Генерация видео и изменение фото ----------

def add_video_label(video_bytes: bytes) -> bytes:
    """Накладывает фирменную надпись на видео. Если обработка не удалась — выбрасывает ошибку."""
    ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
    with tempfile.TemporaryDirectory() as workdir:
        input_path = os.path.join(workdir, "input.mp4")
        watermark_path = os.path.join(workdir, "watermark.png")
        output_path = os.path.join(workdir, "watermarked.mp4")
        with open(input_path, "wb") as source:
            source.write(video_bytes)

        probe = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-i", input_path],
            capture_output=True, text=True, timeout=30,
        )
        metadata = probe.stderr or probe.stdout or ""
        match = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", metadata, re.DOTALL)
        if not match:
            raise RuntimeError("не удалось определить размер видео")
        width, height = int(match.group(1)), int(match.group(2))
        watermark = label_layer(width)
        watermark.save(watermark_path, format="PNG")
        margin = label_margin(width)

        result = subprocess.run(
            [
                ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error",
                "-i", input_path, "-i", watermark_path,
                "-filter_complex",
                f"[0:v][1:v]overlay=x=main_w-overlay_w-{margin}:y=main_h-overlay_h-{margin}:format=auto[v]",
                "-map", "[v]", "-map", "0:a?",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart", output_path,
            ],
            capture_output=True, text=True, timeout=240,
        )
        if result.returncode != 0 or not os.path.isfile(output_path):
            detail = (result.stderr or "неизвестная ошибка ffmpeg").strip()[-500:]
            raise RuntimeError(f"не удалось наложить водяной знак: {detail}")
        with open(output_path, "rb") as rendered:
            return rendered.read()


def _make_huggingface_video(prompt: str, photo_bytes: bytes = None):
    """Видео через публичный Lightricks LTX-2.5 ZeroGPU Space, без платных Inference Providers."""
    if not HF_TOKEN:
        return None, ["не задан HF_TOKEN в Environment Render"]
    prompt = (translate_to_english(prompt) or prompt).strip() + VIDEO_SUFFIX
    temp_image_path = None
    try:
        image_input = None
        if photo_bytes:
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as temp_image:
                temp_image.write(photo_bytes)
                temp_image_path = temp_image.name
            image_input = handle_file(temp_image_path)

        client = Client(HF_SPACE, token=HF_TOKEN, verbose=False)
        # Публичная функция run Space: prompt, image, height, width, duration,
        # seed, decoder, auto length, random seed, prompt enhancement.
        result = client.predict(
            prompt, image_input, 512, 512, 2.0, 42, "conv",
            False, True, False,
            api_name="/run",
        )
        video_result = result[0] if isinstance(result, (list, tuple)) else result
        if isinstance(video_result, dict):
            video_path = video_result.get("path") or video_result.get("name")
        else:
            video_path = video_result
        if not video_path or not os.path.isfile(str(video_path)):
            return None, ["Hugging Face вернул результат без доступного видеофайла"]
        with open(video_path, "rb") as video_file:
            raw_video = video_file.read()
        # Не отправляем оригинал без фирменной надписи: сначала обязательно обрабатываем.
        watermarked_video = add_video_label(raw_video)
        return watermarked_video, []
    except Exception as e:
        log.exception("Hugging Face ZeroGPU: ошибка генерации")
        message = str(e).replace(HF_TOKEN, "[скрытый токен]") if HF_TOKEN else str(e)
        return None, [f"Hugging Face ZeroGPU: {type(e).__name__}: {message[:220]}"]
    finally:
        if temp_image_path:
            try:
                os.remove(temp_image_path)
            except OSError:
                pass

def make_edit(prompt: str, photo_bytes: bytes):
    return None, ["изменение фото временно отключено: бесплатный сервис ещё не подключён"]

def make_video(prompt: str):
    return _make_huggingface_video(prompt)

def make_photo_video(prompt: str, photo_bytes: bytes):
    prompt = prompt or DEFAULT_PHOTO_VIDEO_PROMPT
    return _make_huggingface_video(prompt, photo_bytes)

def format_errors(errors) -> str:
    if not errors:
        return "Подробностей нет, смотри логи в Render."
    lines = [f"— {e}" for e in errors[:6]]
    return ("Что пошло не так:\n" + "\n".join(lines))[:3500]

def fetch_models_text() -> str:
    restart = f"каждые {RESTART_EVERY_MIN} мин" if RESTART_EVERY_MIN > 0 else "выключен"
    ping = f"каждые {KEEP_ALIVE_MIN} мин" if KEEP_ALIVE_MIN > 0 else "выключена"
    default_limit = f"{DEFAULT_DAILY_LIMIT} в день" if DEFAULT_DAILY_LIMIT > 0 else "нет"
    lines = [
        f"Cloudflare Workers AI (фото): {'подключён' if CF_OK else 'НЕ подключён'}",
        "Изменение фото: временно недоступно (бесплатный сервис не подключён)",
        "Hugging Face ZeroGPU (видео): " + ("подключён" if HF_TOKEN else "нужен HF_TOKEN"),
        "Видео по тексту: Lightricks LTX-2.5 ZeroGPU",
        "Видео из фото: Lightricks LTX-2.5 ZeroGPU",
        f"Автоперезапуск: {restart}",
        f"Самопроверка: {ping}",
        f"Админов: {len(ADMIN_IDS)}, общий лимит: {default_limit}",
    ]
    return "\n".join(lines)[:3900]

# ---------- Общая логика генерации ----------

async def do_generate(bot, chat_id: int, user_id: int, kind: str, prompt: str = "", file_id: str = None):
    """kind: photo (по описанию), video (по описанию), edit (правка фото), photovideo (видео из фото)."""
    prompt = prompt.strip()[:MAX_PROMPT_LEN]

    allowed, deny_text = check_access(user_id)
    if not allowed:
        await bot.send_message(chat_id, deny_text)
        return

    if user_id in BUSY_USERS:
        await bot.send_message(chat_id, "Подожди, предыдущий запрос ещё делается.")
        return

    BUSY_USERS.add(user_id)
    status = None
    success = False
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
                success = True
            else:
                await bot.send_message(
                    chat_id,
                    "Не получилось сделать видео.\n\n" + format_errors(errors)
                    + "\n\nПлатные сервисы не используются.",
                )

        elif kind == "edit":
            status = await bot.send_message(chat_id, "Меняю фото, подожди 20-60 секунд...")
            await bot.send_chat_action(chat_id, "upload_photo")
            image, errors = await asyncio.to_thread(make_edit, prompt, photo_bytes)
            if image:
                await bot.send_photo(chat_id, photo=image, caption=prompt[:200])
                success = True
            else:
                await bot.send_message(
                    chat_id,
                    "Не получилось изменить фото.\n\n" + format_errors(errors)
                    + "\n\nПлатные сервисы не используются.",
                )

        else:
            status = await bot.send_message(chat_id, "Рисую, подожди 10-30 секунд...")
            await bot.send_chat_action(chat_id, "upload_photo")
            image, errors = await asyncio.to_thread(make_image, prompt)
            if image:
                await bot.send_photo(chat_id, photo=image, caption=prompt[:200])
                success = True
            else:
                await bot.send_message(chat_id, "Не получилось создать картинку.\n\n" + format_errors(errors))
    except Exception:
        log.exception("Ошибка при генерации")
        await bot.send_message(chat_id, "Что-то пошло не так, попробуй ещё раз.")
    finally:
        BUSY_USERS.discard(user_id)
        if success:
            record_use(user_id)
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
        "Или пришли мне своё фото: я изменю его по описанию или сделаю из него видео.\n\n"
        "/mylimit — сколько генераций осталось"
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
async def mylimit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in ADMIN_IDS:
        await update.message.reply_text("Ты администратор: без лимитов и пауз.")
        return
    lines = []
    left = pause_left_seconds(user_id)
    if left > 0:
        lines.append(f"Пауза: генерировать нельзя ещё примерно {minutes_text(left)}")
    limit = user_limit(user_id)
    used = used_today(user_id)
    if limit is None:
        lines.append(f"Сегодня сделано: {used}. Лимита нет.")
    else:
        lines.append(f"Сегодня сделано: {used} из {limit}.")
    await update.message.reply_text("\n".join(lines))

# ---------- Команды админов ----------

@admin_only
async def limit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/limit ID число — личный лимит генераций в день."""
    try:
        target = int(context.args[0])
        amount = int(context.args[1])
        if amount < 0:
            raise ValueError
    except (IndexError, ValueError):
        await update.message.reply_text("Формат: /limit ID число\nНапример: /limit 123456789 5")
        return
    with STATE_LOCK:
        STATE["limits"][str(target)] = amount
        save_state()
    await update.message.reply_text(f"Готово: у {target} теперь лимит {amount} генераций в день.")

@admin_only
async def unlimit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/unlimit ID — убрать личный лимит."""
    try:
        target = int(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("Формат: /unlimit ID")
        return
    with STATE_LOCK:
        removed = STATE["limits"].pop(str(target), None)
        save_state()
    if removed is None:
        await update.message.reply_text(f"У {target} не было личного лимита.")
    else:
        await update.message.reply_text(f"Личный лимит у {target} убран.")

@admin_only
async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/pause ID минуты — запретить генерировать на N минут."""
    try:
        target = int(context.args[0])
        minutes = int(context.args[1])
        if minutes <= 0 or minutes > 10080:
            raise ValueError
    except (IndexError, ValueError):
        await update.message.reply_text(
            "Формат: /pause ID минуты (от 1 до 10080)\nНапример: /pause 123456789 30"
        )
        return
    with STATE_LOCK:
        STATE["pauses"][str(target)] = time.time() + minutes * 60
        save_state()
    await update.message.reply_text(f"Готово: {target} не сможет генерировать {minutes} мин.")

@admin_only
async def unpause_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/unpause ID — снять паузу."""
    try:
        target = int(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("Формат: /unpause ID")
        return
    with STATE_LOCK:
        removed = STATE["pauses"].pop(str(target), None)
        save_state()
    if removed is None:
        await update.message.reply_text(f"У {target} не было паузы.")
    else:
        await update.message.reply_text(f"Пауза у {target} снята.")

@admin_only
async def limits_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/limits — список личных лимитов и активных пауз."""
    now = time.time()
    with STATE_LOCK:
        limits = dict(STATE["limits"])
        pauses = dict(STATE["pauses"])

    default_text = f"{DEFAULT_DAILY_LIMIT} в день" if DEFAULT_DAILY_LIMIT > 0 else "нет"
    lines = [f"Общий лимит для всех: {default_text}", ""]

    if limits:
        lines.append("Личные лимиты:")
        for uid, amount in limits.items():
            lines.append(f"— {uid}: {amount} в день (сегодня сделано {used_today(int(uid))})")
    else:
        lines.append("Личных лимитов нет.")

    active = [(uid, until) for uid, until in pauses.items() if float(until) > now]
    lines.append("")
    if active:
        lines.append("Паузы:")
        for uid, until in active:
            lines.append(f"— {uid}: ещё примерно {minutes_text(int(float(until) - now))}")
    else:
        lines.append("Активных пауз нет.")
    await update.message.reply_text("\n".join(lines))

# ---------- Обработка сообщений и кнопок ----------

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

    load_state()
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
    application.add_handler(CommandHandler("mylimit", mylimit_command))
    application.add_handler(CommandHandler("limit", limit_command))
    application.add_handler(CommandHandler("unlimit", unlimit_command))
    application.add_handler(CommandHandler("pause", pause_command))
    application.add_handler(CommandHandler("unpause", unpause_command))
    application.add_handler(CommandHandler("limits", limits_command))
    application.add_handler(CallbackQueryHandler(choice_callback, pattern=r"^[pv]:\d+$"))
    application.add_handler(CallbackQueryHandler(photo_choice_callback, pattern=r"^[ea]:\d+$"))
    application.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, photo_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    application.run_polling()

if __name__ == '__main__':
    main()
