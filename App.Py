import os
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# Сюда вставьте ваш токен из BotFather (в кавычках)
BOT_TOKEN = "8859800865:AAGEBuRewSMS_QiYM0rYk-7KILfMJYoR1dE"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Привет! Я твой бот. Напиши мне что-нибудь, и я повторю.')

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Бот просто повторяет текст, который вы ему отправили
    await update.message.reply_text(update.message.text)

if __name__ == '__main__':
    # Создаём приложение с вашим токеном
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    # Добавляем команду /start
    app.add_handler(CommandHandler("start", start))
    
    # Добавляем обработчик всех текстовых сообщений
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
    
    # Запускаем бота
    app.run_polling()
