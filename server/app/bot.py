import telebot
from threading import Thread

from .config import Config
from .logger import log
from .jobs import JOBS
from .file_utils import extract_text
from .jobs import create_job, process_job, start_result_monitor
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

USER_CHOICES = {}   # chat_id → {"format": "...", "pending_file": bytes}


bot = telebot.TeleBot(Config.BOT_TOKEN, parse_mode="HTML")


@bot.message_handler(commands=["start"])
def start_cmd(message):
    log.info(f"/start от {message.chat.id}")
    bot.reply_to(message, "👋 Привет! Это бот CinematicWizard. Можешь отправлять pdf или docx файлы сценариев!")


@bot.message_handler(content_types=["document"])
def handle_document(message):
    doc = message.document
    chat_id = message.chat.id

    log.info(f"Получен файл {doc.file_name} от {chat_id}")

    name = doc.file_name.lower()
    if not (name.endswith(".pdf") or name.endswith(".docx")):
        bot.reply_to(message, "Отправь PDF или DOCX 🙏")
        return

    file_info = bot.get_file(doc.file_id)
    file_bytes = bot.download_file(file_info.file_path)

    if len(file_bytes) > Config.MAX_FILE_SIZE:
        bot.reply_to(message, "Файл слишком большой (до 50MB)")
        return

    # сохраняем файл И имя
    USER_CHOICES[chat_id] = {
        "file": file_bytes,
        "filename": doc.file_name
    }

    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("📄 CSV", callback_data="fmt_csv"))
    kb.add(InlineKeyboardButton("📊 XLSX", callback_data="fmt_xlsx"))
    kb.add(InlineKeyboardButton("📄📊 Оба", callback_data="fmt_both"))

    bot.send_message(chat_id, "В каком формате прислать результат?", reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data.startswith("fmt_"))
def handle_format_choice(call):
    chat_id = call.message.chat.id

    if chat_id not in USER_CHOICES:
        bot.answer_callback_query(call.id, "Нет файла для обработки 😕")
        return

    fmt = call.data.replace("fmt_", "")
    USER_CHOICES[chat_id]["format"] = fmt

    bot.answer_callback_query(call.id)
    bot.send_message(chat_id, "🛠 Начинаю обработку файла...")

    # извлекаем файл
    file_bytes = USER_CHOICES[chat_id]["file"]

    filename = USER_CHOICES[chat_id]["filename"]

    try:
        text = extract_text(file_bytes, filename)

    except Exception as e:
        log.error(f"Ошибка чтения файла: {e}")
        bot.send_message(chat_id, "Ошибка чтения файла 😞")
        return

    # запускаем задачу
    job_id = create_job(
        text=text,
        preset="full",
        custom_columns=None,
        chat_id=chat_id,
        msg_id=call.message.message_id
    )
    JOBS[job_id]["desired_format"] = fmt  # <── сохраняем формат в задаче

    Thread(target=process_job, args=(job_id,), daemon=True).start()



def start_bot():
    log.info("Telegram bot запущен")
    start_result_monitor(bot)
    bot.infinity_polling()
