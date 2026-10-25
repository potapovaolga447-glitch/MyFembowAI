import os
import threading
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# Берем токен из переменных окружения Render (безопаснее)
BOT_TOKEN = os.environ.get("BOT_TOKEN")

# 1. Создаем Flask-приложение для "занятия" порта
app = Flask(__name__)

@app.route('/')
def index():
    return "Bot is running"

@app.route('/health')
def health():
    return "OK"

# 2. Логика вашего бота
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Привет! Я твой бот. Напиши мне что-нибудь, и я повторю.')

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(update.message.text)

def run_bot():
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
    application.run_polling()

# 3. Запускаем бота в фоновом потоке
threading.Thread(target=run_bot, daemon=True).start()

if __name__ == '__main__':
    # Слушаем порт, который даст Render
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
