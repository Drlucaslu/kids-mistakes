import re
import string
from pathlib import Path

from conftest import add_user, login

from cuoti import db, i18n

PKG = Path(__file__).parent.parent / "cuoti"
CJK = re.compile(r"[一-鿿]")


def _literals():
    pats = [r'\b_\(\s*"((?:[^"\\]|\\.)*)"', r"\b_\(\s*'([^']*)'", r'\bt\(\s*"((?:[^"\\]|\\.)*)"']
    for p in list(PKG.glob("templates/*.html")) + list(PKG.glob("*.py")):
        text = p.read_text()
        for pat in pats:
            for m in re.finditer(pat, text):
                yield p.name, m.group(1).replace('\\"', '"')


def _fields(s):
    return {f for _, f, _, _ in string.Formatter().parse(s) if f}


def test_every_ui_string_has_english():
    missing = sorted({(f, s) for f, s in _literals() if CJK.search(s) and s not in i18n.EN})
    assert not missing, missing
    bad = [k for k, v in i18n.EN.items() if _fields(k) != _fields(v)]
    assert not bad, bad


def test_english_is_default_and_parent_can_switch(app_env):
    from fastapi.testclient import TestClient

    from cuoti.web import app
    with TestClient(app) as c:
        assert i18n.lang() == "en"
        assert "Who are you?" in c.get("/login").text
        with db.tx() as conn:
            parent = conn.execute("SELECT id FROM users WHERE role='parent'").fetchone()["id"]
        kid = add_user("Kid", "child", "1111", grade=4)
        login(c, kid, "1111")
        assert c.post("/settings/lang", data={"lang": "zh"}).status_code == 403
        login(c, parent, "123456")
        assert "Interface language" in c.get("/settings").text
        c.post("/settings/lang", data={"lang": "zh"})
        assert i18n.lang() == "zh" and "界面语言" in c.get("/settings").text
        with db.tx() as conn:  # survives a restart
            assert db.kv_get(conn, "ui_lang") == "zh"
        c.post("/settings/lang", data={"lang": "en"})
        assert "Careless" in c.get("/new").text  # default causes follow the language


def test_english_pages_have_no_chinese(app_env):
    from fastapi.testclient import TestClient

    from cuoti.web import app
    with TestClient(app) as c:
        kid = add_user("Kid", "child", "1111", grade=4)
        tutor = add_user("Phil", "tutor", "555555", subjects="math,english,chinese")
        with db.tx() as conn:
            parent = conn.execute("SELECT id FROM users WHERE role='parent'").fetchone()["id"]
        login(c, tutor, "555555")
        r = c.post("/new", data={"child_id": kid, "subject": "math", "question_text": "3 + 4 = ?",
                                 "topic": "addition", "causes": ["Careless"]}, follow_redirects=False)
        mid = int(r.headers["location"].split("/m/")[1].split("?")[0])
        with db.tx() as conn:
            conn.execute("UPDATE mistakes SET due_date = ? WHERE id = ?", (db.today(), mid))
        c.post("/tasks", data={"child_ids": [kid], "subject": "math", "title": "Workbook p.3",
                               "due_date": db.today(), "repeat": "weekly"})
        pages = ["/today", "/new", "/book", "/book/print", "/tasks", "/report", f"/m/{mid}", "/nope"]
        for url in pages:
            body = c.get(url).text
            hit = CJK.search(body)
            assert not hit, (url, body[max(0, hit.start() - 80): hit.start() + 40])
        r = c.post(f"/m/{mid}/test", data={"correct": 1}, follow_redirects=True)
        assert "Correct. Next review" in r.text and not CJK.search(r.text)
        login(c, kid, "1111")
        for url in ["/me", "/book", f"/m/{mid}"]:
            assert not CJK.search(c.get(url).text), url
        login(c, parent, "123456")
        body = c.get("/settings").text
        hit = CJK.search(body.replace("中文", ""))  # the language picker names Chinese in Chinese
        assert not hit, body[max(0, hit.start() - 80): hit.start() + 40]
        assert "Who are you?" in c.get("/login").text
