"""Серверная часть игры: облачные сохранения и правки персонажей администратором.

Как это работает:
- Игра игрока шлёт своё сохранение на /api/save (подпись Telegram initData проверяется по токену бота).
- Администратор через /api/admin/get смотрит сохранение любого игрока и через /api/admin/op
  ставит в очередь правки (ресурсы, уровень, доспехи).
- Игра игрока при запуске и раз в полминуты забирает очередь через /api/ops, применяет правки
  и подтверждает их через /api/ack. Так правка не затирается сохранением самого игрока.
"""
import hashlib
import hmac
import json
import re
import sqlite3
import time
from urllib.parse import parse_qsl

from aiohttp import web

RES_FIELDS = {"c", "cr", "pr", "en", "pts"}   # монеты, кристаллы, жемчуг, энергия, очки навыков
ID_RE = re.compile(r"^[A-Za-z0-9_]{1,32}$")
MAX_STATE = 1_500_000        # байт, предел размера одного сохранения
INIT_TTL = 7 * 86400         # сколько секунд подпись initData считается свежей
MAX_VALUE = 10 ** 9
MAX_PENDING = 200            # предел очереди правок на одного игрока


def check_init(init_data: str, token: str):
    """Проверяет подпись Telegram Mini App. Возвращает словарь user или None."""
    if not init_data or not isinstance(init_data, str):
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    except ValueError:
        return None
    got = pairs.pop("hash", None)
    if not got:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, got):
        return None
    try:
        if time.time() - int(pairs.get("auth_date", "0")) > INIT_TTL:
            return None
        user = json.loads(pairs.get("user", "{}"))
    except (ValueError, TypeError):
        return None
    if not isinstance(user, dict) or not isinstance(user.get("id"), int):
        return None
    return user


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and abs(v) <= MAX_VALUE


def clean_op(o):
    """Пропускает только известные правки с безопасными значениями."""
    if not isinstance(o, dict):
        return None
    k = o.get("k")
    if k == "res" and o.get("f") in RES_FIELDS and _num(o.get("v")):
        return {"k": "res", "f": o["f"], "v": int(o["v"])}
    if k == "ore" and isinstance(o.get("id"), str) and ID_RE.match(o["id"]) and _num(o.get("v")):
        return {"k": "ore", "id": o["id"], "v": int(o["v"])}
    if k == "lvl" and _num(o.get("v")) and 1 <= int(o["v"]) <= 80:
        return {"k": "lvl", "v": int(o["v"])}
    if k == "gear" and isinstance(o.get("id"), str) and ID_RE.match(o["id"]):
        return {"k": "gear", "id": o["id"], "rm": 1 if o.get("rm") else 0}
    return None


class Store:
    def __init__(self, path: str):
        self.path = path
        with self._c() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS saves (user_id INTEGER PRIMARY KEY, state TEXT, "
                "username TEXT, first_name TEXT, ts INTEGER)"
            )
            c.execute(
                "CREATE TABLE IF NOT EXISTS ops (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, "
                "op TEXT, ts INTEGER, done INTEGER DEFAULT 0)"
            )
            c.execute("CREATE INDEX IF NOT EXISTS ops_user ON ops (user_id, done)")
            c.execute(
                "CREATE TABLE IF NOT EXISTS admin_log (id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER, "
                "target_id INTEGER, op TEXT, ts INTEGER)"
            )

    def _c(self):
        return sqlite3.connect(self.path)

    def save(self, uid, state, username, first_name):
        with self._c() as c:
            c.execute(
                "INSERT INTO saves (user_id, state, username, first_name, ts) VALUES (?,?,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET state=excluded.state, username=excluded.username, "
                "first_name=excluded.first_name, ts=excluded.ts",
                (uid, state, username, first_name, int(time.time())),
            )

    def get(self, uid):
        with self._c() as c:
            return c.execute(
                "SELECT state, username, first_name, ts FROM saves WHERE user_id=?", (uid,)
            ).fetchone()

    def find(self, text: str):
        """Ищет ID по числу или по @username (в сохранениях и в статистике бота)."""
        text = (text or "").strip()
        if text.lstrip("-").isdigit():
            return int(text)
        name = text.lstrip("@").lower()
        if not name:
            return None
        with self._c() as c:
            r = c.execute("SELECT user_id FROM saves WHERE lower(username)=?", (name,)).fetchone()
            if r:
                return r[0]
            try:
                r = c.execute("SELECT id FROM users WHERE lower(username)=?", (name,)).fetchone()
            except sqlite3.OperationalError:
                r = None
            return r[0] if r else None

    def add_op(self, admin_id, uid, op):
        with self._c() as c:
            n = c.execute("SELECT COUNT(*) FROM ops WHERE user_id=? AND done=0", (uid,)).fetchone()[0]
            if n >= MAX_PENDING:
                return None
            j = json.dumps(op, ensure_ascii=False)
            cur = c.execute("INSERT INTO ops (user_id, op, ts) VALUES (?,?,?)", (uid, j, int(time.time())))
            c.execute(
                "INSERT INTO admin_log (admin_id, target_id, op, ts) VALUES (?,?,?,?)",
                (admin_id, uid, j, int(time.time())),
            )
            return cur.lastrowid

    def pending(self, uid):
        with self._c() as c:
            return c.execute("SELECT COUNT(*) FROM ops WHERE user_id=? AND done=0", (uid,)).fetchone()[0]

    def take(self, uid):
        with self._c() as c:
            rows = c.execute(
                "SELECT id, op FROM ops WHERE user_id=? AND done=0 ORDER BY id LIMIT 100", (uid,)
            ).fetchall()
        return [{"id": r[0], "op": json.loads(r[1])} for r in rows]

    def ack(self, uid, ids):
        ids = [int(i) for i in ids if isinstance(i, int) and not isinstance(i, bool)][:200]
        if not ids:
            return
        with self._c() as c:
            c.executemany("UPDATE ops SET done=1 WHERE id=? AND user_id=?", [(i, uid) for i in ids])


