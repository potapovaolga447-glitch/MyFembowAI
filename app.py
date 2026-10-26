import os
import io
import random
import asyncio
import threading
from urllib.parse import quote

import requests
from PIL import Image, ImageDraw, ImageFont
from deep_translator import GoogleTranslator
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

BOT_TOKEN = os.environ.get("BOT_TOKEN")

LABEL_TEXT = "Фембой AI"
LABEL_COLOR = (255, 105, 180)   # розовый
OUTLINE_COLOR = (60, 0, 40)     # тёмная обводка, чтобы текст читался на любом фоне

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
    font = load_font(max(24, img.width // 14))
    margin = img.width // 40
    draw.text(
        (margin, margin),
        LABEL_TEXT,
        font=font,
        fill=LABEL_COLOR,
        stroke_width=max(2, img.width // 300),
        stroke_fill=OUTLINE_COLOR,
    )
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=92)
    return out.getvalue()

def make_image(prompt: str):
    """Переводит описание, скачивает картинку, добавляет надпись."""
    english = translate_to_english(prompt)
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

async def image_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await generate(update, " ".join(context.args))

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await generate(update, update.message.text)

def main():
    if not BOT_TOKEN:
        raise RuntimeError("Переменная BOT_TOKEN не задана в Environment на Render")

    threading.Thread(target=run_flask, daemon=True).start()
    asyncio.set_event_loop(asyncio.new_event_loop())

    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("image", image_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    application.run_polling()

if __name__ == '__main__':
    main()
