import asyncio
import logging
import math
import os
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder


# ---------- Настройки (из файла .env или переменных окружения) ----------
def load_env(path=".env"):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()
TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
DB_PATH = os.getenv("DB_PATH", "expenses.db")
TIMEZONE = os.getenv("TIMEZONE", "UTC")
CURRENCY = os.getenv("CURRENCY", "")
REMINDER_HOUR = int(os.getenv("REMINDER_HOUR", "21"))  # -1 = без напоминаний

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

KIND_TITLE = {"expense": "Расходы", "income": "Доходы"}
DEFAULT_CATEGORIES = {
    "expense": ["Еда", "Транспорт", "Доп. расходы", "Непредвиденные", "Кредит"],
    "income": ["Фикс", "KPI", "Прочее"],
}


# ---------- База данных ----------
def run(sql, args=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(sql, args)
        rows = cur.fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def init_db():
    run(
        """CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            UNIQUE(kind, name)
        )"""
    )
    run(
        """CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            category_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL
        )"""
    )
    if not run("SELECT 1 FROM categories LIMIT 1"):
        for kind, names in DEFAULT_CATEGORIES.items():
            for name in names:
                run("INSERT INTO categories(kind, name) VALUES(?, ?)", (kind, name))


# ---------- Вспомогательные функции ----------
def now():
    return datetime.now(ZoneInfo(TIMEZONE))


def fmt(x):
    s = f"{x:,.2f}".replace(",", " ")
    if s.endswith(".00"):
        s = s[:-3]
    return f"{s} {CURRENCY}".strip()


def parse_amount(text):
    try:
        v = float(text.replace(" ", "").replace(",", "."))
    except ValueError:
        return None
    if not math.isfinite(v) or v <= 0:
        return None
    return round(v, 2)


def period_start(code):
    n = now().replace(hour=0, minute=0, second=0, microsecond=0)
    if code == "week":
        n -= timedelta(days=n.weekday())
    elif code == "month":
        n = n.replace(day=1)
    elif code == "year":
        n = n.replace(month=1, day=1)
    return n.strftime("%Y-%m-%d %H:%M:%S")


PERIOD_TITLE = {
    "today": "сегодня",
    "week": "эту неделю",
    "month": "этот месяц",
    "year": "этот год",
}


def totals_since(start):
    rows = run(
        "SELECT kind, SUM(amount) AS s FROM transactions WHERE created_at >= ? GROUP BY kind",
        (start,),
    )
    res = {"expense": 0.0, "income": 0.0}
    for r in rows:
        res[r["kind"]] = r["s"] or 0.0
    return res


async def show(event, text, kb=None):
    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            pass
        await event.answer()
    else:
        await event.answer(text, reply_markup=kb)


# ---------- Клавиатуры ----------
BACK = InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")


def with_back(builder, back=BACK):
    markup = builder.as_markup()
    return InlineKeyboardMarkup(inline_keyboard=markup.inline_keyboard + [[back]])


def main_kb():
    b = InlineKeyboardBuilder()
    b.button(text="📊 Статистика", callback_data="stats")
    b.button(text="💸 Расходы", callback_data="add:expense")
    b.button(text="💰 Доходы", callback_data="add:income")
    b.button(text="⚙️ Настройки", callback_data="settings")
    b.adjust(1, 2, 1)
    return b.as_markup()


def cats_kb(kind, prefix):
    rows = run(
        "SELECT id, name FROM categories WHERE kind=? AND active=1 ORDER BY id",
        (kind,),
    )
    b = InlineKeyboardBuilder()
    for r in rows:
        b.button(text=r["name"], callback_data=f"{prefix}:{r['id']}")
    b.adjust(2)
    return with_back(b)


# ---------- Состояния ----------
class TxFlow(StatesGroup):
    amount = State()
    note = State()


class CatFlow(StatesGroup):
    name = State()


# ---------- Роутеры ----------
public = Router()
router = Router()
router.message.filter(F.from_user.id == OWNER_ID)
router.callback_query.filter(F.from_user.id == OWNER_ID)


@public.message(Command("id"))
async def cmd_id(m: Message):
    await m.answer(f"Ваш Telegram ID: {m.from_user.id}")


# --- Меню ---
@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Главное меню:", reply_markup=main_kb())


@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    t = totals_since(period_start("today"))
    text = (
        "Главное меню\n\n"
        f"Сегодня: расходы {fmt(t['expense'])}, доходы {fmt(t['income'])}"
    )
    await show(cb, text, main_kb())


# --- Добавление записи ---
@router.callback_query(F.data.startswith("add:"))
async def cb_add(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    kind = cb.data.split(":")[1]
    await show(cb, f"{KIND_TITLE[kind]}: выберите категорию", cats_kb(kind, "tx"))


@router.callback_query(F.data.startswith("tx:"))
async def cb_tx(cb: CallbackQuery, state: FSMContext):
    cat_id = int(cb.data.split(":")[1])
    rows = run("SELECT id, kind, name FROM categories WHERE id=?", (cat_id,))
    if not rows:
        await cb.answer("Категория не найдена", show_alert=True)
        return
    c = rows[0]
    await state.set_state(TxFlow.amount)
    await state.update_data(cat_id=c["id"], kind=c["kind"], cat_name=c["name"])
    cancel = InlineKeyboardMarkup(inline_keyboard=[[BACK]])
    await show(cb, f"{c['name']}\n\nВведите сумму:", cancel)


@router.message(TxFlow.amount, F.text)
async def msg_amount(m: Message, state: FSMContext):
    amount = parse_amount(m.text)
    if amount is None:
        await m.answer("Не понял сумму. Введите число, например 25000 или 12.5")
        return
    await state.update_data(amount=amount)
    await state.set_state(TxFlow.note)
    d = await state.get_data()
    if d["kind"] == "income":
        prompt = "Комментарий (например, название KPI) или нажмите «Пропустить»:"
    else:
        prompt = "Комментарий (необязательно) или нажмите «Пропустить»:"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Пропустить", callback_data="skipnote")],
            [BACK],
        ]
    )
    await m.answer(f"Сумма: {fmt(amount)}\n{prompt}", reply_markup=kb)


