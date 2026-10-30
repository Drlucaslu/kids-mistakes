import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from cuoti import auth, db
from cuoti.config import get_settings


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("INIT_PARENT_PIN", "123456")
    monkeypatch.setenv("MODEL_PROVIDER", "mock")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


@pytest.fixture
def client(app_env):
    from cuoti import i18n
    from cuoti.web import app
    with TestClient(app) as c:
        i18n.set_lang("zh")  # most tests assert on the Chinese wording; test_i18n covers English
        yield c


def add_user(name, role, pin, subjects="", grade=None):
    with db.tx() as c:
        return c.execute("INSERT INTO users(name, role, subjects, grade, pin_hash, created_at) VALUES (?,?,?,?,?,?)",
                         (name, role, subjects, grade, auth.hash_pin(pin), db.now_iso())).lastrowid


def login(client, uid, pin):
    client.cookies.clear()
    r = client.post(f"/login/{uid}", data={"pin": pin}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/", r.headers.get("location")
    return client


def jpeg(color="white"):
    buf = io.BytesIO()
    Image.new("RGB", (2400, 1800), color).save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
def family(client):
    kid4 = add_user("小明", "child", "1111", grade=4)
    kid5 = add_user("小红", "child", "2222", grade=5)
    math = add_user("王老师", "tutor", "333333", subjects="math")
    eng = add_user("Amy", "tutor", "444444", subjects="english")
    with db.tx() as c:
        parent = c.execute("SELECT id FROM users WHERE role='parent'").fetchone()["id"]
    return {"kid4": kid4, "kid5": kid5, "math": math, "eng": eng, "parent": parent}
