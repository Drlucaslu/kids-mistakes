"""PIN sign-in and signed session cookies.

The app has a public Olares entrance, so: PINs are hashed (scrypt), five wrong tries lock the account for
ten minutes, and sessions are HMAC-signed cookies that a PIN change invalidates (session_version)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from datetime import timedelta

from fastapi import HTTPException, Request

from . import db
from .config import get_settings
from .i18n import t

COOKIE = "cuoti_session"
SESSION_TTL_S = 60 * 60 * 24 * 60  # kids and tutors stay signed in on their own devices
MAX_FAILS = 5
LOCK_MINUTES = 10
MIN_PIN = {"child": 4, "tutor": 6, "parent": 6}


@dataclass
class Principal:
    id: int | None
    name: str
    role: str  # parent | tutor | child
    subjects: list[str] = field(default_factory=list)
    source: str = "session"  # session | olares

    @property
    def is_parent(self) -> bool:
        return self.role == "parent"

    @property
    def is_staff(self) -> bool:
        return self.role in ("parent", "tutor")

    def can_subject(self, subject: str) -> bool:
        return self.is_parent or (self.role == "tutor" and subject in self.subjects)

    def visible_subjects(self) -> list[str]:
        return list(db.SUBJECTS) if self.role != "tutor" else [s for s in db.SUBJECTS if s in self.subjects]


def hash_pin(pin: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pin.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${h.hex()}"


def check_pin(pin: str, stored: str) -> bool:
    try:
        _, salt, h = stored.split("$")
    except ValueError:
        return False
    got = hashlib.scrypt(pin.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
    return hmac.compare_digest(got.hex(), h)


def validate_pin(pin: str, role: str) -> str | None:
    if not pin.isdigit():
        return t("PIN 只能是数字")
    if len(pin) < MIN_PIN[role]:
        return t("孩子的 PIN 至少 {n} 位" if role == "child" else "老师/家长的 PIN 至少 {n} 位", n=MIN_PIN[role])
    return None


def _secret() -> bytes:
    path = get_settings().data_dir / "session.key"
    if not path.exists():
        path.write_text(secrets.token_hex(32))
        path.chmod(0o600)
    return path.read_text().strip().encode()


def make_token(uid: int, sv: int) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"uid": uid, "sv": sv, "exp": int(time.time()) + SESSION_TTL_S})
                                    .encode()).decode()
    sig = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def read_token(tok: str) -> dict | None:
    try:
        body, sig = tok.rsplit(".", 1)
    except ValueError:
        return None
    if not hmac.compare_digest(sig, hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()):
        return None
    data = json.loads(base64.urlsafe_b64decode(body.encode()))
    return data if data.get("exp", 0) > time.time() else None


def set_cookie(resp, token: str, request: Request) -> None:
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    resp.set_cookie(COOKIE, token, max_age=SESSION_TTL_S, httponly=True, secure=secure, samesite="lax", path="/")


def login(uid: int, pin: str) -> tuple[dict | None, str | None]:
    """Returns (user row, error message)."""
    with db.tx() as c:
        u = c.execute("SELECT * FROM users WHERE id = ? AND active = 1", (uid,)).fetchone()
        if not u:
            return None, t("账号不存在")
        now = db.now()
        if u["locked_until"] and u["locked_until"] > now.isoformat():
            return None, t("错误次数太多，请 {m} 分钟后再试", m=LOCK_MINUTES)
        if check_pin(pin, u["pin_hash"]):
            c.execute("UPDATE users SET failed_logins = 0, locked_until = NULL WHERE id = ?", (uid,))
            return dict(u), None
        fails = u["failed_logins"] + 1
        locked = (now + timedelta(minutes=LOCK_MINUTES)).isoformat() if fails >= MAX_FAILS else None
        c.execute("UPDATE users SET failed_logins = ?, locked_until = ? WHERE id = ?",
                  (0 if locked else fails, locked, uid))
        return None, t("PIN 不对") if not locked else t("错误次数太多，请 {m} 分钟后再试", m=LOCK_MINUTES)


def resolve(request: Request) -> Principal | None:
    s = get_settings()
    hdr = request.headers.get(s.trusted_user_header)
    if hdr:
        return Principal(id=None, name=hdr[:64], role="parent", source="olares")
    tok = request.cookies.get(COOKIE)
    if tok and (data := read_token(tok)):
        with db.tx() as c:
            u = c.execute("SELECT * FROM users WHERE id = ?", (data["uid"],)).fetchone()
        if u and u["active"] and u["session_version"] == data["sv"]:
            return Principal(id=u["id"], name=u["name"], role=u["role"], subjects=db.split(u["subjects"]))
    return None


class LoginRequired(Exception):
    pass


def require(request: Request, *roles: str) -> Principal:
    p = resolve(request)
    if p is None:
        raise LoginRequired()
    if roles and p.role not in roles:
        raise HTTPException(403, t("没有权限"))
    return p


def ensure_parent() -> None:
    """First run: create the parent account from INIT_PARENT_PIN (if set and no parent exists yet)."""
    s = get_settings()
    with db.tx() as c:
        if c.execute("SELECT 1 FROM users WHERE role = 'parent'").fetchone() or not s.init_parent_pin:
            return
        c.execute("INSERT INTO users(name, role, pin_hash, created_at) VALUES (?, 'parent', ?, ?)",
                  (s.init_parent_name, hash_pin(s.init_parent_pin), db.now_iso()))
