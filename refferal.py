import sqlite3
import datetime
import os
from telegram import Bot

DB_PATH = "meals.db"
BONUS_THRESHOLD = 5
BONUS_QUOTA = 150


def init_referral_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            inviter_id INTEGER,
            invited_id INTEGER PRIMARY KEY,
            timestamp TEXT
        )
    """)
    conn.commit()
    conn.close()


def process_referral(invited_id: int, start_param: str):
    if not start_param or not start_param.startswith("ref_"):
        return

    inviter_id = int(start_param.replace("ref_", ""))
    if inviter_id == invited_id:
        return  # защита от self-ref

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("SELECT 1 FROM referrals WHERE invited_id = ?", (invited_id,))
    already_referred = cursor.fetchone()

    if not already_referred:
        cursor.execute(
            "INSERT INTO referrals (inviter_id, invited_id, timestamp) VALUES (?, ?, ?)",
            (inviter_id, invited_id, datetime.datetime.now().isoformat())
        )
        conn.commit()

        # Проверяем, достиг ли порог
        cursor.execute("SELECT COUNT(*) FROM referrals WHERE inviter_id = ?", (inviter_id,))
        count = cursor.fetchone()[0]
        if count == BONUS_THRESHOLD:
            token = os.getenv("TELEGRAM_TOKEN")
            if token:
                bot = Bot(token)
                bot.send_message(
                    chat_id=inviter_id,
                    text=f"🎉 Поздравляем! Вы пригласили {BONUS_THRESHOLD} друзей и получили {BONUS_QUOTA} бонусных запросов."
                )

    conn.close()


def get_referral_stats(user_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM referrals WHERE inviter_id = ?", (user_id,))
    count = cursor.fetchone()[0]
    conn.close()
    return count


def get_bonus_quota(user_id: int) -> int:
    invited = get_referral_stats(user_id)
    return BONUS_QUOTA if invited >= BONUS_THRESHOLD else 0


def get_referral_link(bot_username: str, user_id: int) -> str:
    return f"https://t.me/{bot_username}?start=ref_{user_id}"
