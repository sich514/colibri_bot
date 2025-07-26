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

from referral import (
    init_referral_db,
    process_referral,
    get_bonus_quota,
    get_referral_stats,
    get_referral_link
)

# 🔐 Загрузка ключей
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

client = OpenAI()
DB_PATH = "meals.db"
WHITELIST = [411134984, 930120924, 242606188, 638538033]
MAX_REQUESTS_PER_DAY = 5


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Создаём таблицу meals с нужными полями
    cursor.execute("""CREATE TABLE IF NOT EXISTS meals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        date TEXT,
        calories INTEGER,
        proteins INTEGER,
        fats INTEGER,
        carbs INTEGER,
        description TEXT,
        assessment TEXT
    )""")

    # Миграция: добавляем новые поля, если таблица уже существует без них
    try:
        cursor.execute("ALTER TABLE meals ADD COLUMN description TEXT")
    except sqlite3.OperationalError:
        pass

    try:
        cursor.execute("ALTER TABLE meals ADD COLUMN assessment TEXT")
    except sqlite3.OperationalError:
        pass

    # Остальные таблицы
    cursor.execute("""CREATE TABLE IF NOT EXISTS usage_log (
        user_id INTEGER,
        date TEXT,
        count INTEGER,
        PRIMARY KEY (user_id, date)
    )""")

    cursor.execute("""CREATE TABLE IF NOT EXISTS daily_limit (
        user_id INTEGER PRIMARY KEY,
        calorie_limit INTEGER
    )""")

    conn.commit()
    conn.close()


def encode_for_openai(image_bytes: bytes) -> str:
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG")
    b64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
    return f"data:image/jpeg;base64,{b64}"