def make_app(token: str, db_path: str, admin_ids) -> web.Application:
    store = Store(db_path)
    admins = {int(x) for x in admin_ids}

    @web.middleware
    async def cors(request, handler):
        if request.method == "OPTIONS":
            resp = web.Response(status=204)
        else:
            try:
                resp = await handler(request)
            except web.HTTPException as e:
                resp = web.json_response({"ok": False, "err": e.reason}, status=e.status)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS"
        return resp

    def err(msg, status=400):
        return web.json_response({"ok": False, "err": msg}, status=status)

    async def body(request):
        try:
            d = await request.json()
        except Exception:
            return None, None, err("bad json")
        if not isinstance(d, dict):
            return None, None, err("bad json")
        user = check_init(d.get("initData"), token)
        if not user:
            return None, None, err("auth", 401)
        return user, d, None

    async def ping(request):
        return web.json_response({"ok": True})

    async def save(request):
        user, d, e = await body(request)
        if e:
            return e
        st = d.get("state")
        if not isinstance(st, dict):
            return err("state")
        j = json.dumps(st, ensure_ascii=False, separators=(",", ":"))
        if len(j) > MAX_STATE:
            return err("too big", 413)
        store.save(user["id"], j, user.get("username"), user.get("first_name"))
        return web.json_response({"ok": True})

    async def ops(request):
        user, d, e = await body(request)
        if e:
            return e
        return web.json_response({"ok": True, "ops": store.take(user["id"])})

    async def ack(request):
        user, d, e = await body(request)
        if e:
            return e
        ids = d.get("ids")
        store.ack(user["id"], ids if isinstance(ids, list) else [])
        return web.json_response({"ok": True})

    async def admin_user(request):
        user, d, e = await body(request)
        if e:
            return None, None, e
        if user["id"] not in admins:
            return None, None, err("forbidden", 403)
        uid = store.find(str(d.get("target", "")))
        if uid is None:
            return None, None, err("not found", 404)
        return user, (uid, d), None

    async def admin_get(request):
        user, r, e = await admin_user(request)
        if e:
            return e
        uid, d = r
        row = store.get(uid)
        out = {"ok": True, "id": uid, "pending": store.pending(uid), "found": bool(row)}
        if row:
            out.update(state=json.loads(row[0]), username=row[1], name=row[2], ts=row[3])
        return web.json_response(out)

    async def admin_op(request):
        user, r, e = await admin_user(request)
        if e:
            return e
        uid, d = r
        op = clean_op(d.get("op"))
        if not op:
            return err("bad op")
        oid = store.add_op(user["id"], uid, op)
        if oid is None:
            return err("queue full", 429)
        return web.json_response({"ok": True, "id": oid, "op": op, "pending": store.pending(uid)})

    app = web.Application(middlewares=[cors], client_max_size=2 * 1024 * 1024)
    app.add_routes(
        [
            web.get("/api/ping", ping),
            web.post("/api/save", save),
            web.post("/api/ops", ops),
            web.post("/api/ack", ack),
            web.post("/api/admin/get", admin_get),
            web.post("/api/admin/op", admin_op),
            web.route("OPTIONS", "/{tail:.*}", ping),
        ]
    )
    return app
