#!/usr/bin/env python3
"""DueBot · Telegram 订阅到期提醒 bot（多用户，SQLite 存储，long polling，无需公网端口）"""
import html
import json
import logging
import os
import re
import sqlite3
from calendar import monthrange
from contextlib import contextmanager
from datetime import date, datetime, time as dtime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup as Kb, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

# ───────────────────────── 配置 ─────────────────────────
TOKEN = os.environ["BOT_TOKEN"]
_ids = [int(x) for x in re.split(r"[,\s]+", os.environ.get("ALLOWED_IDS", "")) if x]  # 兼容旧版配置
ADMIN = int(os.environ.get("ADMIN_ID") or (_ids[0] if _ids else 0))
LEGACY = [i for i in _ids if i != ADMIN]
TZ = ZoneInfo(os.environ.get("TZ", "Asia/Shanghai"))
DB_PATH = os.environ.get("DB_PATH", "/data/subs.db")
PROXY = os.environ.get("PROXY") or None
MAX_SUBS = 100

HTML = ParseMode.HTML
CYCLES = {"m1": ("月付", 1), "m3": ("季付", 3), "m6": ("半年付", 6),
          "y1": ("年付", 12), "once": ("一次性", 0)}
WIN_OPTS = [1, 3, 7, 15, 30]  # 到期前多少天开始每天提醒
PAGE = 8
WEEK = "一二三四五六日"
LINE = "━━━━━━━━━━━━"

STEPS = ["name", "cycle", "due", "amount", "unit"]
LABEL = {"name": "名称", "due": "到期", "amount": "金额", "unit": "单位", "cycle": "周期"}
ASK = {
    "name": "请输入订阅名称",
    "due": "请输入下次到期日\n<i>例：2026-12-31、12-31、12月31日</i>",
    "amount": "请输入金额\n<i>只填数字，例：19.9</i>",
    "unit": "请输入金额单位\n<i>例：CNY、USD、¥、$</i>",
    "cycle": "请选择续费周期\n<i>到期后会按周期自动顺延</i>",
}

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.WARNING)
log = logging.getLogger("duebot")


def B(text, data):
    return InlineKeyboardButton(text, callback_data=data)


# ───────────────────────── 数据库 ─────────────────────────
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with db() as c:
        fresh = not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
        c.execute("""CREATE TABLE IF NOT EXISTS users(
            uid INTEGER PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
            days TEXT NOT NULL DEFAULT '7', time TEXT NOT NULL DEFAULT '09:00',
            quiet INTEGER NOT NULL DEFAULT 1, last TEXT NOT NULL DEFAULT '', panel INTEGER)""")
        c.execute("""CREATE TABLE IF NOT EXISTS subs(
            id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER NOT NULL,
            name TEXT NOT NULL, due TEXT NOT NULL, amount TEXT NOT NULL,
            unit TEXT NOT NULL, cycle TEXT NOT NULL, anchor INTEGER NOT NULL,
            rolled_at TEXT NOT NULL DEFAULT '')""")
        cols = {r["name"] for r in c.execute("PRAGMA table_info(subs)")}
        # 旧版单用户数据：订阅和设置全部归给管理员
        if "uid" not in cols:
            c.execute(f"ALTER TABLE subs ADD COLUMN uid INTEGER NOT NULL DEFAULT {ADMIN}")
        if "rolled_at" not in cols:
            c.execute("ALTER TABLE subs ADD COLUMN rolled_at TEXT NOT NULL DEFAULT ''")
        c.execute("INSERT OR IGNORE INTO users(uid) VALUES(?)", (ADMIN,))
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'").fetchone():
            old = {r["k"]: r["v"] for r in c.execute("SELECT k, v FROM settings")}
            for k in ("days", "time", "quiet", "last"):
                if k in old:
                    c.execute(f"UPDATE users SET {k}=? WHERE uid=?", (old[k], ADMIN))
            c.execute("DROP TABLE settings")
        if fresh:
            for i in LEGACY:
                c.execute("INSERT OR IGNORE INTO users(uid) VALUES(?)", (i,))
        c.execute("CREATE INDEX IF NOT EXISTS subs_uid ON subs(uid, due)")


# 用户
def user(uid):
    with db() as c:
        r = c.execute("SELECT * FROM users WHERE uid=?", (uid,)).fetchone()
    return dict(r) if r else None


def users():
    with db() as c:
        return [dict(r) for r in c.execute(
            "SELECT u.*, (SELECT COUNT(*) FROM subs s WHERE s.uid=u.uid) n "
            "FROM users u ORDER BY (u.uid=?) DESC, u.uid", (ADMIN,))]


def upd_user(uid, **kw):
    kw = {k: v for k, v in kw.items() if k in {"name", "days", "time", "quiet", "last", "panel"}}
    with db() as c:
        c.execute(f"UPDATE users SET {', '.join(f'{k}=?' for k in kw)} WHERE uid=?", (*kw.values(), uid))


def add_user(uid):
    with db() as c:
        c.execute("INSERT OR IGNORE INTO users(uid) VALUES(?)", (uid,))