# 📸 Фото → OpenAI → Макросы
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    date = datetime.date.today().isoformat()
    photo = await update.message.photo[-1].get_file()
    image_bytes = await photo.download_as_bytearray()
    image_url = encode_for_openai(image_bytes)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Лимиты
    if user_id not in WHITELIST:
        cursor.execute("SELECT count FROM usage_log WHERE user_id=? AND date=?", (user_id, date))
        row = cursor.fetchone()
        used = row[0] if row else 0
        extra_limit = get_bonus_quota(user_id)
        daily_limit = MAX_REQUESTS_PER_DAY + extra_limit

        if used >= daily_limit:
            await update.message.reply_text("⛔️ Лимит запросов исчерпан. Пригласите друзей для бонусов!")
            conn.close()
            return

    # ⚙️ Запрос к OpenAI
    response = client.responses.create(
        model="gpt-4.1",
        input=[{
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": (
                        "Ты — дипломированный нутрициолог и эксперт по питанию. "
                        "Твоя задача — проанализировать изображение еды и выдать максимально точную нутриентную оценку. "
                        "Оцени:\n"
                        "1. Калории (ккал)\n"
                        "2. Белки (г)\n"
                        "3. Жиры (г)\n"
                        "4. Углеводы (г)\n\n"
                        "⚠️ Верни только цифры в этом формате, без лишнего текста:\n"
                        "Калории: ..., Белки: ..., Жиры: ..., Углеводы: ...\n\n"
                        "Затем:\n"
                        "- Одним предложением кратко опиши блюдо (что это примерно)\n"
                        "- Одной строкой скажи свое мнение как нутрициолог по этому блюду "
                        "Ты работаешь как профессионал и должен стараться не ошибиться."
                    )
                },
                {"type": "input_image", "image_url": image_url}
            ]
        }]
    )

    import re

    # Разделим ответ на строки
    lines = response.output_text.strip().splitlines()

    # Найдём макросы
    values = re.findall(r'\d+', response.output_text)
    calories = int(values[0]) if len(values) > 0 else 0
    proteins = int(values[1]) if len(values) > 1 else 0
    fats = int(values[2]) if len(values) > 2 else 0
    carbs = int(values[3]) if len(values) > 3 else 0

    # Описание и плюсы/минусы
    description = next((l for l in lines if not re.search(r'\d+', l) and "Калории" not in l), "")
    comment = lines[-1] if len(lines) > 1 else ""

    # Логирование использования
    if user_id not in WHITELIST:
        if row:
            cursor.execute("UPDATE usage_log SET count=count+1 WHERE user_id=? AND date=?", (user_id, date))
        else:
            cursor.execute("INSERT INTO usage_log (user_id, date, count) VALUES (?, ?, 1)", (user_id, date))
        conn.commit()

    conn.close()

    # Кнопка сохранения
    callback_data = f"save:{user_id}:{calories}:{proteins}:{fats}:{carbs}"
    keyboard = [[InlineKeyboardButton("✅ Записать", callback_data=callback_data)]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    reply_text = (
        f"🍽️ Калории: {calories} kcal\n"
        f"💪 Белки: {proteins} г\n"
        f"🥑 Жиры: {fats} г\n"
        f"🍞 Углеводы: {carbs} г\n\n"
        f"📝 Блюдо: {description}\n"
        f"⚖️ Оценка: {comment}"
    )

    await update.message.reply_text(reply_text, reply_markup=reply_markup)



async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data.startswith("save:"):
        parts = query.data.split(":")
        user_id = int(parts[1])
        cal, prot, fat, carb = map(int, parts[2:6])
        desc = parts[6] if len(parts) > 6 else ""
        assess = parts[7] if len(parts) > 7 else ""
        date = datetime.date.today().isoformat()

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        cursor.execute("""INSERT INTO meals (
            user_id, date, calories, proteins, fats, carbs, description, assessment
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                       (user_id, date, cal, prot, fat, carb, desc, assess))

        conn.commit()

        # Остаток до лимита
        cursor.execute("SELECT SUM(calories) FROM meals WHERE user_id=? AND date=?", (user_id, date))
        total_today = cursor.fetchone()[0] or 0
        cursor.execute("SELECT calorie_limit FROM daily_limit WHERE user_id=?", (user_id,))
        limit_row = cursor.fetchone()
        conn.close()

        if limit_row:
            daily_limit = limit_row[0]
            remaining = max(0, daily_limit - total_today)
            await query.edit_message_text(
                f"✅ Записано: {cal} kcal ({date})\n"
                f"⏳ До дневного лимита осталось: {remaining} kcal"
            )
        else:
            await query.edit_message_text(f"✅ Записано: {cal} kcal ({date})")


# 🧭 Стартовое меню + обработка реферала
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    start_param = context.args[0] if context.args else None

    process_referral(user_id, start_param)

    keyboard = [["📅 Статистика за сегодня"], ["📈 Статистика за все дни"], ["🎯 Указать дневной лимит"], ["🎁 Бесплатные запросы"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text("📋 Добро пожаловать! Я помогу тебе ориентироваться, сколько калорий ты потребляешь. Просто пришли фотографию своего блюда, чтобы начать, или выбери действие в меню:", reply_markup=reply_markup)

# 📊 Статистика и лимит
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
        await update.message.reply_text(f"📈 История потребления:\n\n{report}")

    elif text == "🎁 Бесплатные запросы":
        bot_username = context.bot.username
        ref_link = get_referral_link(bot_username, user_id)
        invited = get_referral_stats(user_id)
        remaining = max(0, 5 - invited)

        await update.message.reply_text(
            f"👥 Вы пригласили: {invited} друзей\n"
            f"🎯 Осталось: {remaining} до бонуса\n\n"
            f"🔗 Ваша ссылка:\n{ref_link}\n\n"
            "Приведи 5 друзей и получи 150 бесплатных запросов!"
        )

    elif text == "🎯 Указать дневной лимит":
        context.user_data["awaiting_limit"] = True
        await update.message.reply_text("Введите дневной лимит калорий (только число). Чтобы отключить лимит — введите 0.")
        return

    elif context.user_data.get("awaiting_limit"):
        if not text.isdigit():
            await update.message.reply_text("⚠️ Введите только число. Повторите попытку, нажав «🎯 Указать дневной лимит».")
            context.user_data["awaiting_limit"] = False
            return

        limit = int(text)
        if limit == 0:
            cursor.execute("DELETE FROM daily_limit WHERE user_id=?", (user_id,))
            await update.message.reply_text("🛑 Лимит калорий отключён.")
        else:
            cursor.execute("REPLACE INTO daily_limit (user_id, calorie_limit) VALUES (?, ?)", (user_id, limit))
            await update.message.reply_text(f"✅ Дневной лимит установлен: {limit} kcal")

        conn.commit()
        context.user_data["awaiting_limit"] = False
        conn.close()
        return

    conn.close()

# 🚀 Запуск
if __name__ == "__main__":
    init_db()
    init_referral_db()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))
    app.run_polling()
