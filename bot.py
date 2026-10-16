"""
Telegram-бот учёта расходов и доходов.
Меню: Расходы, Доходы, Статистика, История, Настройки.
Хранение: SQLite (файл budget.db). Напоминание каждый день (по умолчанию 21:00).

Запуск:
    pip install -r requirements.txt
    BOT_TOKEN=123:ABC python bot.py
"""
import io
import logging
import os
import sqlite3
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.styles import Font
from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup as Markup
from telegram import ReplyKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    TypeHandler,
    filters,
)

# ================= НАСТРОЙКИ =================
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬ_ТОКЕН_ОТ_BOTFATHER")
TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Moscow"))  # твой часовой пояс
DB_PATH = os.getenv("DB_PATH", "budget.db")
# Если хочешь, чтобы бот работал только для тебя, укажи свой Telegram ID
# (узнать можно у бота @userinfobot): ALLOWED_USERS=123456789
ALLOWED = {int(x) for x in os.getenv("ALLOWED_USERS", "").replace(" ", "").split(",") if x}
# =============================================

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("budget-bot")

DEFAULT_EXPENSE = [
    "🍽 Еда", "🍕 Доп. еда", "🚕 Транспорт", "👕 Одежда", "💳 Оплата кредита",
    "⚠️ Непредвиденные расходы", "🏠 Жильё и коммуналка", "📱 Связь и интернет",
    "💊 Здоровье", "🎬 Развлечения",
]
DEFAULT_INCOME = ["💼 Зарплата", "💵 Подработка", "🎁 Подарки", "➕ Другое"]

MAIN_KB = ReplyKeyboardMarkup(
    [["➖ Расходы", "➕ Доходы"], ["📊 Статистика", "🕘 История"], ["⚙️ Настройки"]],
    resize_keyboard=True,
)
MENU_BUTTONS = {"➖ Расходы", "➕ Доходы", "📊 Статистика", "🕘 История", "⚙️ Настройки"}
CANCEL_KB = Markup([[Btn("❌ Отмена", callback_data="cancel")]])


# ================= БАЗА ДАННЫХ =================
def run(sql, args=(), fetch=None):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(sql, args)
        conn.commit()
        if fetch == "one":
            return cur.fetchone()
        if fetch == "all":
            return cur.fetchall()
        return cur.lastrowid
    finally:
        conn.close()


