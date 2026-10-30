from datetime import timedelta

from conftest import jpeg, login

from cuoti import db


def make_mistake(client, family, subject="math", **extra):
    data = {"child_id": family["kid4"], "subject": subject, "topic": "分数加减", "causes": ["粗心"], **extra}
    r = client.post("/new", data=data, files=[("photos", ("q.jpg", jpeg(), "image/jpeg"))], follow_redirects=False)
    assert r.status_code == 303, r.text
    return int(r.headers["location"].split("/m/")[1].split("?")[0])


def set_due_today(mid):
    with db.tx() as c:
        c.execute("UPDATE mistakes SET due_date = ? WHERE id = ?", (db.today(), mid))


def test_login_lockout(client, family):
    for _ in range(5):
        r = client.post(f"/login/{family['kid4']}", data={"pin": "9999"}, follow_redirects=False)
    assert "%E9%94%99%E8%AF%AF%E6%AC%A1%E6%95%B0" in r.headers["location"]  # 错误次数
    r = client.post(f"/login/{family['kid4']}", data={"pin": "1111"}, follow_redirects=False)
    assert r.headers["location"] != "/"


def test_unauthenticated_redirects(client):
    assert client.get("/today", follow_redirects=False).headers["location"] == "/login"


def test_olares_owner_header_is_parent(client, family):
    r = client.get("/settings", headers={"X-Bfl-User": "owner"})
    assert r.status_code == 200 and "账号" in r.text


def test_tutor_records_and_ocr_fills_fields(client, family):
    login(client, family["math"], "333333")
    mid = make_mistake(client, family, topic="", causes=[])
    with db.tx() as c:
        m = c.execute("SELECT * FROM mistakes WHERE id = ?", (mid,)).fetchone()
        photo = c.execute("SELECT filename FROM photos WHERE mistake_id = ?", (mid,)).fetchone()["filename"]
    assert m["ai_status"] == "done" and m["question_text"].startswith("（示例识别）")
    assert m["topic"] == "分数加减" and m["causes"] == "概念不懂"
    assert m["due_date"] == (db.now().date() + timedelta(days=1)).isoformat()
    # photo was shrunk to 1600px
    from PIL import Image

    from cuoti.images import photo_path
    assert max(Image.open(photo_path(photo)).size) == 1600
    assert client.get(f"/photo/{photo}").status_code == 200


def test_ocr_never_overwrites_tutor_text(client, family):
    login(client, family["math"], "333333")
    mid = make_mistake(client, family, question_text="老师写的题", causes=["审题不清"])
    with db.tx() as c:
        m = c.execute("SELECT * FROM mistakes WHERE id = ?", (mid,)).fetchone()
    assert m["question_text"] == "老师写的题" and m["causes"] == "审题不清" and m["answer_text"]


def test_tutor_only_sees_own_subject(client, family):
    login(client, family["math"], "333333")
    mid = make_mistake(client, family)
    login(client, family["eng"], "444444")
    assert client.get(f"/m/{mid}").status_code == 403
    assert client.post("/new", data={"child_id": family["kid4"], "subject": "math", "question_text": "x"}).status_code == 403


def test_review_cycle_to_mastery_and_child_view(client, family):
    login(client, family["math"], "333333")
    mid = make_mistake(client, family)
    set_due_today(mid)
    # child sees it today, practices, then sees the answer; cannot test or edit
    login(client, family["kid4"], "1111")
    page = client.get("/me").text
    assert "我做完了" in page
    client.post(f"/m/{mid}/practice")
    assert "已练习" in client.get("/me").text
    assert client.post(f"/m/{mid}/test", data={"correct": 1}).status_code == 403
    assert client.post(f"/m/{mid}/status", data={"action": "mastered"}).status_code == 403
    # the other child cannot see it
    login(client, family["kid5"], "2222")
    assert client.get(f"/m/{mid}").status_code == 403
    # tutor tests three times correct -> mastered
    login(client, family["math"], "333333")
    assert "孩子已自己练过" in client.get("/today").text
    for _ in range(3):
        set_due_today(mid)
        client.post(f"/m/{mid}/test", data={"correct": 1})
    with db.tx() as c:
        m = c.execute("SELECT * FROM mistakes WHERE id = ?", (mid,)).fetchone()
        n = c.execute("SELECT COUNT(*) FROM reviews WHERE mistake_id = ? AND kind='test'", (mid,)).fetchone()[0]
    assert m["status"] == "mastered" and m["mastered_at"] and n == 3
    rep = client.get("/report").text
    assert "小明" in rep and "新掌握" in rep


def test_variants_need_approval(client, family):
    login(client, family["math"], "333333")
    mid = make_mistake(client, family)
    client.post(f"/m/{mid}/variants")
    with db.tx() as c:
        vs = c.execute("SELECT * FROM mistakes WHERE parent_id = ?", (mid,)).fetchall()
    assert len(vs) == 2 and all(v["approved"] == 0 for v in vs)
    # not visible to the child, not scheduled
    login(client, family["kid4"], "1111")
    assert client.get(f"/m/{vs[0]['id']}").status_code == 403
    login(client, family["math"], "333333")
    client.post(f"/v/{vs[0]['id']}/approve", data={"question_text": "改过的变式题", "answer_text": "答案"})
    client.post(f"/v/{vs[1]['id']}/reject")
    with db.tx() as c:
        vs = c.execute("SELECT * FROM mistakes WHERE parent_id = ?", (mid,)).fetchall()
    assert len(vs) == 1 and vs[0]["approved"] == 1 and vs[0]["question_text"] == "改过的变式题" and vs[0]["due_date"]


