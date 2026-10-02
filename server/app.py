"""拼了个图 —— 自托管后端

FastAPI + SQLite，提供注册登录、成绩排行榜、图片存储。
前端通过同源的 /api/ 访问，静态资源由 nginx 托管。

密码：PBKDF2-HMAC-SHA256，20 万次迭代（避免 passlib/bcrypt 版本兼容问题）
会话：JWT，写在 httpOnly Cookie 里
"""

import os
import re
import sqlite3
import secrets
import hashlib
import uuid
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import jwt
import qq_auth
import sms_auth
import wechat
from fastapi import Depends, FastAPI, HTTPException, UploadFile, File, Cookie, Response, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

# ---------------------------------------------------------------- 配置

BASE_DIR = os.environ.get("PINTU_BASE", "/opt/pintu")
DB_PATH = os.path.join(BASE_DIR, "data", "pintu.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "data", "uploads")
STATIC_DIR = os.path.join(BASE_DIR, "static")

# 站点的对外根地址，QQ / 微信回调必须是公网可达的绝对 URL
PUBLIC_BASE = os.environ.get("PINTU_PUBLIC_BASE", "").strip().rstrip("/")

# 微信回调地址必须是 https，本地 http 调试时把 cookie 的 Secure 关掉才会写入
COOKIE_SECURE = os.environ.get("PINTU_COOKIE_SECURE", "1").strip().lower() not in ("0", "false", "no")

SECRET_KEY = os.environ.get("PINTU_SECRET")
if not SECRET_KEY:
    # 首次运行自动生成并持久化，避免每次重启把所有人踢下线
    key_file = os.path.join(BASE_DIR, "data", ".secret")
    if os.path.exists(key_file):
        SECRET_KEY = open(key_file).read().strip()
    else:
        SECRET_KEY = secrets.token_urlsafe(48)
        with open(key_file, "w") as f:
            f.write(SECRET_KEY)
        os.chmod(key_file, 0o600)

JWT_ALG = "HS256"
JWT_DAYS = 30
MAX_IMAGE_BYTES = 8 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
SMS_TTL_SECONDS = 300
SMS_RESEND_SECONDS = 60
SMS_PHONE_DAILY_LIMIT = 10
SMS_IP_DAILY_LIMIT = 30
SMS_MAX_ATTEMPTS = 5

app = FastAPI(title="pintu", docs_url=None, redoc_url=None)


# ---------------------------------------------------------------- 数据库

@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def upsert_identity_columns(conn):
    """给老库补第三方身份和本地成绩同步需要的列，重复执行无副作用。"""
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    for column, decl in [
        ("provider", "provider TEXT NOT NULL DEFAULT 'local'"),
        ("provider_uid", "provider_uid TEXT"),
        ("avatar_url", "avatar_url TEXT NOT NULL DEFAULT ''"),
        ("updated_at", "updated_at TEXT"),
    ]:
        if column not in existing:
            conn.execute(f"ALTER TABLE users ADD COLUMN {decl}")

    score_columns = {r["name"] for r in conn.execute("PRAGMA table_info(scores)").fetchall()}
    if "client_id" not in score_columns:
        conn.execute("ALTER TABLE scores ADD COLUMN client_id TEXT")


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id            TEXT PRIMARY KEY,
                email         TEXT NOT NULL UNIQUE,
                name          TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at    TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scores (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     TEXT NOT NULL,
                user_name   TEXT NOT NULL,
                size        INTEGER NOT NULL,
                seconds     INTEGER NOT NULL,
                duration_ms INTEGER NOT NULL,
                moves       INTEGER NOT NULL,
                played_at   TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_scores_rank ON scores(size, duration_ms, moves)")
        upsert_identity_columns(conn)
        # 同一个外部账号只能有一条记录；本地账号 provider_uid 为 NULL，不受唯一约束影响
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_identity ON users(provider, provider_uid)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_scores_client ON scores(user_id, client_id)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sms_codes (
                phone       TEXT PRIMARY KEY,
                code_hash   TEXT NOT NULL,
                expires_at  REAL NOT NULL,
                attempts    INTEGER NOT NULL DEFAULT 0,
                sent_at     REAL NOT NULL,
                request_ip  TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sms_send_log (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                phone    TEXT NOT NULL,
                ip       TEXT NOT NULL,
                sent_at  REAL NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sms_log_phone ON sms_send_log(phone, sent_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sms_log_ip ON sms_send_log(ip, sent_at)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS oauth_states (
                state       TEXT PRIMARY KEY,
                provider    TEXT NOT NULL,
                expires_at  REAL NOT NULL
            )
        """)


init_db()


# ---------------------------------------------------------------- 密码与会话

def hash_password(raw: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", raw.encode("utf-8"), salt.encode(), 200_000).hex()
    return f"pbkdf2$200000${salt}${digest}"


def verify_password(raw: str, stored: str) -> bool:
    try:
        algo, iters, salt, digest = stored.split("$")
        if algo != "pbkdf2":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", raw.encode(), salt.encode(), int(iters)).hex()
        return secrets.compare_digest(calc, digest)
    except Exception:
        return False


def make_token(user_id: str) -> str:
    payload = {
        "sub": user_id,
        "exp": datetime.now(timezone.utc) + timedelta(days=JWT_DAYS),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=JWT_ALG)


def set_session(resp: Response, user_id: str):
    resp.set_cookie(
        key="pintu_session",
        value=make_token(user_id),
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,     # 全站 HTTPS；本地 http 调试可用环境变量关掉
        max_age=JWT_DAYS * 86400,
        path="/",
    )


def current_user(session: str = Cookie(default=None, alias="pintu_session")):
    """返回当前登录用户，未登录返回 None。"""
    if not session:
        return None
    try:
        payload = jwt.decode(session, SECRET_KEY, algorithms=[JWT_ALG])
        uid = payload.get("sub")
        if not uid:
            return None
    except jwt.PyJWTError:
        return None
    with db() as conn:
        row = conn.execute(
            "SELECT id, email, name, avatar_url, provider FROM users WHERE id=?", (uid,)
        ).fetchone()
    return dict(row) if row else None


def require_user(session: str = Cookie(default=None, alias="pintu_session")):
    user = current_user(session)
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


# ---------------------------------------------------------------- 请求模型

class RegisterIn(BaseModel):
    email: str
    password: str
    name: str


class LoginIn(BaseModel):
    email: str
    password: str


class RenameIn(BaseModel):
    name: str


class ScoreIn(BaseModel):
    size: int
    seconds: int
    duration_ms: int
    moves: int
    client_id: str | None = None


class PhoneCodeIn(BaseModel):
    phone: str


class PhoneVerifyIn(BaseModel):
    phone: str
    code: str
    name: str = ""


# ---------------------------------------------------------------- 认证接口

@app.post("/api/auth/register")
def register(body: RegisterIn, response: Response):
    email = body.email.strip().lower()
    name = body.name.strip()
    if not EMAIL_RE.match(email):
        raise HTTPException(400, "邮箱格式不正确")
    if len(body.password) < 8:
        raise HTTPException(400, "密码至少 8 位")
    if not (1 <= len(name) <= 16):
        raise HTTPException(400, "名字需要 1—16 个字符")

    with db() as conn:
        exists = conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone()
        if exists:
            raise HTTPException(409, "该邮箱已注册")
        uid = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO users (id, email, name, password_hash, created_at) VALUES (?,?,?,?,?)",
            (uid, email, name, hash_password(body.password), datetime.now(timezone.utc).isoformat()),
        )
    set_session(response, uid)
    return {"id": uid, "email": email, "name": name}


@app.post("/api/auth/login")
def login(body: LoginIn, response: Response):
    email = body.email.strip().lower()
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not row or not verify_password(body.password, row["password_hash"]):
        raise HTTPException(401, "邮箱或密码不正确")
    set_session(response, row["id"])
    return {"id": row["id"], "email": row["email"], "name": row["name"]}


@app.post("/api/auth/logout")
def logout(response: Response):
    response.delete_cookie("pintu_session", path="/")
    return {"ok": True}


def _request_ip(request: Request) -> str:
    return (request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown"))[:64]


def _code_hash(phone: str, code: str) -> str:
    return hashlib.sha256(f"{SECRET_KEY}:{phone}:{code}".encode("utf-8")).hexdigest()


def _upsert_provider_user(provider: str, provider_uid: str, suggested_name: str,
                          avatar_url: str = "") -> dict:
    """首次绑定建号；再次登录刷新头像，但保留玩家修改过的云端名称。"""
    now = datetime.now(timezone.utc).isoformat()
    name = suggested_name.strip()[:16] or "拼图玩家"
    avatar = avatar_url.strip()
    with db() as conn:
        row = conn.execute(
            "SELECT id, email, name, avatar_url FROM users WHERE provider=? AND provider_uid=?",
            (provider, provider_uid),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE users SET avatar_url=?, updated_at=? WHERE id=?",
                (avatar or row["avatar_url"], now, row["id"]),
            )
            return {"id": row["id"], "email": row["email"], "name": row["name"],
                    "avatar_url": avatar or row["avatar_url"], "provider": provider}

        uid = uuid.uuid4().hex
        email = f"{provider}_{provider_uid}@{provider}.local"
        conn.execute(
            "INSERT INTO users (id, email, name, password_hash, created_at, provider,"
            " provider_uid, avatar_url, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (uid, email, name, "", now, provider, provider_uid, avatar, now),
        )
        return {"id": uid, "email": email, "name": name, "avatar_url": avatar,
                "provider": provider}


# ---------------------------------------------------------------- 手机号验证码登录

@app.post("/api/auth/phone/code")
def send_phone_code(body: PhoneCodeIn, request: Request):
    phone = body.phone.strip()
    if not PHONE_RE.fullmatch(phone):
        raise HTTPException(400, "请输入正确的中国大陆手机号")
    if not sms_auth.enabled():
        raise HTTPException(503, "短信登录尚未配置")

    now = time.time()
    ip = _request_ip(request)
    with db() as conn:
        conn.execute("DELETE FROM sms_send_log WHERE sent_at<?", (now - 86400,))
        latest = conn.execute(
            "SELECT MAX(sent_at) AS sent_at FROM sms_send_log WHERE phone=?", (phone,)
        ).fetchone()["sent_at"]
        if latest and now - latest < SMS_RESEND_SECONDS:
            wait = max(1, int(SMS_RESEND_SECONDS - (now - latest)))
            raise HTTPException(429, f"请在 {wait} 秒后重试")
        phone_count = conn.execute(
            "SELECT COUNT(*) AS n FROM sms_send_log WHERE phone=? AND sent_at>=?",
            (phone, now - 86400),
        ).fetchone()["n"]
        ip_count = conn.execute(
            "SELECT COUNT(*) AS n FROM sms_send_log WHERE ip=? AND sent_at>=?",
            (ip, now - 86400),
        ).fetchone()["n"]
        if phone_count >= SMS_PHONE_DAILY_LIMIT or ip_count >= SMS_IP_DAILY_LIMIT:
            raise HTTPException(429, "今天发送次数过多，请明天再试")

    code = f"{secrets.randbelow(1_000_000):06d}"
    try:
        sms_auth.send_code(phone, code, SMS_TTL_SECONDS // 60)
    except sms_auth.SmsError as exc:
        raise HTTPException(502, f"验证码发送失败：{exc}") from exc

    with db() as conn:
        conn.execute(
            "INSERT INTO sms_send_log (phone, ip, sent_at) VALUES (?,?,?)",
            (phone, ip, now),
        )
        conn.execute(
            "INSERT INTO sms_codes (phone, code_hash, expires_at, attempts, sent_at, request_ip)"
            " VALUES (?,?,?,?,?,?) ON CONFLICT(phone) DO UPDATE SET"
            " code_hash=excluded.code_hash, expires_at=excluded.expires_at, attempts=0,"
            " sent_at=excluded.sent_at, request_ip=excluded.request_ip",
            (phone, _code_hash(phone, code), now + SMS_TTL_SECONDS, 0, now, ip),
        )
    result = {"ok": True, "expiresIn": SMS_TTL_SECONDS, "resendIn": SMS_RESEND_SECONDS}
    if sms_auth.is_mock():
        result["debugCode"] = code
    return result


@app.post("/api/auth/phone/verify")
def verify_phone_code(body: PhoneVerifyIn, response: Response):
    phone = body.phone.strip()
    code = body.code.strip()
    if not PHONE_RE.fullmatch(phone) or not re.fullmatch(r"\d{6}", code):
        raise HTTPException(400, "手机号或验证码格式不正确")

    now = time.time()
    with db() as conn:
        row = conn.execute("SELECT * FROM sms_codes WHERE phone=?", (phone,)).fetchone()
        if not row or row["expires_at"] < now:
            conn.execute("DELETE FROM sms_codes WHERE phone=?", (phone,))
            raise HTTPException(400, "验证码已失效，请重新获取")
        if row["attempts"] >= SMS_MAX_ATTEMPTS:
            raise HTTPException(429, "验证码错误次数过多，请重新获取")
        if not secrets.compare_digest(row["code_hash"], _code_hash(phone, code)):
            conn.execute("UPDATE sms_codes SET attempts=attempts+1 WHERE phone=?", (phone,))
            raise HTTPException(400, "验证码不正确")
        conn.execute("DELETE FROM sms_codes WHERE phone=?", (phone,))

    user = _upsert_provider_user(
        "phone", phone, body.name or f"玩家{phone[-4:]}"
    )
    set_session(response, user["id"])
    return user


# ---------------------------------------------------------------- QQ 登录

@app.get("/api/auth/qq/start")
def qq_start(request: Request):
    if not qq_auth.enabled():
        raise HTTPException(503, "QQ 登录尚未配置")
    state = secrets.token_urlsafe(24)
    with db() as conn:
        conn.execute("DELETE FROM oauth_states WHERE expires_at<?", (time.time(),))
        conn.execute(
            "INSERT INTO oauth_states (state, provider, expires_at) VALUES (?,?,?)",
            (state, "qq", time.time() + 600),
        )
    callback_url = f"{_callback_base(request)}/api/auth/qq/callback"
    if qq_auth.is_mock():
        authorize_url = f"{callback_url}?code=mock_user&state={state}"
    else:
        authorize_url = qq_auth.build_authorize_url(callback_url, state)
    return {"authorizeUrl": authorize_url}


@app.get("/api/auth/qq/callback")
def qq_callback(request: Request, code: str = "", state: str = ""):
    if not code or not state:
        raise HTTPException(400, "QQ 登录参数不完整")
    with db() as conn:
        row = conn.execute(
            "SELECT provider, expires_at FROM oauth_states WHERE state=?", (state,)
        ).fetchone()
        conn.execute("DELETE FROM oauth_states WHERE state=?", (state,))
    if not row or row["provider"] != "qq" or row["expires_at"] < time.time():
        raise HTTPException(400, "QQ 登录请求已失效，请重新发起")

    callback_url = f"{_callback_base(request)}/api/auth/qq/callback"
    try:
        openid, profile = qq_auth.login_by_code(code, callback_url)
    except qq_auth.QQError as exc:
        raise HTTPException(502, str(exc)) from exc
    user = _upsert_provider_user(
        "qq", openid, profile.get("nickname") or "QQ玩家", profile.get("avatar_url") or ""
    )
    redirect = RedirectResponse(url="/?bound=qq", status_code=302)
    set_session(redirect, user["id"])
    return redirect


# ---------------------------------------------------------------- 微信登录

# 一次性凭证 → 用户。扫码登录时，授权发生在手机微信里，
# 网页端靠轮询拿到这个凭证再换自己的会话。
_exchange_store: dict[str, tuple[str, float]] = {}
EXCHANGE_TTL = 120


def _new_exchange(user_id: str) -> str:
    key = secrets.token_urlsafe(32)
    _exchange_store[key] = (user_id, time.time())
    return key


def _take_exchange(key: str) -> str | None:
    item = _exchange_store.pop(key, None)
    if item is None:
        return None
    user_id, created_at = item
    return user_id if time.time() - created_at <= EXCHANGE_TTL else None


def _callback_base(request: Request) -> str:
    """回调地址的域名前缀：优先环境变量，其次当前请求的 origin。"""
    return PUBLIC_BASE or str(request.base_url).rstrip("/")


def _upsert_wechat_user(openid: str, profile: dict) -> tuple[dict, bool]:
    """首次登录建号并返回；老用户刷新头像，但保留玩家在游戏里改过的名字。"""
    now = datetime.now(timezone.utc).isoformat()
    nickname = (profile.get("nickname") or "").strip()[:16] or "微信玩家"
    avatar = (profile.get("avatar_url") or "").strip()

    with db() as conn:
        row = conn.execute(
            "SELECT id, name, avatar_url FROM users WHERE provider='wechat' AND provider_uid=?",
            (openid,),
        ).fetchone()

        if row is None:
            uid = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO users (id, email, name, password_hash, created_at,"
                " provider, provider_uid, avatar_url, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (uid, f"{openid}@wechat.local", nickname, "", now,
                 "wechat", openid, avatar, now),
            )
            return {"id": uid, "name": nickname, "avatar_url": avatar}, True

        # avatar 为空时不覆盖，避免接口偶尔返回空头像把已有头像洗掉
        conn.execute(
            "UPDATE users SET avatar_url=?, updated_at=? WHERE id=?",
            (avatar or row["avatar_url"], now, row["id"]),
        )
        # name 故意不动：本地改过的名字才是权威值
        return {"id": row["id"], "name": row["name"],
                "avatar_url": avatar or row["avatar_url"]}, False


@app.get("/api/auth/wechat/start")
def wechat_start(request: Request):
    """返回一条授权地址。微信内直接跳，微信外画成二维码让用户扫码。"""
    if not wechat.enabled():
        raise HTTPException(503, "服务端未配置微信登录")

    state = wechat.new_state()
    wechat.save_poll_state(state)
    callback_url = f"{_callback_base(request)}/api/auth/wechat/callback"
    return {
        "authorizeUrl": wechat.build_authorize_url(callback_url, state),
        "state": state,
        "mock": wechat.is_mock(),
    }


@app.get("/api/auth/wechat/callback")
def wechat_callback(request: Request, code: str = "", state: str = ""):
    """微信授权后跳回来的落点。无论微信内还是扫码，都走这里。"""
    if not code:
        raise HTTPException(400, "缺少 code")

    try:
        openid, profile = wechat.login_by_code(code)
    except wechat.WeChatError as exc:
        raise HTTPException(502, str(exc))

    user, _ = _upsert_wechat_user(openid, profile)
    wechat.mark_poll_done(state, exchange=_new_exchange(user["id"]))

    # 注意：显式返回 Response 实例时，注入的 response 参数上设的 Cookie 会被丢弃，
    # 所以这里必须在真正要返回的那个响应上挂 Cookie。
    html = HTMLResponse("""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="2;url=/">
<title>登录成功</title>
</head><body style="margin:0;display:flex;align-items:center;justify-content:center;
height:100vh;font-family:system-ui,sans-serif;background:#f6f7f9;color:#222">
<div style="text-align:center;line-height:1.8">
  <div style="font-size:40px;color:#07c160">&#10003;</div>
  <div style="font-size:17px">登录成功</div>
  <div style="font-size:13px;color:#888">正在返回游戏…</div>
</div></body></html>""")
    html.set_cookie(
        key="pintu_session",
        value=make_token(user["id"]),
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
        max_age=JWT_DAYS * 86400,
        path="/",
    )
    return html


@app.get("/api/auth/wechat/poll")
def wechat_poll(state: str = ""):
    """网页端轮询扫码结果。ok=true 时带回一次性 exchange。"""
    item = wechat.read_poll_state(state)
    if item is None:
        raise HTTPException(404, "二维码已失效，请刷新重试")
    if not item.get("ok"):
        return {"ok": False}
    return {"ok": True, "exchange": item.get("exchange")}


class ClaimIn(BaseModel):
    exchange: str


@app.post("/api/auth/wechat/claim")
def wechat_claim(body: ClaimIn, response: Response):
    """扫码的那台浏览器用 exchange 换回自己的会话，这样双方都在线。"""
    user_id = _take_exchange(body.exchange)
    if not user_id:
        raise HTTPException(400, "凭证已失效，请重新扫码")

    with db() as conn:
        row = conn.execute(
            "SELECT id, email, name, avatar_url, provider FROM users WHERE id=?", (user_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "用户不存在")

    set_session(response, user_id)
    return dict(row)


@app.get("/api/auth/me")
def me(user=Depends(current_user)):
    return user


@app.put("/api/me/name")
def rename(body: RenameIn, user=Depends(require_user)):
    name = body.name.strip()
    if not (1 <= len(name) <= 16):
        raise HTTPException(400, "名字需要 1—16 个字符")
    with db() as conn:
        conn.execute("UPDATE users SET name=? WHERE id=?", (name, user["id"]))
        # 历史成绩同步显示名，排行榜才不会出现旧名字
        conn.execute("UPDATE scores SET user_name=? WHERE user_id=?", (name, user["id"]))
    return {"name": name}


# ---------------------------------------------------------------- 成绩与排行榜

@app.post("/api/scores")
def add_score(body: ScoreIn, user=Depends(require_user)):
    if body.size not in (2, 3, 4, 6):
        raise HTTPException(400, "难度不合法")
    client_id = (body.client_id or "").strip()[:80] or None
    with db() as conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO scores"
            " (user_id, user_name, size, seconds, duration_ms, moves, played_at, client_id)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (user["id"], user["name"], body.size, body.seconds,
             body.duration_ms, body.moves, datetime.now(timezone.utc).isoformat(), client_id),
        )
    return {"ok": True, "created": cursor.rowcount > 0}


@app.get("/api/rankings")
def rankings(size: int = 3):
    if size not in (2, 3, 4, 6):
        raise HTTPException(400, "难度不合法")
    with db() as conn:
        rows = conn.execute(
            "SELECT user_id, user_name, seconds, duration_ms, moves, played_at"
            " FROM scores WHERE size=?"
            " ORDER BY duration_ms ASC, moves ASC, played_at ASC, id ASC LIMIT 50",
            (size,),
        ).fetchall()
    return {"rows": [dict(r) for r in rows]}


# ---------------------------------------------------------------- 图片

def user_upload_dir(uid: str) -> str:
    path = os.path.join(UPLOAD_DIR, uid)
    os.makedirs(path, exist_ok=True)
    return path


@app.post("/api/images")
async def upload_image(file: UploadFile = File(...), user=Depends(require_user)):
    ctype = (file.content_type or "").lower()
    if ctype not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(400, "只支持 JPEG / PNG / WebP / GIF")
    data = await file.read()
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(413, "图片不能超过 8MB")

    ext = {  # 只用白名单后缀，不信客户端文件名
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }[ctype]
    name = uuid.uuid4().hex + ext
    with open(os.path.join(user_upload_dir(user["id"]), name), "wb") as f:
        f.write(data)
    return {"name": name, "url": f"/uploads/{user['id']}/{name}"}


@app.get("/api/images")
def list_images(user=Depends(require_user)):
    d = user_upload_dir(user["id"])
    items = []
    for fn in sorted(os.listdir(d), reverse=True):
        p = os.path.join(d, fn)
        if os.path.isfile(p):
            items.append({
                "name": fn,
                "url": f"/uploads/{user['id']}/{fn}",
                "size": os.path.getsize(p),
            })
    return {"items": items[:60]}


@app.delete("/api/images/{name}")
def delete_image(name: str, user=Depends(require_user)):
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "文件名不合法")
    p = os.path.join(user_upload_dir(user["id"]), name)
    if not os.path.isfile(p):
        raise HTTPException(404, "文件不存在")
    os.remove(p)
    return {"ok": True}


# ---------------------------------------------------------------- 健康检查

@app.get("/api/health")
def health():
    return {"ok": True, "time": datetime.now(timezone.utc).isoformat()}
