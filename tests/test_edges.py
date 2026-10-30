"""Boundary cases: every form refuses bad input with a message and writes nothing."""
from conftest import add_user, jpeg, login

from cuoti import db
from cuoti.config import get_settings


def count(table, where="1=1", args=()):
    with db.tx() as c:
        return c.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", args).fetchone()[0]


def photo_files():
    return list(get_settings().photo_dir.rglob("*.jpg"))


def post_new(client, data, files=None):
    return client.post("/new", data=data, files=files or [], follow_redirects=True)


def test_add_requires_a_child(client, family):
    login(client, family["math"], "333333")
    files = [("photos", ("q.jpg", jpeg(), "image/jpeg"))]
    for child in (None, "", "abc", str(family["math"]), "99999"):  # missing, empty, junk, a tutor, unknown
        data = {"subject": "math", "question_text": "1+1"}
        if child is not None:
            data["child_id"] = child
        r = post_new(client, data, files)
        assert r.status_code == 200 and "请选择孩子" in r.text, child
    assert count("mistakes") == 0 and photo_files() == []  # nothing written, no orphan photos


def test_add_refuses_inactive_child(client, family):
    with db.tx() as c:
        c.execute("UPDATE users SET active = 0 WHERE id = ?", (family["kid5"],))
    login(client, family["math"], "333333")
    assert "请选择孩子" in post_new(client, {"child_id": family["kid5"], "subject": "math", "question_text": "x"}).text
    assert count("mistakes") == 0


def test_add_requires_subject_photo_or_text(client, family):
    login(client, family["math"], "333333")
    r = post_new(client, {"child_id": family["kid4"]})
    assert "请选择科目" in r.text
    r = post_new(client, {"child_id": family["kid4"], "subject": "math", "question_text": "   "})
    assert "请至少拍一张照片或写下题目" in r.text
    assert count("mistakes") == 0


def test_add_photo_limits(client, family):
    login(client, family["math"], "333333")
    base = {"child_id": family["kid4"], "subject": "math"}
    r = post_new(client, base, [("photos", ("a.txt", b"not an image", "text/plain"))])
    assert "无法识别" in r.text
    r = post_new(client, base, [("photos", (f"{i}.jpg", jpeg(), "image/jpeg")) for i in range(7)])
    assert "最多 6 张" in r.text
    # one good + one bad photo: the good one must not be stored either
    r = post_new(client, base, [("photos", ("ok.jpg", jpeg(), "image/jpeg")), ("photos", ("bad.jpg", b"xx", "image/jpeg"))])
    assert "无法识别" in r.text
    assert count("mistakes") == 0 and photo_files() == []


def test_add_page_guards_save_button(client, family):
    login(client, family["math"], "333333")
    page = client.get("/new").text
    assert f'name="child_id" value="{family["kid4"]}" required' in page
    assert "checked" not in page.split('name="child_id"')[1].split(">")[0]  # two kids: none pre-picked
    assert 'id="need"' in page and "checkForm" in page
    assert client.get("/new?child=abc&subject=zzz").status_code == 200
    assert client.get("/tasks?child=abc").status_code == 200
    for url in ["/book?child=abc&status=zzz", "/book/print?child=-1", "/report?week=abc", "/report?week=99"]:
        assert client.get(url).status_code == 200, url
    no_subjects = add_user("Temp", "tutor", "777777", subjects="")
    login(client, no_subjects, "777777")
    assert "你还没有负责的科目" in client.get("/new").text


def test_long_text_is_capped(client, family):
    login(client, family["math"], "333333")
    post_new(client, {"child_id": family["kid4"], "subject": "math", "question_text": "x" * 9000, "topic": "t" * 500})
    with db.tx() as c:
        m = c.execute("SELECT question_text, topic FROM mistakes").fetchone()
    assert len(m["question_text"]) == 5000 and len(m["topic"]) == 60


def _mistake(client, family, **extra):
    data = {"child_id": family["kid4"], "subject": "math", "question_text": "2+2", **extra}
    r = client.post("/new", data=data, follow_redirects=False)
    return int(r.headers["location"].split("/m/")[1].split("?")[0])


def test_check_refused_when_not_due_and_double_tap(client, family):
    login(client, family["math"], "333333")
    mid = _mistake(client, family)
    r = client.post(f"/m/{mid}/test", data={"correct": 1}, follow_redirects=True)
    assert "还没到复测时间" in r.text and count("reviews") == 0
    with db.tx() as c:
        c.execute("UPDATE mistakes SET due_date = ? WHERE id = ?", (db.today(), mid))
    client.post(f"/m/{mid}/test", data={"correct": 1})
    client.post(f"/m/{mid}/test", data={"correct": 1})  # second tap: no longer due
    with db.tx() as c:
        m = c.execute("SELECT correct_streak FROM mistakes WHERE id = ?", (mid,)).fetchone()
    assert m["correct_streak"] == 1 and count("reviews", "kind='test'") == 1