def test_no_ai_configured_degrades(client, family, monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "")
    from cuoti.config import get_settings
    get_settings.cache_clear()
    login(client, family["math"], "333333")
    assert "保存后 AI" not in client.get("/new").text
    mid = make_mistake(client, family)
    with db.tx() as c:
        assert c.execute("SELECT ai_status FROM mistakes WHERE id = ?", (mid,)).fetchone()[0] == ""
    assert "disabled" in client.get(f"/m/{mid}").text


def test_check_buttons_only_when_due(client, family):
    login(client, family["math"], "333333")
    mid = make_mistake(client, family)
    assert "✓ 做对了" not in client.get(f"/m/{mid}").text  # first check is tomorrow
    set_due_today(mid)
    assert "✓ 做对了" in client.get(f"/m/{mid}").text


def test_variants_do_not_copy_causes(client, family):
    login(client, family["math"], "333333")
    mid = make_mistake(client, family)
    client.post(f"/m/{mid}/variants")
    with db.tx() as c:
        assert {r[0] for r in c.execute("SELECT causes FROM mistakes WHERE parent_id = ?", (mid,))} == {""}


def test_task_without_child_is_a_message(client, family):
    login(client, family["math"], "333333")
    r = client.post("/tasks", data={"subject": "math", "title": "x", "due_date": db.today()}, follow_redirects=True)
    assert r.status_code == 200 and "请选择孩子" in r.text


def test_tasks_repeat_and_confirm(client, family):
    login(client, family["eng"], "444444")
    client.post("/tasks", data={"child_ids": [family["kid4"], family["kid5"]], "subject": "english",
                                "title": "读 20 分钟", "due_date": db.today(), "repeat": "daily"})
    login(client, family["kid4"], "1111")
    with db.tx() as c:
        tid = c.execute("SELECT id FROM tasks WHERE child_id = ?", (family["kid4"],)).fetchone()[0]
    assert "读 20 分钟" in client.get("/me").text
    client.post(f"/t/{tid}/done")
    assert client.post(f"/t/{tid}/confirm").status_code == 403
    with db.tx() as c:
        rows = c.execute("SELECT status, due_date FROM tasks WHERE child_id = ? ORDER BY id", (family["kid4"],)).fetchall()
    assert [r["status"] for r in rows] == ["done", "todo"]
    assert rows[1]["due_date"] == (db.now().date() + timedelta(days=1)).isoformat()
    login(client, family["eng"], "444444")
    assert "孩子说完成了" in client.get("/today").text
    client.post(f"/t/{tid}/confirm")
    with db.tx() as c:
        assert c.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == "confirmed"


def test_settings_users_and_pin_change_logs_out(client, family):
    login(client, family["parent"], "123456")
    r = client.post("/settings/users", data={"name": "弟弟", "role": "child", "pin": "12"}, follow_redirects=False)
    assert "PIN" in client.get(r.headers["location"]).text  # too short
    client.post("/settings/users", data={"uid": family["kid4"], "name": "小明", "role": "child", "grade": "4",
                                         "pin": "5555", "active": "1"})
    login(client, family["kid4"], "5555")
    # a cookie from before the PIN change no longer works
    login(client, family["parent"], "123456")
    old = client.cookies.get("cuoti_session")
    client.post("/settings/users", data={"uid": family["parent"], "name": "家长", "role": "parent", "pin": "654321",
                                         "active": "1"})
    client.cookies.set("cuoti_session", old)
    assert client.get("/settings", follow_redirects=False).status_code == 303


def test_print_and_book(client, family):
    login(client, family["math"], "333333")
    make_mistake(client, family)
    assert "分数加减" in client.get("/book").text
    assert "错题重做卷" in client.get("/book/print").text
    login(client, family["parent"], "123456")
    client.post("/settings/backup")
    assert "cuoti-" in client.get("/settings").text


def test_pending_ai_jobs_resume_after_restart(client, family):
    from cuoti import web
    login(client, family["math"], "333333")
    mid = make_mistake(client, family, topic="", causes=[])
    with db.tx() as c:  # as if the pod restarted before the background tasks ran
        c.execute("UPDATE mistakes SET ai_status='pending', question_text='', variant_status='pending' WHERE id=?",
                  (mid,))
    web.resume_ai_jobs()
    with db.tx() as c:
        m = c.execute("SELECT * FROM mistakes WHERE id = ?", (mid,)).fetchone()
        n = c.execute("SELECT COUNT(*) FROM mistakes WHERE parent_id = ? AND approved = 0", (mid,)).fetchone()[0]
    assert m["ai_status"] == "done" and m["question_text"] and m["variant_status"] == "" and n == 2
