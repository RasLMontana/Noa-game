"""Telegram-бот, который открывает игру про Ноа как Mini App."""
import asyncio
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)

TOKEN = os.environ["BOT_TOKEN"]      # токен от @BotFather
WEBAPP_URL = os.environ["WEBAPP_URL"]  # https-адрес, где лежит index.html
# Telegram ID администраторов через запятую (команда /stats). Свой ID покажет команда /id
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}
DB_PATH = os.environ.get("STATS_DB", str(Path(__file__).with_name("noa_stats.db")))
TZ_HOURS = int(os.environ.get("STATS_TZ_HOURS", "7"))  # часовой пояс для «сегодня» (7 = Красноярск)

dp = Dispatcher()
WELCOME_IMG = Path(__file__).with_name("welcome.jpg")


def play_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⚔️ Играть", web_app=WebAppInfo(url=WEBAPP_URL))]
        ]
    )


# ---------- Статистика пользователей ----------
def db_init() -> None:
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS users ("
            "id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, "
            "first_seen INTEGER, last_seen INTEGER, starts INTEGER DEFAULT 0, msgs INTEGER DEFAULT 0)"
        )


def track(user, is_start: bool = False) -> None:
    if user is None or user.is_bot:
        return
    now = int(time.time())
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            "INSERT INTO users (id, username, first_name, first_seen, last_seen, starts, msgs) "
            "VALUES (?, ?, ?, ?, ?, ?, 1) "
            "ON CONFLICT(id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name, "
            "last_seen=excluded.last_seen, starts=starts+?, msgs=msgs+1",
            (user.id, user.username, user.first_name, now, now, 1 if is_start else 0, 1 if is_start else 0),
        )


def day_start(days_ago: int = 0) -> int:
    tz = timezone(timedelta(hours=TZ_HOURS))
    d = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_ago)
    return int(d.timestamp())


def stats_text() -> str:
    t0, t7 = day_start(0), day_start(6)
    with sqlite3.connect(DB_PATH) as c:
        q = lambda sql, *a: c.execute(sql, a).fetchone()[0] or 0
        total = q("SELECT COUNT(*) FROM users")
        new_today = q("SELECT COUNT(*) FROM users WHERE first_seen>=?", t0)
        new_week = q("SELECT COUNT(*) FROM users WHERE first_seen>=?", t7)
        act_today = q("SELECT COUNT(*) FROM users WHERE last_seen>=?", t0)
        act_week = q("SELECT COUNT(*) FROM users WHERE last_seen>=?", t7)
        starts = q("SELECT SUM(starts) FROM users")
        last = c.execute(
            "SELECT first_name, username, first_seen FROM users ORDER BY first_seen DESC LIMIT 5"
        ).fetchall()
    tz = timezone(timedelta(hours=TZ_HOURS))
    lines = [
        "📊 Статистика бота",
        f"Всего пользователей: {total}",
        f"Новых сегодня: {new_today}",
        f"Новых за 7 дней: {new_week}",
        f"Активных сегодня: {act_today}",
        f"Активных за 7 дней: {act_week}",
        f"Запусков /start: {starts}",
    ]
    if last:
        lines.append("\nПоследние новые:")
        for name, uname, ts in last:
            when = datetime.fromtimestamp(ts, tz).strftime("%d.%m %H:%M")
            lines.append(f"• {name or 'без имени'}" + (f" @{uname}" if uname else "") + f" ({when})")
    lines.append("\nУчитываются те, кто писал боту. Открытие игры кнопкой «Играть» бот не видит.")
    return "\n".join(lines)


@dp.message.outer_middleware()
async def stats_middleware(handler, event, data):
    try:
        is_start = bool(event.text and event.text.startswith("/start"))
        track(event.from_user, is_start)
    except Exception:
        logging.exception("stats tracking failed")
    return await handler(event, data)


@dp.message(Command("id"))
async def my_id(message: Message) -> None:
    await message.answer(f"Ваш Telegram ID: {message.from_user.id}")


@dp.message(Command("stats"))
async def stats_cmd(message: Message) -> None:
    uid = message.from_user.id
    if not ADMIN_IDS:
        await message.answer(
            f"Статистика не настроена. Ваш ID: {uid}.\n"
            "Задайте переменную ADMIN_IDS с этим числом и перезапустите бота."
        )
        return
    if uid not in ADMIN_IDS:
        return
    await message.answer(stats_text())


@dp.message(CommandStart())
async def start(message: Message) -> None:
    name = message.from_user.first_name if message.from_user else "путник"
    text = (
        f"Добро пожаловать, {name}! 🌊\n\n"
        "«Ноа: Туманный Архипелаг» — приключенческая RPG прямо в Telegram.\n\n"
        "Ты очнулся на берегу, над головой погасший Маяк, а в кармане светится осколок. "
        "Кто ты, не помнишь, но тебя зовут Ноа. Вспомни своё прошлое и верни свет Маяку.\n\n"
        "⚔️ Дела, походы и ночной дозор\n"
        "🔨 Кузница: оружие, броня и редкие комплекты\n"
        "👹 Бои с боссами, руда и Жемчужины Тумана\n"
        "🐾 Питомцы, которых можно растить\n"
        "📚 Бестиарий и событие каждого дня\n\n"
        "Прогресс сохраняется в твоём аккаунте Telegram. Нажми «Играть»! 👇"
    )
    if WELCOME_IMG.exists():
        await message.answer_photo(FSInputFile(WELCOME_IMG), caption=text, reply_markup=play_keyboard())
    else:
        await message.answer(text, reply_markup=play_keyboard())


@dp.message(Command("play"))
async def play(message: Message) -> None:
    await message.answer("Открываю игру:", reply_markup=play_keyboard())


@dp.message(Command("help"))
async def help_cmd(message: Message) -> None:
    await message.answer(
        "Как играть:\n"
        "• Дела и походы дают опыт, монеты и осколки уникальных предметов.\n"
        "• В лавке покупай броню, оружие и зелья энергии.\n"
        "• В кузнице прокачивай предметы.\n"
        "• Боссы открываются с 2 по 16 уровень, всего их восемь.\n\n"
        "Команда /play открывает игру.",
        reply_markup=play_keyboard(),
    )


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    db_init()
    bot = Bot(token=TOKEN)
    await bot.set_my_commands(
        [
            BotCommand(command="play", description="Открыть игру"),
            BotCommand(command="help", description="Как играть"),
        ]
    )
    await bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(text="Играть", web_app=WebAppInfo(url=WEBAPP_URL))
    )
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
