import os
import io
import base64
import datetime
import sqlite3
from dotenv import load_dotenv
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from PIL import Image
from openai import OpenAI

# 🔐 Загрузка ключей
load_dotenv()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")

client = OpenAI()
DB_PATH = "meals.db"

WHITELIST = [411134984, 123456789]  # 💎 ID без лимитов
MAX_TOTAL_REQUESTS = 15  # 🔒 Остальным — ограничение

# 🗄️ Инициализация базы
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""CREATE TABLE IF NOT EXISTS meals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        date TEXT,
        calories INTEGER
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS usage_log (
        user_id INTEGER PRIMARY KEY,
        count INTEGER
    )""")
    conn.commit()
    conn.close()

def encode_for_openai(image_bytes: bytes) -> str:
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG")
    b64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
    return f"data:image/jpeg;base64,{b64}"

# 📸 Фото → OpenAI → Калории
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    photo = await update.message.photo[-1].get_file()
    image_bytes = await photo.download_as_bytearray()
    image_url = encode_for_openai(image_bytes)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # 💡 Лимит
    if user_id not in WHITELIST:
        cursor.execute("SELECT count FROM usage_log WHERE user_id=?", (user_id,))
        row = cursor.fetchone()
        used = row[0] if row else 0
        if used >= MAX_TOTAL_REQUESTS:
            await update.message.reply_text("⛔️ Лимит: 15 фото-запросов исчерпан.")
            conn.close()
            return

    # 🔍 Отправка изображения в OpenAI
    response = client.responses.create(
        model="gpt-4.1",
        input=[{
            "role": "user",
            "content": [
                { "type": "input_text", "text": "Если на изображении есть еда — посчитай её калорийность и верни только число. Если еды нет — верни 0." },
                { "type": "input_image", "image_url": image_url }
            ]
        }]
    )

    kcal_raw = response.output_text.strip()
    kcal = ''.join(filter(str.isdigit, kcal_raw)) or "0"

    # ⏳ Увеличение счётчика
    if user_id not in WHITELIST:
        if row:
            cursor.execute("UPDATE usage_log SET count=count+1 WHERE user_id=?", (user_id,))
        else:
            cursor.execute("INSERT INTO usage_log (user_id, count) VALUES (?, 1)")
        conn.commit()

    conn.close()

    # ✅ Кнопка записи
    callback_data = f"save:{user_id}:{kcal}"
    keyboard = [[InlineKeyboardButton("✅ Записать", callback_data=callback_data)]]
    markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(f"🍽️ Калорийность: {kcal} kcal", reply_markup=markup)

# ✅ Обработка кнопки "Записать"
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data.startswith("save:"):
        _, user_id_str, kcal = query.data.split(":")
        user_id = int(user_id_str)
        date = datetime.date.today().isoformat()

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO meals (user_id, date, calories) VALUES (?, ?, ?)",
                       (user_id, date, int(kcal)))
        conn.commit()
        conn.close()

        await query.edit_message_text(f"✅ Записано: {kcal} kcal ({date})")

# 📊 Стартовое меню
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [["📅 Статистика за сегодня"], ["📈 Статистика за все дни"]]
    markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text("📋 Выберите действие:", reply_markup=markup)

# 📊 Текстовая статистика
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    if text == "📅 Статистика за сегодня":
        today = datetime.date.today().isoformat()
        cursor.execute("SELECT SUM(calories) FROM meals WHERE user_id=? AND date=?", (user_id, today))
        total = cursor.fetchone()[0] or 0
        await update.message.reply_text(f"📅 Сегодня: {total} kcal")

    elif text == "📈 Статистика за все дни":
        cursor.execute("SELECT date, SUM(calories) FROM meals WHERE user_id=? GROUP BY date ORDER BY date DESC", (user_id,))
        rows = cursor.fetchall()
        report = "\n".join([f"{date}: {cal} kcal" for date, cal in rows]) or "Нет записей"
        await update.message.reply_text(f"📈 История:\n\n{report}")

    conn.close()

# 🚀 Запуск
if __name__ == "__main__":
    init_db()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))
    app.run_polling()