def init_db():
    run("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        currency TEXT DEFAULT '₽',
        remind_on INTEGER DEFAULT 1,
        remind_time TEXT DEFAULT '21:00',
        budget REAL DEFAULT 0)""")
    run("""CREATE TABLE IF NOT EXISTS categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, type TEXT, name TEXT, archived INTEGER DEFAULT 0)""")
    run("""CREATE TABLE IF NOT EXISTS entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, type TEXT, category_id INTEGER,
        amount REAL, comment TEXT, date TEXT, created_at TEXT)""")
    run("CREATE INDEX IF NOT EXISTS idx_entries ON entries(user_id, date)")


def ensure_user(uid):
    run("INSERT OR IGNORE INTO users(user_id) VALUES (?)", (uid,))
    if not run("SELECT 1 FROM categories WHERE user_id=? LIMIT 1", (uid,), "one"):
        for n in DEFAULT_EXPENSE:
            run("INSERT INTO categories(user_id,type,name) VALUES (?,?,?)", (uid, "e", n))
        for n in DEFAULT_INCOME:
            run("INSERT INTO categories(user_id,type,name) VALUES (?,?,?)", (uid, "i", n))


def get_user(uid):
    return run("SELECT * FROM users WHERE user_id=?", (uid,), "one")


# ================= УТИЛИТЫ =================
def today():
    return datetime.now(TZ).date()


def fmt(n):
    n = round(float(n), 2)
    if n == int(n):
        s = f"{int(n):,}"
    else:
        s = f"{n:,.2f}".rstrip("0").rstrip(".")
    return s.replace(",", " ")


def parse_amount(text, allow_zero=False):
    t = text.replace(" ", "").replace("\u00a0", "").replace(",", ".")
    try:
        v = float(t)
    except ValueError:
        return None
    if v < 0 or (v == 0 and not allow_zero) or v >= 1e12:
        return None
    return v


def parse_date(text):
    t = text.strip().replace("/", ".").replace("-", ".")
    for f in ("%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(t, f).date()
        except ValueError:
            pass
    try:
        d = datetime.strptime(f"{t}.{today().year}", "%d.%m.%Y").date()
        if d > today():
            d = d.replace(year=d.year - 1)
        return d
    except ValueError:
        return None


def reset(context):
    context.user_data.pop("mode", None)
    context.user_data.pop("draft", None)
    context.user_data.pop("newcat_type", None)


async def show(context, chat_id, text, kb=None, q=None):
    """Редактирует сообщение (если нажата кнопка) или шлёт новое."""
    if q:
        try:
            await q.edit_message_text(text, reply_markup=kb)
        except BadRequest:
            pass
        return
    await context.bot.send_message(chat_id, text, reply_markup=kb)


# ================= ДОСТУП =================
async def gatekeeper(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if ALLOWED and user and user.id not in ALLOWED:
        raise ApplicationHandlerStop


# ================= НАПОМИНАНИЯ =================
async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    uid = context.job.chat_id
    u = get_user(uid)
    if not u or not u["remind_on"]:
        return
    n = run("SELECT COUNT(*) AS n FROM entries WHERE user_id=? AND date=?",
            (uid, today().isoformat()), "one")["n"]
    if n == 0:
        text = "🔔 Напоминание: внеси сегодняшние расходы и доходы."
    else:
        text = f"🔔 Сегодня уже записей: {n}. Всё внесено?"
    kb = Markup([
        [Btn("➖ Внести расход", callback_data="menu:e"), Btn("➕ Внести доход", callback_data="menu:i")],
        [Btn("✅ Всё внёс / трат не было", callback_data="rem:ok")],
    ])
    await context.bot.send_message(uid, text, reply_markup=kb)


def schedule_reminder(job_queue, uid):
    for j in job_queue.get_jobs_by_name(f"rem_{uid}"):
        j.schedule_removal()
    u = get_user(uid)
    if not u or not u["remind_on"]:
        return
    h, m = map(int, u["remind_time"].split(":"))
    job_queue.run_daily(reminder_job, time=time(h, m, tzinfo=TZ), chat_id=uid, name=f"rem_{uid}")


async def post_init(app: Application):
    for r in run("SELECT user_id FROM users", fetch="all"):
        schedule_reminder(app.job_queue, r["user_id"])


# ================= /start =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ensure_user(uid)
    reset(context)
    schedule_reminder(context.application.job_queue, uid)
    await update.message.reply_text(
        "Привет! Я веду учёт твоих расходов и доходов.\n\n"
        "➖ Расходы / ➕ Доходы: выбери категорию, сумму, комментарий и дату\n"
        "📊 Статистика: итоги за день, неделю, месяц, год\n"
        "🕘 История: последние записи, можно удалить\n"
        "⚙️ Настройки: свои категории, напоминание, лимит на месяц, экспорт в Excel\n\n"
        "Каждый день в 21:00 пришлю напоминание (время можно поменять).",
        reply_markup=MAIN_KB,
    )


# ================= ВНЕСЕНИЕ ЗАПИСИ =================
def cat_keyboard(uid, t):
    rows = run("SELECT id,name FROM categories WHERE user_id=? AND type=? AND archived=0 ORDER BY id",
               (uid, t), "all")
    kb, row = [], []
    for r in rows:
        row.append(Btn(r["name"], callback_data=f"cat:{t}:{r['id']}"))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    kb.append([Btn("❌ Отмена", callback_data="cancel")])
    return Markup(kb), len(rows)


async def send_categories(context, chat_id, uid, t):
    reset(context)
    kb, n = cat_keyboard(uid, t)
    if n == 0:
        await context.bot.send_message(chat_id, "Категорий нет. Добавь их в ⚙️ Настройки.")
        return
    title = "расходов" if t == "e" else "доходов"
    await context.bot.send_message(chat_id, f"Выбери категорию {title}:", reply_markup=kb)


def date_keyboard():
    t = today()
    y, b = t - timedelta(days=1), t - timedelta(days=2)
    return Markup([
        [Btn(f"Сегодня ({t:%d.%m})", callback_data="d:0")],
        [Btn(f"Вчера ({y:%d.%m})", callback_data="d:1"), Btn(f"Позавчера ({b:%d.%m})", callback_data="d:2")],
        [Btn("📅 Другая дата", callback_data="d:x")],
        [Btn("❌ Отмена", callback_data="cancel")],
    ])


async def ask_date(context, chat_id):
    context.user_data["mode"] = None
    await context.bot.send_message(chat_id, "За какую дату внести?", reply_markup=date_keyboard())


async def finish_entry(context, uid, chat_id, d):
    draft = context.user_data.get("draft")
    if not draft or "amount" not in draft:
        reset(context)
        await context.bot.send_message(chat_id, "Сессия устарела, начни заново.", reply_markup=MAIN_KB)
        return
    run("""INSERT INTO entries(user_id,type,category_id,amount,comment,date,created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (uid, draft["type"], draft["cat"], draft["amount"], draft.get("comment", ""),
         d.isoformat(), datetime.now(TZ).isoformat()))
    cat = run("SELECT name FROM categories WHERE id=?", (draft["cat"],), "one")["name"]
    cur = get_user(uid)["currency"]
    sign = "➖ Расход" if draft["type"] == "e" else "➕ Доход"
    lines = ["✅ Записано", f"{sign}: {fmt(draft['amount'])} {cur}", f"Категория: {cat}", f"Дата: {d:%d.%m.%Y}"]
    if draft.get("comment"):
        lines.append(f"Комментарий: {draft['comment']}")
    if draft["type"] == "e":
        lines += budget_lines(uid, d)
    reset(context)
    await context.bot.send_message(chat_id, "\n".join(lines), reply_markup=MAIN_KB)


