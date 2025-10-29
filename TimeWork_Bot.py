import asyncio
import os
import sqlite3
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

# =====================
# CONFIG
# =====================

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8208517891:AAE8OKgyRKAomH0HqlEKPmfYkK0mgGGDSx4")  # <-- вставь свой токен сюда
DB_PATH = "workbot.db"

# =====================
# DATABASE
# =====================

def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db():
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            currency TEXT,
            rate REAL,
            total_hours REAL DEFAULT 0,
            total_earned REAL DEFAULT 0,
            skips INTEGER DEFAULT 0,
            days_off INTEGER DEFAULT 0,
            start_time TEXT,
            advance_total REAL DEFAULT 0
        );
        """
    )
    cur.execute("PRAGMA table_info(users)")
    columns = [r[1] for r in cur.fetchall()]
    if "advance_total" not in columns:
        cur.execute("ALTER TABLE users ADD COLUMN advance_total REAL DEFAULT 0;")
    conn.commit()
    conn.close()


def ensure_user(user_id: int):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()


def get_user(user_id: int) -> sqlite3.Row | None:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT user_id, currency, rate, total_hours, total_earned, skips, days_off, start_time, advance_total
        FROM users WHERE user_id = ?
        """,
        (user_id,),
    )
    row = cur.fetchone()
    conn.close()
    return row


def update_user(user_id: int, **kwargs):
    if not kwargs:
        return
    conn = db_conn()
    cur = conn.cursor()
    fields = ", ".join(f"{k} = ?" for k in kwargs)
    cur.execute(f"UPDATE users SET {fields} WHERE user_id = ?", (*kwargs.values(), user_id))
    conn.commit()
    conn.close()