def del_user(uid):
    with db() as c:
        c.execute("DELETE FROM subs WHERE uid=?", (uid,))
        c.execute("DELETE FROM users WHERE uid=?", (uid,))


def window(u):
    """到期前几天开始每天提醒；None 表示关闭。兼容旧版的「7,3,1」，取最大值"""
    vals = [int(x) for x in str(u["days"]).split(",") if x.strip().isdigit()]
    return max(vals) if vals else None


# 订阅（所有查询都带 uid，用户之间互相看不到）
def subs(uid):
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM subs WHERE uid=? ORDER BY due, name", (uid,))]


def sub(uid, i):
    with db() as c:
        r = c.execute("SELECT * FROM subs WHERE id=? AND uid=?", (i, uid)).fetchone()
    return dict(r) if r else None


def add_sub(uid, d):
    with db() as c:
        cur = c.execute(
            "INSERT INTO subs(uid,name,due,amount,unit,cycle,anchor) VALUES(?,?,?,?,?,?,?)",
            (uid, d["name"], d["due"], d["amount"], d["unit"], d["cycle"],
             d.get("anchor") or date.fromisoformat(d["due"]).day))
        return cur.lastrowid


def upd_sub(uid, i, **kw):
    kw = {k: v for k, v in kw.items() if k in {"name", "due", "amount", "unit", "cycle", "anchor", "rolled_at"}}
    with db() as c:
        c.execute(f"UPDATE subs SET {', '.join(f'{k}=?' for k in kw)} WHERE id=? AND uid=?",
                  (*kw.values(), i, uid))


def del_sub(uid, i):
    with db() as c:
        c.execute("DELETE FROM subs WHERE id=? AND uid=?", (i, uid))


def used_units(uid):
    with db() as c:
        return [r["unit"] for r in c.execute(
            "SELECT unit, COUNT(*) n FROM subs WHERE uid=? GROUP BY unit ORDER BY n DESC LIMIT 4", (uid,))]


# ───────────────────────── 日期与金额 ─────────────────────────
def today():
    return datetime.now(TZ).date()


def add_months(d, n, anchor):
    """按「锚定日」加月份，1 月 31 日的月付会依次落在 2/28、3/31，不会越跑越早"""
    y, m = divmod(d.month - 1 + n, 12)
    y, m = y + d.year, m + 1
    return date(y, m, min(anchor, monthrange(y, m)[1]))


def next_due(s):
    return add_months(date.fromisoformat(s["due"]), CYCLES[s["cycle"]][1], s["anchor"])


def roll_date(d, cycle, anchor):
    """把过去的日期按周期推到今天或以后；一次性订阅原样返回"""
    months, t = CYCLES[cycle][1], today()
    while months and d < t:
        d = add_months(d, months, anchor)
    return d


def roll_all(uid):
    """把已过期的周期订阅顺延，并记下顺延日期，当天的通知里会列出来"""
    for s in subs(uid):
        d = date.fromisoformat(s["due"])
        nd = roll_date(d, s["cycle"], s["anchor"])
        if nd != d:
            upd_sub(uid, s["id"], due=nd.isoformat(), rolled_at=today().isoformat())


def left(s):
    return (date.fromisoformat(s["due"]) - today()).days


def parse_date(txt, periodic=False):
    s = re.sub(r"[/.年月]", "-", txt.strip()).rstrip("日号-")
    t = today()
    try:
        d = None
        if m := re.fullmatch(r"(\d{4})-?(\d{1,2})-?(\d{1,2})", s):
            d = date(*map(int, m.groups()))
        elif m := re.fullmatch(r"(\d{1,2})-(\d{1,2})", s):
            d = date(t.year, *map(int, m.groups()))
            if d < t and not periodic:
                d = date(t.year + 1, d.month, d.day)
        return d if d and 2000 <= d.year <= 2100 else None
    except ValueError:
        return None


def parse_amount(txt):
    try:
        d = Decimal(txt.strip().replace(",", ""))
    except InvalidOperation:
        return None
    return d if d.is_finite() and 0 <= d < 10 ** 9 else None