def month_expense(uid):
    start = today().replace(day=1).isoformat()
    r = run("SELECT COALESCE(SUM(amount),0) AS s FROM entries WHERE user_id=? AND type='e' AND date>=? AND date<=?",
            (uid, start, today().isoformat()), "one")
    return r["s"]


def budget_lines(uid, d=None):
    u = get_user(uid)
    if not u["budget"] or (d and d.replace(day=1) != today().replace(day=1)):
        return []
    spent, limit = month_expense(uid), u["budget"]
    pct = spent / limit * 100
    line = f"\n💰 Лимит месяца: {fmt(spent)} из {fmt(limit)} {u['currency']} ({pct:.0f}%)"
    if pct >= 100:
        line += "\n🚨 Лимит превышен!"
    elif pct >= 80:
        line += "\n⚠️ Осталось меньше 20% лимита"
    return [line]


# ================= СТАТИСТИКА =================
def period_range(key):
    t = today()
    if key == "today":
        return t, t, "Сегодня"
    if key == "week":
        return t - timedelta(days=t.weekday()), t, "Эта неделя"
    if key == "month":
        return t.replace(day=1), t, "Этот месяц"
    if key == "prev":
        end = t.replace(day=1) - timedelta(days=1)
        return end.replace(day=1), end, "Прошлый месяц"
    return t.replace(month=1, day=1), t, "Этот год"


def stats_kb(active):
    def b(label, key):
        return Btn(("• " if key == active else "") + label, callback_data=f"st:{key}")
    return Markup([
        [b("Сегодня", "today"), b("Неделя", "week"), b("Месяц", "month")],
        [b("Прошлый месяц", "prev"), b("Год", "year")],
    ])


def build_stats(uid, key):
    start, end, title = period_range(key)
    cur = get_user(uid)["currency"]
    rows = run("""SELECT e.type AS type, c.name AS name, SUM(e.amount) AS s
                  FROM entries e JOIN categories c ON c.id = e.category_id
                  WHERE e.user_id=? AND e.date BETWEEN ? AND ?
                  GROUP BY e.type, c.id ORDER BY s DESC""",
               (uid, start.isoformat(), end.isoformat()), "all")
    exp = [(r["name"], r["s"]) for r in rows if r["type"] == "e"]
    inc = [(r["name"], r["s"]) for r in rows if r["type"] == "i"]
    te, ti = sum(s for _, s in exp), sum(s for _, s in inc)
    period = f"{start:%d.%m.%Y}" if start == end else f"{start:%d.%m.%Y} – {end:%d.%m.%Y}"
    lines = [f"📊 {title}", period, ""]
    if not rows:
        lines.append("Записей за этот период нет.")
        return "\n".join(lines)
    lines += [f"➖ Расходы: {fmt(te)} {cur}", f"➕ Доходы: {fmt(ti)} {cur}",
              f"💼 Баланс: {'+' if ti - te >= 0 else '−'}{fmt(abs(ti - te))} {cur}"]
    days = (end - start).days + 1
    if days > 1 and te:
        lines.append(f"📆 В среднем в день: {fmt(te / days)} {cur}")
    if exp:
        lines += ["", "Расходы по категориям:"]
        for name, s in exp:
            share = s / te
            bar = "█" * round(share * 10) + "░" * (10 - round(share * 10))
            lines.append(f"{name}\n{bar} {fmt(s)} ({share * 100:.0f}%)")
    if inc:
        lines += ["", "Доходы по категориям:"]
        for name, s in inc:
            lines.append(f"{name}: {fmt(s)}")
    if key == "month":
        lines += budget_lines(uid)
    return "\n".join(lines)


