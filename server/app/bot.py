import telebot
from threading import Thread

from .config import Config
from .logger import log
from .file_utils import extract_text
from .jobs import create_job, process_job, start_result_monitor

bot = telebot.TeleBot(Config.BOT_TOKEN, parse_mode="HTML")


@bot.message_handler(commands=["start"])
def start_cmd(message):
    log.info(f"/start от {message.chat.id}")
    bot.reply_to(message, "👋 Привет! Отправь PDF файл сценария.")


@bot.message_handler(content_types=["document"])
def handle_pdf(message):
    doc = message.document
    chat_id = message.chat.id
    log.info(f"Получен файл {doc.file_name} от {chat_id}")

    if not doc.file_name.lower().endswith(".pdf"):
        bot.reply_to(message, "Отправь PDF 🙏")
        return

    file_info = bot.get_file(doc.file_id)
    file_bytes = bot.download_file(file_info.file_path)

    if len(file_bytes) > Config.MAX_FILE_SIZE:
        bot.reply_to(message, "Файл слишком большой (до 50MB)")
        return

    try:
        text = extract_text(file_bytes)
    except Exception:
        bot.reply_to(message, "Не удалось прочитать PDF 😞")
        return

    job_id = create_job(text, "full", None, chat_id, message.message_id)
    bot.reply_to(message, f"📄 Обработка началась!")

    Thread(target=process_job, args=(job_id,), daemon=True).start()


def start_bot():
    log.info("Telegram bot запущен")
    start_result_monitor(bot)
    bot.infinity_polling()