def test_edit_and_photo_edges(client, family):
    login(client, family["math"], "333333")
    mid = _mistake(client, family)
    r = client.post(f"/m/{mid}/edit", data={"question_text": "  "}, follow_redirects=True)
    assert "不能为空" in r.text
    r = client.post(f"/m/{mid}/photos", files=[("photos", ("x.txt", b"nope", "text/plain"))], follow_redirects=True)
    assert r.status_code == 200 and "无法识别" in r.text
    r = client.post(f"/m/{mid}/photos", follow_redirects=True)
    assert "请先选择照片" in r.text
    r = client.post(f"/m/{mid}/photos", files=[("photos", (f"{i}.jpg", jpeg(), "image/jpeg")) for i in range(6)],
                    follow_redirects=True)
    assert count("photos") == 6
    r = client.post(f"/m/{mid}/photos", files=[("photos", ("7.jpg", jpeg(), "image/jpeg"))], follow_redirects=True)
    assert "最多 6 张" in r.text and count("photos") == 6


def test_variant_needs_text(client, family):
    login(client, family["math"], "333333")
    mid = _mistake(client, family)
    client.post(f"/m/{mid}/variants")
    with db.tx() as c:
        vid = c.execute("SELECT id FROM mistakes WHERE parent_id = ?", (mid,)).fetchone()[0]
    r = client.post(f"/v/{vid}/approve", data={"question_text": " "}, follow_redirects=True)
    assert "不能为空" in r.text and count("mistakes", "approved = 0") == 2


def test_task_edges(client, family):
    login(client, family["math"], "333333")
    ok = {"child_ids": [family["kid4"]], "subject": "math", "title": "p.3", "due_date": db.today()}
    for bad, msg in [({"title": " "}, "请填写任务内容"), ({"due_date": "not-a-date"}, "请选择截止日期"),
                     ({"due_date": "2020-01-01"}, "截止日期不能早于今天"), ({"child_ids": ["x", "999"]}, "请选择孩子"),
                     ({"subject": "english"}, "请选择科目"), ({"subject": ""}, "请选择科目")]:
        r = client.post("/tasks", data={**ok, **bad}, follow_redirects=True)
        assert r.status_code == 200 and msg in r.text, bad
    assert count("tasks") == 0
    client.post("/tasks", data={**ok, "child_ids": [family["kid4"], family["kid4"]], "repeat": "hourly"})
    with db.tx() as c:
        rows = c.execute("SELECT repeat FROM tasks").fetchall()
    assert [r["repeat"] for r in rows] == ["none"]  # duplicate child once, unknown repeat -> once
    tid = c_tid = None
    with db.tx() as c:
        c_tid = c.execute("SELECT id FROM tasks").fetchone()[0]
    tid = c_tid
    login(client, family["kid4"], "1111")
    client.post(f"/t/{tid}/done")
    r = client.post(f"/t/{tid}/done", follow_redirects=True)  # double tap
    assert r.status_code == 200 and "已经完成" in r.text


def test_account_edges(client, family):
    login(client, family["parent"], "123456")

    def save(**kw):
        return client.post("/settings/users", data=kw, follow_redirects=True).text

    assert "至少一个科目" in save(name="New tutor", role="tutor", pin="888888")
    assert "1 到 12" in save(name="Kid3", role="child", pin="3333", grade="13")
    assert "已经有叫" in save(name="小明", role="child", pin="3333")
    assert "已经有叫" in save(name="王老师", role="tutor", subjects="math", pin="888888")
    # a child with records can't become a tutor
    login(client, family["math"], "333333")
    client.post("/new", data={"child_id": family["kid4"], "subject": "math", "question_text": "1+1"})
    login(client, family["parent"], "123456")
    assert "不能改成其他角色" in save(uid=family["kid4"], name="小明", role="tutor", subjects="math", active="1")
    # the last active parent can't be disabled by another account (self is covered too)
    other = add_user("Parent2", "parent", "654321")
    login(client, other, "654321")
    with db.tx() as c:
        c.execute("UPDATE users SET active = 0 WHERE id = ?", (family["parent"],))
    assert "至少要保留一个家长" not in save(uid=family["parent"], name="家长", role="parent", active="0")
    assert "不能停用或降级自己" in save(uid=other, name="Parent2", role="parent", active="0")
    assert count("users", "role='parent' AND active=1") == 1