# ================= ИСТОРИЯ =================
def history_view(uid):
    rows = run("""SELECT e.id, e.type, e.amount, e.comment, e.date, c.name
                  FROM entries e JOIN categories c ON c.id = e.category_id
                  WHERE e.user_id=? ORDER BY e.date DESC, e.id DESC LIMIT 10""", (uid,), "all")
    if not rows:
        return "Записей пока нет.", None
    cur = get_user(uid)["currency"]
    kb = []
    for r in rows:
        d = datetime.strptime(r["date"], "%Y-%m-%d")
        sign = "−" if r["type"] == "e" else "+"
        label = f"🗑 {d:%d.%m} {sign}{fmt(r['amount'])} {cur} {r['name']}"
        kb.append([Btn(label[:60], callback_data=f"delask:{r['id']}")])
    return "🕘 Последние записи.\nНажми на запись, чтобы удалить её:", Markup(kb)


# ================= НАСТРОЙКИ =================
def settings_view(uid):
    u = get_user(uid)
    rem = f"вкл, в {u['remind_time']}" if u["remind_on"] else "выкл"
    budget = f"{fmt(u['budget'])} {u['currency']}" if u["budget"] else "не задан"
    text = (f"⚙️ Настройки\n\n⏰ Напоминание: {rem}\n💰 Лимит расходов на месяц: {budget}\n"
            f"💱 Валюта: {u['currency']}")
    kb = Markup([
        [Btn("➕ Категория расходов", callback_data="set:addcat:e"),
         Btn("➕ Категория доходов", callback_data="set:addcat:i")],
        [Btn("🗑 Удалить категорию", callback_data="set:delcat")],
        [Btn("🔕 Выключить напоминание" if u["remind_on"] else "🔔 Включить напоминание",
             callback_data="set:rem")],
        [Btn("🕘 Время напоминания", callback_data="set:time")],
        [Btn("💰 Лимит на месяц", callback_data="set:budget"), Btn("💱 Валюта", callback_data="set:cur")],
        [Btn("📥 Экспорт в Excel", callback_data="set:export")],
    ])
    return text, kb


async def send_export(context, uid, chat_id):
    rows = run("""SELECT e.date, e.type, c.name, e.amount, e.comment
                  FROM entries e JOIN categories c ON c.id = e.category_id
                  WHERE e.user_id=? ORDER BY e.date, e.id""", (uid,), "all")
    wb = Workbook()
    ws = wb.active
    ws.title = "Операции"
    ws.append(["Дата", "Тип", "Категория", "Сумма", "Комментарий"])
    for c in ws[1]:
        c.font = Font(bold=True)
    for r in rows:
        ws.append([datetime.strptime(r["date"], "%Y-%m-%d").date(),
                   "Расход" if r["type"] == "e" else "Доход", r["name"], r["amount"], r["comment"]])
    for col, w in zip("ABCDE", (13, 10, 30, 12, 40)):
        ws.column_dimensions[col].width = w
    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    await context.bot.send_document(chat_id, document=bio, filename=f"budget_{today():%Y-%m-%d}.xlsx")