def increment_field(user_id: int, field: str):
    if field not in ("skips", "days_off"):
        raise ValueError("Invalid field to increment")
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(f"UPDATE users SET {field} = COALESCE({field},0) + 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def reset_user(user_id: int):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE users
        SET total_hours = 0,
            total_earned = 0,
            skips = 0,
            days_off = 0,
            advance_total = 0,
            start_time = NULL
        WHERE user_id = ?
        """,
        (user_id,),
    )
    conn.commit()
    conn.close()


# =====================
# FSM STATES
# =====================

class RegStates(StatesGroup):
    choosing_currency = State()
    entering_rate = State()
    entering_advance = State()


# =====================
# KEYBOARDS
# =====================

def currency_keyboard():
    builder = InlineKeyboardBuilder()
    currencies = [
        ("🇺🇸 USD", "USD"),
        ("🇪🇺 EUR", "EUR"),
        ("🇰🇿 KZT", "KZT"),
        ("🇺🇦 UAH", "UAH"),
        ("🇨🇿 CZK", "CZK"),
        ("🇵🇱 PLN", "PLN"),
    ]
    for label, code in currencies:
        builder.button(text=label, callback_data=f"cur:{code}")
    builder.adjust(3)
    return builder.as_markup()


def profile_keyboard():
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="🟢 Начало рабочего дня", callback_data="act:start"),
        InlineKeyboardButton(text="🔴 Окончание рабочего дня", callback_data="act:end"),
    )
    kb.row(
        InlineKeyboardButton(text="📊 Общий результат", callback_data="act:stats"),
        InlineKeyboardButton(text="💸 Взять аванс", callback_data="act:advance"),
    )
    kb.row(
        InlineKeyboardButton(text="⏸ Пропуск", callback_data="act:skip"),
        InlineKeyboardButton(text="🌴 Выходной", callback_data="act:dayoff"),
    )
    kb.row(
        InlineKeyboardButton(text="🧹 Стереть весь результат", callback_data="act:confirm_reset"),
    )
    return kb.as_markup()


def confirm_reset_keyboard():
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="✅ Да, стереть", callback_data="act:reset_yes"),
        InlineKeyboardButton(text="❌ Нет", callback_data="act:reset_no"),
    )
    return kb.as_markup()


# =====================
# HELPERS
# =====================

def fmt_money(val, cur):
    v = 0.0 if val is None else float(val)
    return f"{v:.2f} {cur or ''}".strip()


def render_profile(row: sqlite3.Row) -> str:
    currency = row["currency"]
    rate = row["rate"]
    total_hours = row["total_hours"] or 0
    total_earned = row["total_earned"] or 0
    skips = row["skips"] or 0
    days_off = row["days_off"] or 0
    start_time = row["start_time"]
    advance_total = row["advance_total"] or 0
    start_line = "Не начат" if not start_time else f"с {start_time} UTC"
    return (
        f"👤 Твой профиль:\n"
        f"• Валюта: {currency or '—'}\n"
        f"• Ставка: {(rate if rate is not None else '—')} {(currency or '')}/ч\n"
        f"• Всего отработано: {float(total_hours):.2f} ч\n"
        f"• Заработано: {fmt_money(total_earned, currency)}\n"
        f"• 💸 Взято авансом: {fmt_money(advance_total, currency)}\n"
        f"• Пропусков: {skips}\n"
        f"• Выходных: {days_off}\n"
        f"• Текущая смена: {start_line}"
    )


# =====================
# BOT LOGIC
# =====================

async def cmd_start(message: types.Message, state: FSMContext):
    ensure_user(message.from_user.id)
    await state.set_state(RegStates.choosing_currency)
    await message.answer("Привет! 👋 В какой валюте будет ставка?", reply_markup=currency_keyboard())


async def choose_currency(callback: types.CallbackQuery, state: FSMContext):
    code = callback.data.split(":", 1)[1]
    update_user(callback.from_user.id, currency=code)
    await state.set_state(RegStates.entering_rate)
    await callback.message.edit_text(f"Отлично! Валюта: {code}. Теперь введи ставку в час (число).")


async def enter_rate(message: types.Message, state: FSMContext):
    try:
        rate = float(message.text.replace(",", "."))
    except ValueError:
        await message.reply("Пожалуйста, введи число, например 10.5")
        return
    update_user(message.from_user.id, rate=rate)
    await state.clear()
    row = get_user(message.from_user.id)
    await message.answer(render_profile(row), reply_markup=profile_keyboard())


async def actions(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    ensure_user(user_id)
    row = get_user(user_id)
    if not row:
        await callback.answer("Сначала запусти /start", show_alert=True)
        return

    currency = row["currency"]
    rate = row["rate"] or 0
    total_hours = row["total_hours"] or 0
    total_earned = row["total_earned"] or 0
    skips = row["skips"] or 0
    days_off = row["days_off"] or 0
    start_time = row["start_time"]
    advance_total = row["advance_total"] or 0

    action = callback.data.split(":", 1)[1]

    if action == "start":
        if start_time:
            await callback.answer("Смена уже запущена.", show_alert=True)
            return
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        update_user(user_id, start_time=now)
        await callback.message.edit_text(
            f"Смена начата в {now} UTC\n\n" + render_profile(get_user(user_id)),
            reply_markup=profile_keyboard(),
        )
        await callback.answer("✅ Начало рабочего дня записано")

    elif action == "end":
        if not start_time:
            await callback.answer("Смена не начата.", show_alert=True)
            return
        start_dt = datetime.fromisoformat(start_time)
        end_dt = datetime.now(timezone.utc)
        hours = round((end_dt - start_dt).total_seconds() / 3600, 2)
        earned = round(hours * rate, 2)
        update_user(
            user_id,
            total_hours=total_hours + hours,
            total_earned=total_earned + earned,
            start_time=None,
        )
        await callback.message.edit_text(
            f"Смена завершена.\nОтработано: {hours:.2f} ч\nЗаработано: {fmt_money(earned, currency)}\n\n"
            + render_profile(get_user(user_id)),
            reply_markup=profile_keyboard(),
        )
        await callback.answer("✅ Конец рабочего дня записан")

    elif action == "stats":
        await callback.message.edit_text(render_profile(row), reply_markup=profile_keyboard())
        await callback.answer()

    elif action == "skip":
        increment_field(user_id, "skips")
        await callback.message.edit_text(
            "⏸ Пропуск добавлен.\n\n" + render_profile(get_user(user_id)),
            reply_markup=profile_keyboard(),
        )
        await callback.answer("✅ Пропуск учтен")

    elif action == "dayoff":
        increment_field(user_id, "days_off")
        await callback.message.edit_text(
            "🌴 Выходной добавлен.\n\n" + render_profile(get_user(user_id)),
            reply_markup=profile_keyboard(),
        )
        await callback.answer("✅ Выходной учтен")

    elif action == "advance":
        await state.set_state(RegStates.entering_advance)
        await callback.message.edit_text("💰 Введи сумму аванса (например: 1500):")
        await callback.answer()

    elif action == "confirm_reset":
        await callback.message.edit_text(
            "⚠️ Ты уверен, что хочешь полностью стереть все данные?\n\nЭто действие нельзя отменить!",
            reply_markup=confirm_reset_keyboard(),
        )
        await callback.answer()

    elif action == "reset_yes":
        reset_user(user_id)
        await callback.message.edit_text(
            "✅ Все данные успешно очищены.\n\n" + render_profile(get_user(user_id)),
            reply_markup=profile_keyboard(),
        )
        await callback.answer("Данные сброшены")

    elif action == "reset_no":
        await callback.message.edit_text(
            "Отмена очистки.\n\n" + render_profile(row),
            reply_markup=profile_keyboard(),
        )
        await callback.answer("❌ Отменено")


async def enter_advance(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text.replace(",", "."))
    except ValueError:
        await message.reply("Пожалуйста, введи число, например 500.0")
        return

    row = get_user(message.from_user.id)
    if not row:
        await message.reply("Сначала запусти /start")
        return

    new_total = (row["advance_total"] or 0) + amount
    update_user(message.from_user.id, advance_total=new_total)

    await state.clear()
    updated = get_user(message.from_user.id)
    await message.answer(
        f"💸 Аванс {fmt_money(amount, updated['currency'])} добавлен!\n\n" + render_profile(updated),
        reply_markup=profile_keyboard(),
    )


# =====================
# MAIN
# =====================

async def main():
    init_db()
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())

    dp.message.register(cmd_start, F.text == "/start")
    dp.callback_query.register(choose_currency, F.data.startswith("cur:"))
    dp.message.register(enter_rate, RegStates.entering_rate)
    dp.callback_query.register(actions, F.data.startswith("act:"))
    dp.message.register(enter_advance, RegStates.entering_advance)

    print("🤖 Bot started...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