def parse_time(txt):
    m = re.fullmatch(r"(\d{1,2})(?:[:：.](\d{2}))?", txt.strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    return f"{h:02d}:{mi:02d}" if h < 24 and mi < 60 else None


def esc(x):
    return html.escape(str(x))


def num(x):
    return format(Decimal(str(x)).quantize(Decimal("0.01")).normalize(), "f")


def rel(n):
    if n < 0:
        return f"已过期 {-n} 天"
    return {0: "今天", 1: "明天", 2: "后天"}.get(n, f"{n} 天后")


def dot(n):
    if n < 0:
        return "⚫"
    return "🔴" if n <= 1 else "🟠" if n <= 3 else "🟡" if n <= 7 else "🟢"


def md(d):
    d = date.fromisoformat(d) if isinstance(d, str) else d
    return d.strftime("%m-%d") if d.year == today().year else d.isoformat()


def sum_by_unit(pairs):
    acc = {}
    for a, u in pairs:
        acc[u] = acc.get(u, Decimal(0)) + a
    return " · ".join(f"{num(v)} {esc(u)}" for u, v in acc.items())


def monthly(ss):
    return sum_by_unit((Decimal(s["amount"]) / CYCLES[s["cycle"]][1], s["unit"])
                       for s in ss if CYCLES[s["cycle"]][1])


def win_label(w):
    if w is None:
        return "已关闭"
    return "仅到期当天" if w == 0 else f"到期前 {w} 天起每天"


def uname(u):
    return esc(u["name"]) if u["name"] else f"ID {u['uid']}"


# ───────────────────────── 视图（返回 文本, 键盘） ─────────────────────────
def v_home(uid, note=None):
    roll_all(uid)
    ss, u = subs(uid), user(uid)
    lines = [note, ""] if note else []
    lines += ["<b>📦 DueBot</b>", LINE]
    if ss:
        lines.append(f"订阅　<b>{len(ss)}</b> 个")
        up = next((s for s in ss if left(s) >= 0), None)
        if up:
            lines.append(f"最近　{esc(up['name'])} · {rel(left(up))}")
        if m := monthly(ss):
            lines.append(f"月均　{m}")
    else:
        lines.append("还没有订阅，点「➕ 添加订阅」开始吧")
    w = window(u)
    lines.append(f"提醒　{win_label(w)}{' · ' + u['time'] if w is not None else ''}")
    rows = [
        [B("📋 订阅列表", "L:0"), B("➕ 添加订阅", "add")],
        [B("⏰ 提醒设置", "set"), B("🔔 预览提醒", "prev")],
        [B("📤 导出备份", "exp"), B("📥 导入备份", "imp")],
    ]
    if uid == ADMIN:
        rows.append([B("👥 用户管理", "U")])
    return "\n".join(lines), Kb(rows)


def v_list(uid, page=0):
    roll_all(uid)
    ss = sorted(subs(uid), key=lambda s: left(s) < 0)  # 过期的一次性订阅排最后
    if not ss:
        return "<b>📋 订阅列表</b>\n\n空空如也", Kb([[B("➕ 添加订阅", "add")], [B("« 返回", "home")]])
    pages = (len(ss) - 1) // PAGE + 1
    page = max(0, min(page, pages - 1))
    rows = []
    for s in ss[page * PAGE:(page + 1) * PAGE]:
        n = left(s)
        rows.append([B(f"{dot(n)} {s['name']} · {rel(n)}", f"v:{s['id']}")])
    if pages > 1:
        rows.append([B("‹", f"L:{page - 1}"), B(f"{page + 1}/{pages}", "noop"), B("›", f"L:{page + 1}")])
    rows.append([B("« 返回", "home"), B("➕ 添加", "add")])
    return f"<b>📋 订阅列表</b>　共 {len(ss)} 个\n<i>点击查看与修改</i>", Kb(rows)


def v_detail(uid, i, note=None):
    roll_all(uid)
    s = sub(uid, i)
    if not s:
        return v_list(uid)
    n = left(s)
    lines = [note, ""] if note else []
    lines += [f"<b>{esc(s['name'])}</b>", LINE,
              f"到期　{s['due']}（{rel(n)}）",
              f"金额　{num(s['amount'])} {esc(s['unit'])}",
              f"周期　{CYCLES[s['cycle']][0]}"]
    row3 = [B("🔁 周期", f"e:cycle:{i}")]
    if s["cycle"] != "once":
        row3.append(B("⏭ 续费一期", f"r:{i}"))
    return "\n".join(lines), Kb([
        [B("✏️ 名称", f"e:name:{i}"), B("📅 到期", f"e:due:{i}")],
        [B("💰 金额", f"e:amount:{i}"), B("🏷 单位", f"e:unit:{i}")],
        row3,
        [B("🗑 删除", f"d:{i}"), B("« 返回列表", "L:0")],
    ])


def cycle_rows(prefix):
    items = [B(name, f"{prefix}{code}") for code, (name, _) in CYCLES.items()]
    return [items[:3], items[3:]]


def unit_rows(uid, prefix):
    btns = [B(u, f"{prefix}{u}") for u in used_units(uid) if len(f"{prefix}{u}".encode()) <= 64]
    return [btns] if btns else []


def show_val(k, v):
    if k == "amount":
        return num(v)
    if k == "cycle":
        return CYCLES[v][0]
    return esc(v)


def v_add(uid, dr, step, err=None):
    lines = [f"<b>➕ 添加订阅</b>　<code>{STEPS.index(step) + 1}/5</code>", LINE]
    filled = [f"{LABEL[k]}　{show_val(k, dr[k])}" for k in STEPS if k in dr]
    if filled:
        lines += filled + [""]
    if err:
        lines.append(f"⚠️ {err}")
    lines.append(ASK[step])
    rows = []
    if step == "unit":
        rows += unit_rows(uid, "au:")
    if step == "cycle":
        rows += cycle_rows("ac:")
    rows.append([B("❌ 取消", "x")])
    return "\n".join(lines), Kb(rows)


def v_confirm(dr):
    lines = ["<b>➕ 确认添加</b>", LINE] + [f"{LABEL[k]}　{show_val(k, dr[k])}" for k in STEPS]
    return "\n".join(lines), Kb([[B("✅ 保存", "asave"), B("❌ 取消", "x")]])


def v_edit(uid, i, field, err=None):
    s = sub(uid, i)
    cur = num(s["amount"]) if field == "amount" else s[field]
    lines = [f"<b>✏️ 修改{LABEL[field]}</b>　{esc(s['name'])}", LINE, f"当前　<code>{esc(cur)}</code>", ""]
    if err:
        lines.append(f"⚠️ {err}")
    lines.append(ASK[field])
    rows = unit_rows(uid, f"eu:{i}:") if field == "unit" else []
    rows.append([B("❌ 取消", f"v:{i}")])
    return "\n".join(lines), Kb(rows)


def v_edit_cycle(uid, i):
    s = sub(uid, i)
    return (f"<b>🔁 修改周期</b>　{esc(s['name'])}\n{LINE}\n当前　{CYCLES[s['cycle']][0]}",
            Kb(cycle_rows(f"ec:{i}:") + [[B("❌ 取消", f"v:{i}")]]))


def v_settings(uid, note=None):
    u = user(uid)
    w = window(u)

    def opt(d):
        return B(("✅ " if w == d else "▫️ ") + f"{d}天", f"w:{d}")

    quiet = int(u["quiet"])
    lines = [note, ""] if note else []
    lines += ["<b>⏰ 提醒设置</b>", LINE,
              f"提醒　{win_label(w)}",
              f"时间　每天 {u['time']}",
              f"空闲　{'没有到期时不推送' if quiet else '没有到期也推送一条'}",
              "", "<i>选择到期前几天开始，每天推送直到到期当天</i>"]
    return "\n".join(lines), Kb([
        [opt(d) for d in WIN_OPTS[:3]],
        [opt(d) for d in WIN_OPTS[3:]] + [B(("✅ " if w is None else "▫️ ") + "关闭", "w:off")],
        [B(f"🕘 推送时间 {u['time']}", "st")],
        [B(f"📭 没有到期也推送：{'关' if quiet else '开'}", "sq")],
        [B("« 返回", "home")],
    ])


def v_time(uid, err=None):
    lines = ["<b>🕘 修改推送时间</b>", LINE, f"当前　{user(uid)['time']}", ""]
    if err:
        lines.append(f"⚠️ {err}")
    lines.append("请输入新时间\n<i>例：09:00、21:30、9</i>")
    return "\n".join(lines), Kb([[B("❌ 取消", "set")]])


# 管理员：用户管理（只显示订阅数量，看不到别人的订阅内容）
def v_users(note=None):
    us = users()
    lines = [note, ""] if note else []
    lines += [f"<b>👥 用户管理</b>　共 {len(us)} 人", LINE, "<i>点击查看或移除用户</i>"]
    rows = [[B(f"{'👑 ' if u['uid'] == ADMIN else ''}{u['name'] or u['uid']} · {u['n']} 个订阅",
               f"u:{u['uid']}")] for u in us]
    rows.append([B("➕ 添加用户", "uadd"), B("« 返回", "home")])
    return "\n".join(lines), Kb(rows)


def v_user(t):
    u = user(t)
    if not u:
        return v_users()
    w = window(u)
    lines = [f"<b>{uname(u)}</b>{'　👑 管理员' if t == ADMIN else ''}", LINE,
             f"ID　<code>{t}</code>",
             f"订阅　{len(subs(t))} 个",
             f"提醒　{win_label(w)}{' · ' + u['time'] if w is not None else ''}"]
    rows = [] if t == ADMIN else [[B("🗑 移除用户", f"ux:{t}")]]
    rows.append([B("« 返回", "U")])
    return "\n".join(lines), Kb(rows)


def parse_backup(raw):
    """校验备份文件，返回 (有效订阅列表, 设置, 跳过数)；格式不对返回 None"""
    try:
        data = json.loads(raw)
        items = data["subs"]
        assert isinstance(items, list)
    except Exception:
        return None
    good, bad = [], 0
    for it in items:
        try:
            name = " ".join(str(it["name"]).split())
            unit = " ".join(str(it["unit"]).split())
            cycle = it["cycle"]
            d = date.fromisoformat(it["due"])
            amount = parse_amount(str(it["amount"]))
            anchor = int(it.get("anchor") or d.day)
            assert 0 < len(name) <= 40 and 0 < len(unit) <= 10 and cycle in CYCLES
            assert 2000 <= d.year <= 2100 and amount is not None and 1 <= anchor <= 31
        except Exception:
            bad += 1
            continue
        good.append({"name": name, "unit": unit, "cycle": cycle, "amount": str(amount), "anchor": anchor,
                     "due": roll_date(d, cycle, anchor).isoformat()})
    st, raw_st = {}, data.get("settings") or {}
    if isinstance(raw_st, dict):
        if isinstance(raw_st.get("days"), str):
            vals = [int(x) for x in raw_st["days"].split(",") if x.strip().isdigit()]
            if not raw_st["days"].strip():
                st["days"] = ""
            elif vals and max(vals) <= 30:
                st["days"] = str(max(vals))
        if t := parse_time(str(raw_st.get("time", ""))):
            st["time"] = t
        if str(raw_st.get("quiet")) in ("0", "1"):
            st["quiet"] = int(raw_st["quiet"])
    return good, st, bad


def sub_key(s):
    return (s["name"], num(s["amount"]), s["unit"], s["cycle"])


def v_import(err=None):
    lines = ["<b>📥 导入备份</b>", LINE]
    if err:
        lines.append(f"⚠️ {err}")
    lines.append("请发送之前导出的 JSON 文件")
    return "\n".join(lines), Kb([[B("❌ 取消", "home")]])


def v_import_preview(uid, imp):
    have = len(subs(uid))
    lines = ["<b>📥 导入备份</b>", LINE, f"备份里有　<b>{len(imp['subs'])}</b> 个订阅"]
    if imp["bad"]:
        lines.append(f"无效跳过　{imp['bad']} 个")
    lines += [f"当前已有　{have} 个", "",
              "<b>替换</b>：删掉现有订阅，换成备份里的，提醒设置一起恢复",
              "<b>追加</b>：保留现有订阅，把备份里的加进来，相同的跳过"]
    return "\n".join(lines), Kb([[B("替换全部", "imr"), B("追加", "ima")], [B("❌ 取消", "home")]])


def v_adduser(err=None):
    lines = ["<b>➕ 添加用户</b>", LINE]
    if err:
        lines.append(f"⚠️ {err}")
    lines.append("请输入对方的 Telegram 数字 ID\n<i>让对方找 @userinfobot 发条消息就能拿到</i>")
    return "\n".join(lines), Kb([[B("❌ 取消", "U")]])


# ───────────────────────── 每日通知 ─────────────────────────
def notice(uid, force=False):
    roll_all(uid)
    t, u, ss = today(), user(uid), subs(uid)
    w = window(u)
    hits = sorted(((left(s), s) for s in ss if w is not None and 0 <= left(s) <= w), key=lambda x: x[0])
    rolled = [(s["name"], s["due"]) for s in ss if s["rolled_at"] == t.isoformat()]
    if not hits and not rolled and not force and int(u["quiet"]):
        return None
    blocks = [f"<b>⏰ 订阅提醒</b>　{t.month}月{t.day}日 · 周{WEEK[t.weekday()]}\n{LINE}"]
    for n, s in hits:
        blocks.append(f"{dot(n)} <b>{esc(s['name'])}</b> · {rel(n)}\n"
                      f"　 └ {num(s['amount'])} {esc(s['unit'])} · "
                      f"{CYCLES[s['cycle']][0]} · {md(s['due'])}")
    if hits:
        total = sum_by_unit((Decimal(s["amount"]), s["unit"]) for _, s in hits)
        blocks.append(f"<i>合计　{total}</i>")
    if rolled:
        blocks.append("<b>↻ 已自动顺延</b>\n" + "\n".join(f"　{esc(nm)} → {md(d)}" for nm, d in rolled))
    if not hits and not rolled:
        blocks.append("✨ 今天没有需要续费的订阅")
    return "\n\n".join(blocks)


async def send_long(bot, uid, text, kb=None):
    chunks, cur = [], ""
    for blk in text.split("\n\n"):
        if cur and len(cur) + len(blk) + 2 > 4000:
            chunks.append(cur)
            cur = blk
        else:
            cur = f"{cur}\n\n{blk}" if cur else blk
    chunks.append(cur)
    for n, c in enumerate(chunks):
        await bot.send_message(uid, c, parse_mode=HTML, reply_markup=kb if n == len(chunks) - 1 else None)


async def daily(ctx: ContextTypes.DEFAULT_TYPE):
    uid = ctx.job.data
    if not user(uid):
        return
    upd_user(uid, last=today().isoformat())
    text = notice(uid)
    if not text:
        return
    try:
        await send_long(ctx.bot, uid, text, Kb([[B("📋 打开面板", "menu")]]))
    except TelegramError as e:
        log.warning("推送给 %s 失败：%s", uid, e)


def unschedule(app, uid):
    for j in app.job_queue.get_jobs_by_name(f"daily:{uid}"):
        j.schedule_removal()


def catch_up(app, u):
    """今天的推送时间已过、但还没推过（重启或改了时间）：5 秒后补发"""
    h, m = map(int, u["time"].split(":"))
    now = datetime.now(TZ)
    if now.time() >= dtime(h, m) and u["last"] != now.date().isoformat():
        app.job_queue.run_once(daily, 5, data=u["uid"])


def schedule(app, uid):
    """每个用户按自己设置的时间单独推送"""
    unschedule(app, uid)
    if u := user(uid):
        h, m = map(int, u["time"].split(":"))
        app.job_queue.run_daily(daily, time=dtime(h, m, tzinfo=TZ), name=f"daily:{uid}", data=uid)


# ───────────────────────── 渲染与交互 ─────────────────────────
async def render(ctx, uid, view, mid=None):
    """优先原地编辑面板消息，失败再发新消息"""
    text, kb = view
    mid = mid or user(uid)["panel"]
    if mid:
        try:
            await ctx.bot.edit_message_text(text, chat_id=uid, message_id=mid,
                                            parse_mode=HTML, reply_markup=kb)
            upd_user(uid, panel=mid)
            return
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
    m = await ctx.bot.send_message(uid, text, parse_mode=HTML, reply_markup=kb)
    upd_user(uid, panel=m.message_id)


async def close_panel(ctx, uid):
    """删掉旧面板；超过 48 小时 Telegram 不让删，就把它改成一行灰字并去掉按钮"""
    mid = user(uid)["panel"]
    if not mid:
        return
    upd_user(uid, panel=None)
    try:
        await ctx.bot.delete_message(uid, mid)
    except TelegramError:
        try:
            await ctx.bot.edit_message_text("<i>面板已关闭</i>", chat_id=uid, message_id=mid, parse_mode=HTML)
        except TelegramError:
            pass


async def new_panel(update, ctx, uid, view):
    if update.message:  # 顺手删掉用户发的 /menu，聊天里始终只留一个面板
        try:
            await update.message.delete()
        except TelegramError:
            pass
    await close_panel(ctx, uid)
    await render(ctx, uid, view)


def gate(update):
    """只放行用户列表里的人，其他人发什么都不回应；顺便记下昵称给管理员看"""
    tu = update.effective_user
    if not tu or update.effective_chat.type != "private":
        return None
    u = user(tu.id)
    if not u:
        return None
    name = tu.full_name[:40]
    if u["name"] != name:
        upd_user(tu.id, name=name)
    return tu.id


def reset(ud):
    ud.pop("await", None)
    ud.pop("draft", None)


async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not (uid := gate(update)):
        return
    reset(ctx.user_data)
    await new_panel(update, ctx, uid, v_home(uid))


async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not (uid := gate(update)):
        return
    reset(ctx.user_data)
    if len(subs(uid)) >= MAX_SUBS:
        return await new_panel(update, ctx, uid, v_home(uid, f"⚠️ 最多只能添加 {MAX_SUBS} 个订阅"))
    ctx.user_data.update(draft={}, **{"await": ("add", "name")})
    await new_panel(update, ctx, uid, v_add(uid, {}, "name"))


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not (uid := gate(update)):
        return
    reset(ctx.user_data)
    await new_panel(update, ctx, uid, v_home(uid))


async def on_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not (uid := gate(update)):
        return
    d, ud, mid = q.data, ctx.user_data, q.message.message_id

    async def show(view):
        await render(ctx, uid, view, mid)

    if d == "noop":
        return
    if d == "menu":  # 从通知消息点进来：另起一条面板，不覆盖通知
        reset(ud)
        return await new_panel(update, ctx, uid, v_home(uid))
    if d in ("home", "x"):
        reset(ud)
        return await show(v_home(uid))
    if d.startswith("L:"):
        reset(ud)
        return await show(v_list(uid, int(d[2:])))
    if d.startswith("v:"):
        reset(ud)
        return await show(v_detail(uid, int(d[2:])))

    # 添加流程
    if d == "add":
        reset(ud)
        if len(subs(uid)) >= MAX_SUBS:
            return await show(v_home(uid, f"⚠️ 最多只能添加 {MAX_SUBS} 个订阅"))
        ud["draft"], ud["await"] = {}, ("add", "name")
        return await show(v_add(uid, {}, "name"))
    if d.startswith("ac:") and ud.get("await") == ("add", "cycle") and d[3:] in CYCLES:
        ud["draft"]["cycle"], ud["await"] = d[3:], ("add", "due")
        return await show(v_add(uid, ud["draft"], "due"))
    if d.startswith("au:") and ud.get("await") == ("add", "unit") and 0 < len(d[3:]) <= 10:
        ud["draft"]["unit"] = d[3:]
        ud.pop("await", None)
        return await show(v_confirm(ud["draft"]))
    if d == "asave" and all(k in ud.get("draft", {}) for k in STEPS):
        i = add_sub(uid, ud["draft"])
        reset(ud)
        return await show(v_detail(uid, i, "✅ 已添加"))

    # 修改 / 续费 / 删除
    if d.startswith("e:"):
        _, field, i = d.split(":")
        i = int(i)
        if not sub(uid, i):
            return await show(v_list(uid))
        if field == "cycle":
            return await show(v_edit_cycle(uid, i))
        ud["await"] = ("edit", field, i)
        return await show(v_edit(uid, i, field))
    if d.startswith("ec:"):
        _, i, code = d.split(":")
        if code in CYCLES:
            upd_sub(uid, int(i), cycle=code)
            return await show(v_detail(uid, int(i), "✅ 周期已更新"))
    if d.startswith("eu:"):
        _, i, unit = d.split(":", 2)
        if 0 < len(unit) <= 10:
            upd_sub(uid, int(i), unit=unit)
            ud.pop("await", None)
            return await show(v_detail(uid, int(i), "✅ 单位已更新"))
    if d.startswith("r:"):
        i = int(d[2:])
        if (s := sub(uid, i)) and s["cycle"] != "once":
            return await show((f"<b>⏭ 续费一期</b>　{esc(s['name'])}\n{LINE}\n"
                               f"到期　{s['due']} → <b>{next_due(s)}</b>",
                               Kb([[B("✅ 确认续费", f"R:{i}:{s['due']}"), B("取消", f"v:{i}")]])))
        return await show(v_list(uid))
    if d.startswith("R:"):
        _, i, old = d.split(":")
        i = int(i)
        if (s := sub(uid, i)) and s["cycle"] != "once" and s["due"] == old:
            nd = next_due(s)
            upd_sub(uid, i, due=nd.isoformat(), rolled_at="")
            return await show(v_detail(uid, i, f"✅ 已续费一期 → {nd}"))
        return await show(v_detail(uid, i))
    if d.startswith("d:"):
        i = int(d[2:])
        if s := sub(uid, i):
            return await show((f"<b>🗑 删除订阅</b>\n{LINE}\n确定删除「{esc(s['name'])}」吗？",
                               Kb([[B("确认删除", f"D:{i}"), B("取消", f"v:{i}")]])))
        return await show(v_list(uid))
    if d.startswith("D:"):
        del_sub(uid, int(d[2:]))
        return await show(v_list(uid))

    # 设置
    if d == "set":
        reset(ud)
        return await show(v_settings(uid))
    if d.startswith("w:") and d[2:] in {"off", *map(str, WIN_OPTS)}:
        upd_user(uid, days="" if d[2:] == "off" else d[2:])
        return await show(v_settings(uid))
    if d == "sq":
        upd_user(uid, quiet=0 if int(user(uid)["quiet"]) else 1)
        return await show(v_settings(uid))
    if d == "st":
        ud["await"] = ("time",)
        return await show(v_time(uid))

    # 工具
    if d == "prev":
        await send_long(ctx.bot, uid, notice(uid, force=True))
        return
    if d == "exp":
        u = user(uid)
        data = {"exported_at": datetime.now(TZ).isoformat(timespec="seconds"),
                "settings": {k: u[k] for k in ("days", "time", "quiet")},
                "subs": [{k: v for k, v in s.items() if k not in ("uid", "rolled_at")} for s in subs(uid)]}
        await ctx.bot.send_document(uid, document=json.dumps(data, ensure_ascii=False, indent=2).encode(),
                                    filename=f"duebot-{today()}.json", caption="📤 订阅备份")
        return

    if d == "imp":
        reset(ud)
        ud["await"] = ("import",)
        return await show(v_import())
    if d in ("imr", "ima") and (imp := ud.pop("import", None)):
        reset(ud)
        new = imp["subs"]
        if d == "imr":
            with db() as c:
                c.execute("DELETE FROM subs WHERE uid=?", (uid,))
            if imp["settings"]:
                upd_user(uid, **imp["settings"])
                schedule(ctx.application, uid)
            skipped = 0
        else:
            have = {sub_key(x) for x in subs(uid)}
            fresh = [x for x in new if sub_key(x) not in have]
            skipped, new = len(new) - len(fresh), fresh
        room = MAX_SUBS - len(subs(uid))
        over = max(0, len(new) - room)
        for x in new[:room]:
            add_sub(uid, x)
        note = f"✅ 已导入 {len(new) - over} 个订阅"
        if skipped:
            note += f"，跳过 {skipped} 个相同的"
        if over:
            note += f"，超出上限 {over} 个未导入"
        return await show(v_home(uid, note))

    # 管理员
    if uid == ADMIN:
        if d == "U":
            reset(ud)
            return await show(v_users())
        if d.startswith("u:"):
            return await show(v_user(int(d[2:])))
        if d == "uadd":
            ud["await"] = ("adduser",)
            return await show(v_adduser())
        if d.startswith("ux:"):
            t = int(d[3:])
            if (u := user(t)) and t != ADMIN:
                return await show((f"<b>🗑 移除用户</b>\n{LINE}\n确定移除「{uname(u)}」吗？\n"
                                   f"<i>TA 的 {len(subs(t))} 个订阅会一起删除，无法恢复</i>",
                                   Kb([[B("确认移除", f"UX:{t}"), B("取消", f"u:{t}")]])))
            return await show(v_users())
        if d.startswith("UX:"):
            t = int(d[3:])
            if t != ADMIN:
                del_user(t)
                unschedule(ctx.application, t)
            return await show(v_users("✅ 已移除"))

    # 失效按钮（如重启后点旧面板里的半截流程）回到首页
    reset(ud)
    await show(v_home(uid, "⚠️ 操作已失效，请重新开始"))


async def on_doc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not (uid := gate(update)):
        return
    ud, msg = ctx.user_data, update.message
    if ud.get("await") != ("import",):
        return await msg.reply_text("要导入备份，请先在面板里点「📥 导入备份」")
    doc = msg.document
    try:
        await msg.delete()
    except TelegramError:
        pass
    if (doc.file_size or 0) > 1024 * 1024:
        return await render(ctx, uid, v_import("文件太大"))
    raw = await (await doc.get_file()).download_as_bytearray()
    parsed = parse_backup(bytes(raw).decode("utf-8", "ignore"))
    if not parsed:
        return await render(ctx, uid, v_import("不是 DueBot 的备份文件"))
    good, st, bad = parsed
    if not good:
        return await render(ctx, uid, v_import("备份里没有有效的订阅"))
    ud.pop("await", None)
    ud["import"] = {"subs": good, "settings": st, "bad": bad}
    await render(ctx, uid, v_import_preview(uid, ud["import"]))


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not (uid := gate(update)):
        return
    ud, msg = ctx.user_data, update.message
    st, txt = ud.get("await"), " ".join(msg.text.split())
    if not st:
        return await msg.reply_text("发送 /menu 打开面板")
    try:  # 删掉用户输入，聊天里只留一个面板
        await msg.delete()
    except TelegramError:
        pass

    if st[0] == "add":
        step, dr = st[1], ud.setdefault("draft", {})
        err = None
        if step == "name":
            if 0 < len(txt) <= 40:
                dr["name"] = txt
            else:
                err = "名称 1–40 个字"
        elif step == "due":
            if dt := parse_date(txt, dr["cycle"] != "once"):
                dr["due"], dr["anchor"] = roll_date(dt, dr["cycle"], dt.day).isoformat(), dt.day
            else:
                err = "日期格式不对"
        elif step == "amount":
            if (a := parse_amount(txt)) is not None:
                dr["amount"] = str(a)
            else:
                err = "请输入数字"
        elif step == "unit":
            if 0 < len(txt) <= 10:
                dr["unit"] = txt
            else:
                err = "单位 1–10 个字"
        else:
            err = "请点下方按钮选择"
        if err:
            return await render(ctx, uid, v_add(uid, dr, step, err))
        if step == "unit":
            ud.pop("await", None)
            return await render(ctx, uid, v_confirm(dr))
        nxt = STEPS[STEPS.index(step) + 1]
        ud["await"] = ("add", nxt)
        return await render(ctx, uid, v_add(uid, dr, nxt))

    if st[0] == "edit":
        _, field, i = st
        if not sub(uid, i):
            ud.pop("await", None)
            return await render(ctx, uid, v_list(uid))
        if field == "name":
            ok, kw = 0 < len(txt) <= 40, {"name": txt}
        elif field == "due":
            cyc = sub(uid, i)["cycle"]
            dt = parse_date(txt, cyc != "once")
            ok = dt is not None
            kw = {"due": roll_date(dt, cyc, dt.day).isoformat(), "anchor": dt.day, "rolled_at": ""} if ok else {}
        elif field == "amount":
            a = parse_amount(txt)
            ok, kw = a is not None, {"amount": str(a)}
        else:
            ok, kw = 0 < len(txt) <= 10, {"unit": txt}
        if not ok:
            return await render(ctx, uid, v_edit(uid, i, field, "格式不对，请重新输入"))
        upd_sub(uid, i, **kw)
        ud.pop("await", None)
        return await render(ctx, uid, v_detail(uid, i, f"✅ {LABEL[field]}已更新"))

    if st[0] == "time":
        if not (t := parse_time(txt)):
            return await render(ctx, uid, v_time(uid, "时间格式不对"))
        upd_user(uid, time=t)
        schedule(ctx.application, uid)
        catch_up(ctx.application, user(uid))
        ud.pop("await", None)
        return await render(ctx, uid, v_settings(uid, f"✅ 推送时间改为 {t}"))

    if st[0] == "adduser" and uid == ADMIN:
        if not re.fullmatch(r"\d{5,15}", txt):
            return await render(ctx, uid, v_adduser("ID 只能是数字"))
        t = int(txt)
        if user(t):
            return await render(ctx, uid, v_adduser("这个 ID 已经在列表里了"))
        add_user(t)
        schedule(ctx.application, t)
        ud.pop("await", None)
        try:
            await ctx.bot.send_message(t, "👋 你已获得 <b>DueBot</b> 的使用权限\n发送 /menu 开始使用",
                                       parse_mode=HTML)
            note = "✅ 已添加，并已通知对方"
        except TelegramError:
            note = (f"✅ 已添加\n对方还没和 bot 说过话，让 TA 搜索 "
                    f"@{esc(ctx.bot.username)} 并发送 /start")
        return await render(ctx, uid, v_users(note))


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.warning("处理出错：%r", ctx.error)


async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("menu", "打开控制面板"),
        BotCommand("add", "添加订阅"),
        BotCommand("cancel", "取消当前操作"),
    ])
    for u in users():
        schedule(app, u["uid"])
        catch_up(app, u)


def main():
    if not ADMIN:
        raise SystemExit("请在 .env 里设置 ADMIN_ID（你的 Telegram 数字 ID）")
    init_db()
    b = Application.builder().token(TOKEN).post_init(post_init)
    if PROXY:
        b = b.proxy(PROXY).get_updates_proxy(PROXY)
    app = b.build()
    new = filters.UpdateType.MESSAGE
    app.add_handler(CommandHandler(["menu", "start"], cmd_menu, filters=new))
    app.add_handler(CommandHandler("add", cmd_add, filters=new))
    app.add_handler(CommandHandler("cancel", cmd_cancel, filters=new))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler(new & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(new & filters.Document.ALL, on_doc))
    app.add_error_handler(on_error)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