# ================= ОБРАБОТКА КНОПОК =================
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    chat_id = q.message.chat_id
    ensure_user(uid)
    p = q.data.split(":")
    act = p[0]

    if act == "cancel":
        reset(context)
        await show(context, chat_id, "Отменено.", q=q)

    elif act == "menu":
        await send_categories(context, chat_id, uid, p[1])

    elif act == "rem":
        await show(context, chat_id, "Отлично! 👍", q=q)

    elif act == "cat":
        t, cid = p[1], int(p[2])
        row = run("SELECT name FROM categories WHERE id=? AND user_id=?", (cid, uid), "one")
        if not row:
            return await show(context, chat_id, "Категория не найдена.", q=q)
        context.user_data["draft"] = {"type": t, "cat": cid}
        context.user_data["mode"] = "amount"
        await show(context, chat_id, f"{row['name']}\n\nВведи сумму (например 250 или 1200.50):", CANCEL_KB, q=q)

    elif act == "skipc":
        await q.edit_message_reply_markup(None)
        await ask_date(context, chat_id)

    elif act == "d":
        if not context.user_data.get("draft"):
            return await show(context, chat_id, "Сессия устарела, начни заново.", q=q)
        if p[1] == "x":
            context.user_data["mode"] = "date"
            return await show(context, chat_id, "Введи дату: ДД.ММ.ГГГГ или ДД.ММ\nНапример: 03.10", CANCEL_KB, q=q)
        d = today() - timedelta(days=int(p[1]))
        await q.edit_message_reply_markup(None)
        await finish_entry(context, uid, chat_id, d)

    elif act == "st":
        await show(context, chat_id, build_stats(uid, p[1]), stats_kb(p[1]), q=q)

    elif act == "delask":
        eid = int(p[1])
        kb = Markup([[Btn("Да, удалить", callback_data=f"delyes:{eid}"), Btn("Нет", callback_data="hist")]])
        await show(context, chat_id, "Удалить эту запись?", kb, q=q)

    elif act == "delyes":
        run("DELETE FROM entries WHERE id=? AND user_id=?", (int(p[1]), uid))
        text, kb = history_view(uid)
        await show(context, chat_id, "🗑 Удалено.\n\n" + text, kb, q=q)

    elif act == "hist":
        text, kb = history_view(uid)
        await show(context, chat_id, text, kb, q=q)

    elif act == "delcat":
        run("UPDATE categories SET archived=1 WHERE id=? AND user_id=?", (int(p[1]), uid))
        text, kb = settings_view(uid)
        await show(context, chat_id, "🗑 Категория удалена (старые записи сохранены).\n\n" + text, kb, q=q)

    elif act == "cur":
        run("UPDATE users SET currency=? WHERE user_id=?", (p[1], uid))
        text, kb = settings_view(uid)
        await show(context, chat_id, text, kb, q=q)

    elif act == "set":
        await settings_action(update, context, q, uid, chat_id, p)


async def settings_action(update, context, q, uid, chat_id, p):
    sub = p[1]
    if sub == "back":
        text, kb = settings_view(uid)
        await show(context, chat_id, text, kb, q=q)
    elif sub == "addcat":
        context.user_data["mode"] = "newcat"
        context.user_data["newcat_type"] = p[2]
        await show(context, chat_id,
                   "Напиши название новой категории. Можно с эмодзи в начале, например: 🐶 Питомец",
                   CANCEL_KB, q=q)
    elif sub == "delcat":
        rows = run("SELECT id,type,name FROM categories WHERE user_id=? AND archived=0 ORDER BY type DESC, id",
                   (uid,), "all")
        kb = [[Btn(f"{'➖' if r['type'] == 'e' else '➕'} {r['name']}", callback_data=f"delcat:{r['id']}")]
              for r in rows]
        kb.append([Btn("⬅️ Назад", callback_data="set:back")])
        await show(context, chat_id, "Какую категорию удалить?", Markup(kb), q=q)
    elif sub == "rem":
        u = get_user(uid)
        run("UPDATE users SET remind_on=? WHERE user_id=?", (0 if u["remind_on"] else 1, uid))
        schedule_reminder(context.application.job_queue, uid)
        text, kb = settings_view(uid)
        await show(context, chat_id, text, kb, q=q)
    elif sub == "time":
        context.user_data["mode"] = "time"
        await show(context, chat_id, "Во сколько напоминать? Формат ЧЧ:ММ, например 21:00", CANCEL_KB, q=q)
    elif sub == "budget":
        context.user_data["mode"] = "budget"
        await show(context, chat_id,
                   "Введи лимит расходов на месяц числом (например 60000).\nЧтобы отключить, отправь 0.",
                   CANCEL_KB, q=q)
    elif sub == "cur":
        kb = Markup([[Btn(c, callback_data=f"cur:{c}") for c in ("₽", "$", "€", "₸", "₴", "Br")]])
        await show(context, chat_id, "Выбери валюту:", kb, q=q)
    elif sub == "export":
        await send_export(context, uid, chat_id)


