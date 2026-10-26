import os
import io
import random
import asyncio
import threading
from functools import wraps
from urllib.parse import quote

import requests
from PIL import Image, ImageDraw, ImageFont
from deep_translator import GoogleTranslator
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

BOT_TOKEN = os.environ.get("BOT_TOKEN")

# Белый список: ID пользователей через запятую, например "123456789,987654321"
# Если переменная пустая или не задана, бот доступен всем.
def parse_allowed_ids(raw: str):
    ids = set()
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids

ALLOWED_IDS = parse_allowed_ids(os.environ.get("ALLOWED_USER_IDS", ""))

LABEL_TEXT = "Фембой AI"
LABEL_COLOR = (255, 105, 180)   # розовый
OUTLINE_COLOR = (60, 0, 40)     # тёмная обводка, чтобы текст читался на любом фоне
LABEL_SCALE = 0.5               # размер надписи: 1.0 = прежний, 0.5 = вдвое меньше

# Слова, которые бот сам добавляет к каждому запросу для лучшего качества
QUALITY_SUFFIX = ", highly detailed, high quality, sharp focus, professional, 4k, beautiful lighting, well-formed anatomy"

# Flask нужен, чтобы Render видел открытый порт
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
        if ALLOWED_IDS and user_id not in ALLOWED_IDS:
            await update.message.reply_text(
                f"У тебя нет доступа к этому боту.\nТвой ID: {user_id}"
            )
            return
        await func(update, context)
    return wrapper

# ---------- Шрифт ----------

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

# ---------- Генерация изображения ----------

def translate_to_english(text: str) -> str:
    try:
        return GoogleTranslator(source="auto", target="en").translate(text) or text
    except Exception:
        return text

def add_label(image_bytes: bytes) -> bytes:
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    draw = ImageDraw.Draw(img)

    font_size = max(14, int(img.width / 14 * LABEL_SCALE))
    font = load_font(font_size)
    stroke = max(1, round(img.width / 300 * LABEL_SCALE))
    margin = max(8, int(img.width / 40 * LABEL_SCALE * 2))

    # Размер текста, чтобы поставить его точно в правый нижний угол
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

def make_image(prompt: str):
    """Переводит описание, добавляет качество, скачивает картинку, ставит надпись."""
    english = translate_to_english(prompt) + QUALITY_SUFFIX
    url = f"https://image.pollinations.ai/prompt/{quote(english)}"
    params = {
        "width": 1024,
        "height": 1024,
        "model": "flux",
        "nologo": "true",
        "seed": random.randint(1, 10_000_000),
    }
    try:
        r = requests.get(url, params=params, timeout=90)
        if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
            return add_label(r.content)
    except Exception:
        pass
    return None

# ---------- Команды бота ----------

async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает ID пользователя. Доступна всем, чтобы можно было узнать свой ID."""
    if update.message is None or update.effective_user is None:
        return
    await update.message.reply_text(f"Твой ID: {update.effective_user.id}")

@allowed_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я рисую картинки по описанию.\n\n"
        "Напиши: /image кот в космосе\n"
        "Или просто отправь мне описание текстом."
    )

async def generate(update: Update, prompt: str):
    prompt = prompt.strip()
    if not prompt:
        await update.message.reply_text("Напиши, что нарисовать. Например: /image закат над морем")
        return

    await update.message.reply_text("Рисую, подожди 10-30 секунд...")
    await update.message.chat.send_action("upload_photo")

    image = await asyncio.to_thread(make_image, prompt)

    if image:
        await update.message.reply_photo(photo=image, caption=prompt[:200])
    else:
        await update.message.reply_text(
            "Не получилось создать картинку. Сервис мог быть занят, попробуй через минуту."
        )

@allowed_only
async def image_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await generate(update, " ".join(context.args))

@allowed_only
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await generate(update, update.message.text)

def main():
    if not BOT_TOKEN:
        raise RuntimeError("Переменная BOT_TOKEN не задана в Environment на Render")

    threading.Thread(target=run_flask, daemon=True).start()
    asyncio.set_event_loop(asyncio.new_event_loop())

    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("id", my_id))
    application.add_handler(CommandHandler("image", image_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    application.run_polling()

if __name__ == '__main__':
    main()
