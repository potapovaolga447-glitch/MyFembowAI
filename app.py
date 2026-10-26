import os
import asyncio
import threading
from urllib.parse import quote

import requests
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

BOT_TOKEN = os.environ.get("BOT_TOKEN")

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

# ---------- Генерация изображения ----------

def fetch_image(prompt: str):
    """Скачивает картинку с Pollinations. Возвращает байты или None."""
    url = f"https://image.pollinations.ai/prompt/{quote(prompt)}"
    params = {"width": 1024, "height": 1024, "model": "flux", "nologo": "true"}
    try:
        r = requests.get(url, params=params, timeout=90)
        if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
            return r.content
    except requests.RequestException:
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

    # requests работает синхронно, поэтому запускаем в отдельном потоке
    image = await asyncio.to_thread(fetch_image, prompt)

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

    # Flask в фоне
    threading.Thread(target=run_flask, daemon=True).start()

    # Event loop для новых версий Python
    asyncio.set_event_loop(asyncio.new_event_loop())

    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("image", image_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    application.run_polling()

if __name__ == '__main__':
    main()