# ================= ОБРАБОТКА ТЕКСТА =================
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    ensure_user(uid)

    if text in MENU_BUTTONS:
        reset(context)
        if text == "➖ Расходы":
            return await send_categories(context, chat_id, uid, "e")
        if text == "➕ Доходы":
            return await send_categories(context, chat_id, uid, "i")
        if text == "📊 Статистика":
            return await update.message.reply_text(build_stats(uid, "month"), reply_markup=stats_kb("month"))
        if text == "🕘 История":
            t, kb = history_view(uid)
            return await update.message.reply_text(t, reply_markup=kb)
        t, kb = settings_view(uid)
        return await update.message.reply_text(t, reply_markup=kb)

    mode = context.user_data.get("mode")
    draft = context.user_data.get("draft")

    if mode == "amount" and draft:
        v = parse_amount(text)
        if v is None:
            return await update.message.reply_text("Не понял сумму. Введи число, например 250", reply_markup=CANCEL_KB)
        draft["amount"] = v
        context.user_data["mode"] = "comment"
        kb = Markup([[Btn("Пропустить", callback_data="skipc")], [Btn("❌ Отмена", callback_data="cancel")]])
        return await update.message.reply_text("Добавь комментарий (или нажми «Пропустить»):", reply_markup=kb)

    if mode == "comment" and draft:
        draft["comment"] = text[:200]
        return await ask_date(context, chat_id)

    if mode == "date" and draft:
        d = parse_date(text)
        if d is None:
            return await update.message.reply_text("Не понял дату. Формат: ДД.ММ.ГГГГ или ДД.ММ", reply_markup=CANCEL_KB)
        if d > today():
            return await update.message.reply_text("Эта дата в будущем. Введи сегодняшнюю или прошедшую.",
                                                   reply_markup=CANCEL_KB)
        return await finish_entry(context, uid, chat_id, d)

    if mode == "newcat":
        name = text[:40]
        t = context.user_data.get("newcat_type", "e")
        exists = run("SELECT 1 FROM categories WHERE user_id=? AND type=? AND archived=0 AND lower(name)=lower(?)",
                     (uid, t, name), "one")
        if exists:
            return await update.message.reply_text("Такая категория уже есть. Введи другое название.",
                                                   reply_markup=CANCEL_KB)
        run("INSERT INTO categories(user_id,type,name) VALUES (?,?,?)", (uid, t, name))
        reset(context)
        s_text, kb = settings_view(uid)
        await update.message.reply_text(f"✅ Категория «{name}» добавлена.", reply_markup=MAIN_KB)
        return await update.message.reply_text(s_text, reply_markup=kb)

    if mode == "time":
        try:
            h, m = map(int, text.replace(".", ":").split(":"))
            assert 0 <= h < 24 and 0 <= m < 60
        except Exception:
            return await update.message.reply_text("Формат ЧЧ:ММ, например 21:00", reply_markup=CANCEL_KB)
        run("UPDATE users SET remind_time=?, remind_on=1 WHERE user_id=?", (f"{h:02d}:{m:02d}", uid))
        schedule_reminder(context.application.job_queue, uid)
        reset(context)
        s_text, kb = settings_view(uid)
        await update.message.reply_text("✅ Время напоминания обновлено.", reply_markup=MAIN_KB)
        return await update.message.reply_text(s_text, reply_markup=kb)

    if mode == "budget":
        v = parse_amount(text, allow_zero=True)
        if v is None:
            return await update.message.reply_text("Введи число, например 60000 (или 0, чтобы отключить).",
                                                   reply_markup=CANCEL_KB)
        run("UPDATE users SET budget=? WHERE user_id=?", (v, uid))
        reset(context)
        s_text, kb = settings_view(uid)
        await update.message.reply_text("✅ Лимит сохранён.", reply_markup=MAIN_KB)
        return await update.message.reply_text(s_text, reply_markup=kb)

    await update.message.reply_text("Выбери раздел в меню 👇", reply_markup=MAIN_KB)


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    log.error("Ошибка", exc_info=context.error)


# ================= ЗАПУСК =================
def main():
    init_db()
    app = (Application.builder().token(BOT_TOKEN)
           .defaults(Defaults(tzinfo=TZ)).post_init(post_init).build())
    app.add_handler(TypeHandler(Update, gatekeeper), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    log.info("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