async def save_tx(event, state: FSMContext, note):
    d = await state.get_data()
    run(
        "INSERT INTO transactions(kind, category_id, amount, note, created_at) VALUES(?,?,?,?,?)",
        (
            d["kind"],
            d["cat_id"],
            d["amount"],
            note,
            now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    await state.clear()
    t = totals_since(period_start("today"))
    sign = "−" if d["kind"] == "expense" else "+"
    text = (
        f"✅ Сохранено: {d['cat_name']} {sign}{fmt(d['amount'])}"
        + (f"\n📝 {note}" if note else "")
        + f"\n\nСегодня: расходы {fmt(t['expense'])}, доходы {fmt(t['income'])}"
    )
    await show(event, text, main_kb())


@router.message(TxFlow.note, F.text)
async def msg_note(m: Message, state: FSMContext):
    await save_tx(m, state, m.text.strip()[:200])


@router.callback_query(F.data == "skipnote", TxFlow.note)
async def cb_skip(cb: CallbackQuery, state: FSMContext):
    await save_tx(cb, state, "")


# --- Статистика ---
@router.callback_query(F.data == "stats")
async def cb_stats(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    b = InlineKeyboardBuilder()
    b.button(text="Сегодня", callback_data="st:today")
    b.button(text="Неделя", callback_data="st:week")
    b.button(text="Месяц", callback_data="st:month")
    b.button(text="Год", callback_data="st:year")
    b.button(text="🕘 Последние записи", callback_data="recent")
    b.adjust(2, 2, 1)
    await show(cb, "Статистика: выберите период", with_back(b))


@router.callback_query(F.data.startswith("st:"))
async def cb_period(cb: CallbackQuery):
    code = cb.data.split(":")[1]
    start = period_start(code)
    rows = run(
        """SELECT t.kind AS kind, c.name AS name, SUM(t.amount) AS s
           FROM transactions t JOIN categories c ON c.id = t.category_id
           WHERE t.created_at >= ?
           GROUP BY t.kind, c.name ORDER BY s DESC""",
        (start,),
    )
    lines = {"expense": [], "income": []}
    total = {"expense": 0.0, "income": 0.0}
    for r in rows:
        lines[r["kind"]].append(f"  • {r['name']}: {fmt(r['s'])}")
        total[r["kind"]] += r["s"]
    text = f"📊 Статистика за {PERIOD_TITLE[code]}\n\n"
    text += f"💸 Расходы: {fmt(total['expense'])}\n"
    text += ("\n".join(lines["expense"]) + "\n") if lines["expense"] else "  нет записей\n"
    text += f"\n💰 Доходы: {fmt(total['income'])}\n"
    text += ("\n".join(lines["income"]) + "\n") if lines["income"] else "  нет записей\n"
    text += f"\n⚖️ Баланс: {fmt(total['income'] - total['expense'])}"
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ К периодам", callback_data="stats")
    await show(cb, text, with_back(b))


# --- Последние записи (с удалением) ---
async def show_recent(cb: CallbackQuery):
    rows = run(
        """SELECT t.id, t.kind, t.amount, t.note, t.created_at, c.name
           FROM transactions t JOIN categories c ON c.id = t.category_id
           ORDER BY t.id DESC LIMIT 8"""
    )
    if not rows:
        b = InlineKeyboardBuilder()
        await show(cb, "Записей пока нет.", with_back(b))
        return
    b = InlineKeyboardBuilder()
    for r in rows:
        sign = "−" if r["kind"] == "expense" else "+"
        date = r["created_at"][5:10]
        b.button(
            text=f"🗑 {date} {r['name']} {sign}{fmt(r['amount'])}",
            callback_data=f"deltx:{r['id']}",
        )
    b.adjust(1)
    await show(cb, "Последние записи. Нажмите, чтобы удалить:", with_back(b))


@router.callback_query(F.data == "recent")
async def cb_recent(cb: CallbackQuery):
    await show_recent(cb)


@router.callback_query(F.data.startswith("deltx:"))
async def cb_deltx(cb: CallbackQuery):
    run("DELETE FROM transactions WHERE id=?", (int(cb.data.split(":")[1]),))
    await show_recent(cb)


# --- Настройки: категории ---
@router.callback_query(F.data == "settings")
async def cb_settings(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    b = InlineKeyboardBuilder()
    b.button(text="➕ Категория расходов", callback_data="catadd:expense")
    b.button(text="➕ Категория доходов", callback_data="catadd:income")
    b.button(text="➖ Удалить категорию расходов", callback_data="catdel:expense")
    b.button(text="➖ Удалить категорию доходов", callback_data="catdel:income")
    b.adjust(1)
    await show(cb, "⚙️ Настройки категорий", with_back(b))


@router.callback_query(F.data.startswith("catadd:"))
async def cb_catadd(cb: CallbackQuery, state: FSMContext):
    kind = cb.data.split(":")[1]
    await state.set_state(CatFlow.name)
    await state.update_data(kind=kind)
    kb = InlineKeyboardMarkup(inline_keyboard=[[BACK]])
    await show(cb, f"Введите название новой категории ({KIND_TITLE[kind].lower()}):", kb)


@router.message(CatFlow.name, F.text)
async def msg_catname(m: Message, state: FSMContext):
    name = m.text.strip()[:40]
    if not name:
        await m.answer("Название не может быть пустым.")
        return
    d = await state.get_data()
    run(
        """INSERT INTO categories(kind, name, active) VALUES(?, ?, 1)
           ON CONFLICT(kind, name) DO UPDATE SET active = 1""",
        (d["kind"], name),
    )
    await state.clear()
    await m.answer(f"✅ Категория «{name}» добавлена.", reply_markup=main_kb())


@router.callback_query(F.data.startswith("catdel:"))
async def cb_catdel(cb: CallbackQuery):
    kind = cb.data.split(":")[1]
    await show(
        cb,
        "Какую категорию удалить? (история записей сохранится)",
        cats_kb(kind, "catrm"),
    )


@router.callback_query(F.data.startswith("catrm:"))
async def cb_catrm(cb: CallbackQuery):
    cat_id = int(cb.data.split(":")[1])
    rows = run("SELECT name FROM categories WHERE id=?", (cat_id,))
    run("UPDATE categories SET active = 0 WHERE id=?", (cat_id,))
    name = rows[0]["name"] if rows else ""
    await show(cb, f"🗑 Категория «{name}» удалена.", main_kb())


# --- Всё остальное ---
@router.message()
async def fallback(m: Message):
    await m.answer("Нажмите /start, чтобы открыть меню.", reply_markup=main_kb())


# ---------- Напоминание ----------
async def reminder_loop(bot: Bot):
    if REMINDER_HOUR < 0 or not OWNER_ID:
        return
    last_day = None
    while True:
        n = now()
        if n.hour == REMINDER_HOUR and last_day != n.date():
            last_day = n.date()
            try:
                await bot.send_message(
                    OWNER_ID,
                    "🔔 Не забудьте внести сегодняшние расходы и доходы.",
                    reply_markup=main_kb(),
                )
            except Exception:
                logging.exception("Не удалось отправить напоминание")
        await asyncio.sleep(60)


async def main():
    if not TOKEN:
        raise SystemExit("Не задан BOT_TOKEN (см. файл .env)")
    if not OWNER_ID:
        logging.warning(
            "OWNER_ID не задан: бот отвечает только на /id. "
            "Отправьте боту /id и впишите число в .env"
        )
    init_db()
    bot = Bot(token=TOKEN)
    dp = Dispatcher()
    dp.include_router(public)
    dp.include_router(router)
    reminder = asyncio.create_task(reminder_loop(bot))
    try:
        await dp.start_polling(bot)
    finally:
        reminder.cancel()


if __name__ == "__main__":
    asyncio.run(main())
