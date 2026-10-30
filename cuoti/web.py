"""Pages. Server-rendered, mobile first: forms post and redirect back."""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__, ai, auth, backup, db, i18n, images, report, schedule
from .auth import LoginRequired, Principal
from .i18n import t as _

log = logging.getLogger("cuoti")
HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    i18n.load()
    auth.ensure_parent()
    threading.Thread(target=backup.loop, daemon=True).start()
    threading.Thread(target=resume_ai_jobs, daemon=True).start()
    yield


def resume_ai_jobs() -> None:
    """AI jobs run as background tasks; a restart (deploy, reboot) drops them. Pick them up again."""
    with db.tx() as c:
        ocr = [r["id"] for r in c.execute("SELECT id FROM mistakes WHERE ai_status = 'pending' ORDER BY id")]
        var = c.execute("SELECT id, created_by FROM mistakes WHERE variant_status = 'pending' ORDER BY id").fetchall()
    for mid in ocr:
        run_ocr(mid)
    for r in var:
        run_variants(r["id"], r["created_by"])


app = FastAPI(title="错题本", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


@app.exception_handler(LoginRequired)
def _login_required(request: Request, exc: LoginRequired):
    return RedirectResponse("/login", 303)


@app.exception_handler(StarletteHTTPException)
def _http_error(request: Request, exc: StarletteHTTPException):
    text = exc.detail if isinstance(exc.detail, str) and exc.status_code != 404 else _("页面不存在")
    return templates.TemplateResponse(request, "error.html", {"me": auth.resolve(request), "msg": "", "text": text},
                                      status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
def _form_error(request: Request, exc: RequestValidationError):
    return templates.TemplateResponse(request, "error.html", {"me": auth.resolve(request), "msg": "",
                                      "text": _("表单没有填完整，请返回检查")}, status_code=400)


# ------------------------------------------------------------------------------------------ helpers
def subject_name(s: str) -> str:
    return _(db.SUBJECTS.get(s, s))


def role_name(r: str) -> str:
    return _({"child": "孩子", "tutor": "老师", "parent": "家长"}.get(r, r))


def due_label(d: str | None) -> str:
    if not d:
        return ""
    n = (date.fromisoformat(d) - db.now().date()).days
    if n < 0:
        return _("逾期 {n} 天", n=-n)
    return {0: _("今天"), 1: _("明天")}.get(n) or _("{n} 天后", n=n)


templates.env.filters["subject"] = subject_name
templates.env.filters["due"] = due_label
templates.env.filters["split"] = db.split
templates.env.globals["SUBJECTS"] = db.SUBJECTS
templates.env.globals["version"] = __version__
templates.env.globals["_"] = _
templates.env.globals["lang"] = i18n.lang
templates.env.globals["LANGS"] = i18n.LANGS
templates.env.globals["role_name"] = role_name


def render(request: Request, name: str, p: Principal | None, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, {"me": p, "msg": request.query_params.get("msg", ""), **ctx})


def back(url: str, msg: str = "") -> RedirectResponse:
    if msg:
        url += ("&" if "?" in url else "?") + "msg=" + quote(msg)
    return RedirectResponse(url, 303)


LIMITS = {"topic": 60, "question_text": 5000, "child_answer": 1000, "answer_text": 5000, "note": 2000,
          "title": 200, "detail": 1000, "name": 40}


def clip(field: str, value: str) -> str:
    """Trim and cap free text so one pasted essay cannot bloat the database or break a page."""
    return value.strip()[: LIMITS[field]]


def active_child(c: sqlite3.Connection, raw: str | int | None) -> sqlite3.Row | None:
    try:
        cid = int(raw or 0)
    except ValueError:
        return None
    return c.execute("SELECT * FROM users WHERE id = ? AND role = 'child' AND active = 1", (cid,)).fetchone()


async def read_photos(photos: list[UploadFile]) -> list[bytes]:
    """The uploaded images, checked (count, size, decodable) before anything is written."""
    datas = [d for f in photos if (d := await f.read())]
    images.check_all(datas)
    return datas


def children(c: sqlite3.Connection) -> list[sqlite3.Row]:
    return c.execute("SELECT * FROM users WHERE role = 'child' AND active = 1 ORDER BY grade, name").fetchall()


def get_mistake(c: sqlite3.Connection, mid: int, p: Principal, write: bool = False) -> sqlite3.Row:
    m = c.execute("SELECT * FROM mistakes WHERE id = ?", (mid,)).fetchone()
    if not m:
        raise HTTPException(404, _("找不到这道题"))
    if p.role == "child":
        if write or m["child_id"] != p.id or not m["approved"]:
            raise HTTPException(403, _("没有权限"))
    elif not p.can_subject(m["subject"]):
        raise HTTPException(403, _("这不是你负责的科目"))
    return m


def photos_of(c: sqlite3.Connection, mid: int) -> list[str]:
    return [r["filename"] for r in c.execute("SELECT filename FROM photos WHERE mistake_id = ? ORDER BY id", (mid,))]


def photo_bytes(c: sqlite3.Connection, mid: int) -> list[bytes]:
    return [images.photo_path(f).read_bytes() for f in photos_of(c, mid)]


def subject_filter(p: Principal, col: str = "m.subject") -> tuple[str, list]:
    subs = p.visible_subjects()
    return f"{col} IN ({','.join('?' * len(subs))})", subs


def due_items(c: sqlite3.Connection, p: Principal, child_id: int | None = None) -> list[dict]:
    where, args = subject_filter(p)
    sql = f"""SELECT m.*, u.name child_name FROM mistakes m JOIN users u ON u.id = m.child_id
              WHERE {where} AND m.approved = 1 AND m.status IN ('active','mastered')
              AND m.due_date IS NOT NULL AND m.due_date <= ?"""
    args.append(db.today())
    if child_id:
        sql += " AND m.child_id = ?"
        args.append(child_id)
    rows = c.execute(sql + " ORDER BY u.grade, m.due_date, m.id", args).fetchall()
    out = []
    for m in rows:
        d = dict(m)
        d["photos"] = photos_of(c, m["id"])
        last = c.execute("SELECT created_at FROM reviews WHERE mistake_id = ? AND kind = 'practice' "
                         "ORDER BY id DESC LIMIT 1", (m["id"],)).fetchone()
        last_test = c.execute("SELECT created_at FROM reviews WHERE mistake_id = ? AND kind = 'test' "
                              "ORDER BY id DESC LIMIT 1", (m["id"],)).fetchone()
        d["practiced"] = bool(last and (not last_test or last["created_at"] > last_test["created_at"]))
        d["practiced_today"] = bool(last and last["created_at"][:10] == db.today())
        out.append(d)
    return out


# ------------------------------------------------------------------------------------------ health / auth
@app.get("/health/live")
def live() -> dict:
    return {"ok": True, "version": __version__}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    with db.tx() as c:
        users = c.execute("SELECT id, name, role FROM users WHERE active = 1 ORDER BY "
                          "CASE role WHEN 'child' THEN 0 WHEN 'tutor' THEN 1 ELSE 2 END, name").fetchall()
    return render(request, "login.html", None, users=users)


@app.get("/login/{uid}", response_class=HTMLResponse)
def pin_page(request: Request, uid: int):
    with db.tx() as c:
        u = c.execute("SELECT id, name, role FROM users WHERE id = ? AND active = 1", (uid,)).fetchone()
    if not u:
        return back("/login")
    return render(request, "pin.html", None, u=u, err=request.query_params.get("err", ""))


@app.post("/login/{uid}")
def do_login(request: Request, uid: int, pin: str = Form("")):
    if not pin.strip():
        return RedirectResponse(f"/login/{uid}?err={quote(_('请输入 PIN'))}", 303)
    u, err = auth.login(uid, pin.strip())
    if err:
        return RedirectResponse(f"/login/{uid}?err={quote(err)}", 303)
    resp = RedirectResponse("/", 303)
    auth.set_cookie(resp, auth.make_token(u["id"], u["session_version"]), request)
    return resp


@app.post("/logout")
def logout():
    resp = RedirectResponse("/login", 303)
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


@app.get("/")
def home(request: Request):
    p = auth.require(request)
    return RedirectResponse({"child": "/me", "tutor": "/today"}.get(p.role, "/today"), 303)


# ------------------------------------------------------------------------------------------ tutor: today
@app.get("/today", response_class=HTMLResponse)
def today_page(request: Request):
    p = auth.require(request, "parent", "tutor")
    with db.tx() as c:
        items = due_items(c, p)
        where, args = subject_filter(p)
        pending_variants = c.execute(
            f"""SELECT m.*, u.name child_name FROM mistakes m JOIN users u ON u.id = m.child_id
                WHERE {where} AND m.approved = 0 ORDER BY m.parent_id, m.id""", args).fetchall()
        where_t, args_t = subject_filter(p, "t.subject")
        tasks_done = c.execute(
            f"""SELECT t.*, u.name child_name FROM tasks t JOIN users u ON u.id = t.child_id
                WHERE {where_t} AND t.status = 'done' ORDER BY t.done_at""", args_t).fetchall()
        tasks_overdue = c.execute(
            f"""SELECT t.*, u.name child_name FROM tasks t JOIN users u ON u.id = t.child_id
                WHERE {where_t} AND t.status = 'todo' AND t.due_date < ? ORDER BY t.due_date""",
            [*args_t, db.today()]).fetchall()
        kids = children(c)
    return render(request, "today.html", p, items=items, pending_variants=pending_variants,
                  tasks_done=tasks_done, tasks_overdue=tasks_overdue, kids=kids)


# ------------------------------------------------------------------------------------------ mistakes
@app.get("/new", response_class=HTMLResponse)
def new_page(request: Request, child: str = "", subject: str = ""):
    p = auth.require(request, "parent", "tutor")
    subs = p.visible_subjects()
    subject = subject if subject in subs else subs[0] if subs else None
    with db.tx() as c:
        kids = children(c)
        topic_map = {s: db.topics(c, s)[:30] for s in subs}
        cause_list = db.causes(c)
    return render(request, "new.html", p, kids=kids, subs=subs, subject=subject,
                  child=int(child) if child.isdigit() else (kids[0]["id"] if len(kids) == 1 else None),
                  topic_map=topic_map, cause_list=cause_list,
                  ai_ready=ai.config().ready)


@app.post("/new")
async def create_mistake(request: Request, background: BackgroundTasks,
                         child_id: str = Form(""), subject: str = Form(""), topic: str = Form(""),
                         causes: list[str] = Form([]), question_text: str = Form(""),
                         child_answer: str = Form(""), answer_text: str = Form(""), note: str = Form(""),
                         photos: list[UploadFile] = File([]), next: str = Form("")):
    p = auth.require(request, "parent", "tutor")
    retry = "/new?" + urlencode({k: v for k, v in (("child", child_id), ("subject", subject)) if v})
    if subject not in db.SUBJECTS:
        return back(retry, _("请选择科目"))
    if not p.can_subject(subject):
        raise HTTPException(403, _("这不是你负责的科目"))
    with db.tx() as c:
        child = active_child(c, child_id)
    if not child:
        return back(retry, _("请选择孩子"))
    try:
        datas = await read_photos(photos)
    except images.PhotoError as e:
        return back(retry, str(e))
    if not datas and not question_text.strip():
        return back(retry, _("请至少拍一张照片或写下题目"))
    saved = [images.save_photo(d) for d in datas]
    use_ai = bool(saved) and ai.config().ready
    with db.tx() as c:
        cur = c.execute(
            """INSERT INTO mistakes(child_id, subject, topic, causes, question_text, child_answer, answer_text, note,
                 due_date, ai_status, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (child["id"], subject, clip("topic", topic), ",".join(dict.fromkeys(c[:40] for c in causes if c))[:400],
             clip("question_text", question_text), clip("child_answer", child_answer),
             clip("answer_text", answer_text), clip("note", note), schedule.first_due(db.now().date()),
             "pending" if use_ai else "", p.id, db.now_iso()))
        mid = cur.lastrowid
        for rel in saved:
            c.execute("INSERT INTO photos(mistake_id, filename, created_at) VALUES (?,?,?)", (mid, rel, db.now_iso()))
    if use_ai:
        background.add_task(run_ocr, mid)
    if next == "again":
        return back(f"/new?child={child['id']}&subject={subject}", _("已保存，继续录下一道"))
    return back(f"/m/{mid}", _("已保存") + (_("，AI 正在识别题目…") if use_ai else ""))


def run_ocr(mid: int) -> None:
    """Fill empty fields from the photo. Never overwrites what the tutor typed."""
    with db.tx() as c:
        m = c.execute("SELECT m.*, u.grade FROM mistakes m JOIN users u ON u.id = m.child_id WHERE m.id = ?",
                      (mid,)).fetchone()
        imgs = photo_bytes(c, mid)
        topic_list, cause_list = db.topics(c, m["subject"]), db.causes(c)
    try:
        out = ai.read_question(imgs, m["subject"], m["grade"], topic_list, cause_list)
    except Exception as e:  # noqa: BLE001
        log.warning("OCR failed for mistake %s: %s", mid, e)
        with db.tx() as c:
            c.execute("UPDATE mistakes SET ai_status = 'failed', ai_error = ? WHERE id = ?", (str(e)[:200], mid))
        return
    with db.tx() as c:
        m = c.execute("SELECT * FROM mistakes WHERE id = ?", (mid,)).fetchone()
        fields = {
            "question_text": m["question_text"] or out.get("question_text", ""),
            "child_answer": m["child_answer"] or out.get("child_answer", ""),
            "answer_text": m["answer_text"] or out.get("correct_answer", ""),
            "topic": m["topic"] or out.get("topic", ""),
            "causes": m["causes"] or ",".join(out.get("causes", [])),
        }
        c.execute("""UPDATE mistakes SET question_text=?, child_answer=?, answer_text=?, topic=?, causes=?,
                     ai_status='done', ai_error='' WHERE id=?""", (*fields.values(), mid))


@app.get("/m/{mid}", response_class=HTMLResponse)
def mistake_page(request: Request, mid: int):
    p = auth.require(request)
    with db.tx() as c:
        m = get_mistake(c, mid, p)
        child = c.execute("SELECT * FROM users WHERE id = ?", (m["child_id"],)).fetchone()
        reviews = c.execute("""SELECT r.*, u.name by_name FROM reviews r LEFT JOIN users u ON u.id = r.by_user
                               WHERE r.mistake_id = ? ORDER BY r.id DESC""", (mid,)).fetchall()
        variants = c.execute("SELECT * FROM mistakes WHERE parent_id = ? ORDER BY id", (mid,)).fetchall()
        if p.role == "child":
            variants = [v for v in variants if v["approved"]]
        parent = c.execute("SELECT id, topic FROM mistakes WHERE id = ?", (m["parent_id"],)).fetchone() \
            if m["parent_id"] else None
        topic_list, cause_list = db.topics(c, m["subject"])[:30], db.causes(c)
        photos = photos_of(c, mid)
    return render(request, "mistake.html", p, m=m, child=child, reviews=reviews, variants=variants,
                  parent=parent, photos=photos, topic_list=topic_list, cause_list=cause_list,
                  ai_ready=ai.config().ready, streak_needed=schedule.MASTERY_STREAK, today=db.today())


@app.post("/m/{mid}/edit")
def edit_mistake(request: Request, mid: int, topic: str = Form(""), causes: list[str] = Form([]),
                 question_text: str = Form(""), child_answer: str = Form(""), answer_text: str = Form(""),
                 note: str = Form("")):
    p = auth.require(request, "parent", "tutor")
    with db.tx() as c:
        get_mistake(c, mid, p, write=True)
        if not question_text.strip() and not photos_of(c, mid):
            return back(f"/m/{mid}", _("没有照片时，题目文字不能为空"))
        c.execute("""UPDATE mistakes SET topic=?, causes=?, question_text=?, child_answer=?, answer_text=?, note=?
                     WHERE id=?""", (clip("topic", topic), ",".join(dict.fromkeys(x[:40] for x in causes if x))[:400],
                                     clip("question_text", question_text), clip("child_answer", child_answer),
                                     clip("answer_text", answer_text), clip("note", note), mid))
    return back(f"/m/{mid}", _("已更新"))


@app.post("/m/{mid}/photos")
async def add_photos(request: Request, mid: int, photos: list[UploadFile] = File([])):
    p = auth.require(request, "parent", "tutor")
    with db.tx() as c:
        get_mistake(c, mid, p, write=True)
        have = len(photos_of(c, mid))
    try:
        datas = await read_photos(photos)
    except images.PhotoError as e:
        return back(f"/m/{mid}", str(e))
    if not datas:
        return back(f"/m/{mid}", _("请先选择照片"))
    if have + len(datas) > images.MAX_PHOTOS:
        return back(f"/m/{mid}", _("每道题最多 {n} 张照片", n=images.MAX_PHOTOS))
    saved = [images.save_photo(d) for d in datas]
    with db.tx() as c:
        for rel in saved:
            c.execute("INSERT INTO photos(mistake_id, filename, created_at) VALUES (?,?,?)", (mid, rel, db.now_iso()))
    return back(f"/m/{mid}", _("已添加 {n} 张照片", n=len(saved)))


@app.post("/m/{mid}/ocr")
def redo_ocr(request: Request, mid: int, background: BackgroundTasks):
    p = auth.require(request, "parent", "tutor")
    with db.tx() as c:
        get_mistake(c, mid, p, write=True)
        if not photos_of(c, mid):
            return back(f"/m/{mid}", _("没有照片可识别"))
        c.execute("UPDATE mistakes SET ai_status = 'pending', ai_error = '' WHERE id = ?", (mid,))
    background.add_task(run_ocr, mid)
    return back(f"/m/{mid}", _("AI 正在识别（只会填写空着的栏目）…"))


@app.post("/m/{mid}/test")
def test_mistake(request: Request, mid: int, correct: int = Form(...), note: str = Form(""),
                 next: str = Form("")):
    p = auth.require(request, "parent", "tutor")
    with db.tx() as c:
        m = get_mistake(c, mid, p, write=True)
        if not m["approved"] or m["status"] == "archived":
            raise HTTPException(400, _("这道题不在复习中"))
        if not m["due_date"] or m["due_date"] > db.today():
            return back(next if next.startswith("/") else f"/m/{mid}",
                        _("还没到复测时间（下次：{d}）", d=due_label(m["due_date"])) if m["due_date"]
                        else _("这道题已完成复习"))
        st = schedule.after_test(schedule.State(m["status"], m["stage"], m["correct_streak"], m["due_date"]),
                                 bool(correct), db.now().date())
        mastered_at = db.now_iso() if st.status == "mastered" and m["status"] != "mastered" else \
            (m["mastered_at"] if st.status == "mastered" else None)
        c.execute("""UPDATE mistakes SET status=?, stage=?, correct_streak=?, due_date=?, mastered_at=? WHERE id=?""",
                  (st.status, st.stage, st.correct_streak, st.due_date, mastered_at, mid))
        c.execute("""INSERT INTO reviews(mistake_id, kind, correct, due_date, note, by_user, created_at)
                     VALUES (?, 'test', ?, ?, ?, ?, ?)""", (mid, int(bool(correct)), m["due_date"], note.strip(),
                                                            p.id, db.now_iso()))
    if st.status == "mastered" and m["status"] != "mastered":
        msg = _("🎉 连续答对 3 次，已掌握！30 天后再抽查一次")
    elif correct:
        msg = _("✓ 答对，下次复习：{d}", d=due_label(st.due_date)) if st.due_date else _("✓ 抽查通过")
    else:
        msg = _("✗ 答错，明天再复习一次。可以生成变式题帮孩子巩固")
    return back(next if next.startswith("/") else f"/m/{mid}", msg)


@app.post("/m/{mid}/practice")
def practice(request: Request, mid: int):
    p = auth.require(request, "child")
    with db.tx() as c:
        get_mistake(c, mid, p)
        c.execute("INSERT INTO reviews(mistake_id, kind, by_user, created_at) VALUES (?, 'practice', ?, ?)",
                  (mid, p.id, db.now_iso()))
    return back("/me", _("做得好！答案已经可以看了，老师下次会检查"))


@app.post("/m/{mid}/status")
def set_status(request: Request, mid: int, action: str = Form(...)):
    p = auth.require(request, "parent", "tutor")
    with db.tx() as c:
        m = get_mistake(c, mid, p, write=True)
        if action == "mastered":
            c.execute("UPDATE mistakes SET status='mastered', due_date=?, mastered_at=? WHERE id=?",
                      ((db.now().date() + timedelta(days=schedule.SPOT_CHECK_DAYS)).isoformat(), db.now_iso(), mid))
        elif action == "reopen":
            c.execute("UPDATE mistakes SET status='active', stage=0, correct_streak=0, due_date=?, mastered_at=NULL "
                      "WHERE id=?", (schedule.first_due(db.now().date()), mid))
        elif action == "archive":
            c.execute("UPDATE mistakes SET status='archived', due_date=NULL WHERE id=?", (mid,))
        elif action == "delete" and (p.is_parent or m["created_by"] == p.id):
            files = photos_of(c, mid)
            c.execute("DELETE FROM mistakes WHERE parent_id = ? AND approved = 0", (mid,))
            c.execute("UPDATE mistakes SET parent_id = NULL WHERE parent_id = ?", (mid,))
            c.execute("DELETE FROM mistakes WHERE id = ?", (mid,))
            for f in files:
                images.photo_path(f).unlink(missing_ok=True)
            return back("/book", _("已删除"))
        else:
            raise HTTPException(400, _("未知操作"))
    return back(f"/m/{mid}", _({"mastered": "已标为掌握", "reopen": "已重新开始复习", "archive": "已归档"}[action]))


# ------------------------------------------------------------------------------------------ variants
@app.post("/m/{mid}/variants")
def gen_variants(request: Request, mid: int, background: BackgroundTasks):
    p = auth.require(request, "parent", "tutor")
    with db.tx() as c:
        m = get_mistake(c, mid, p, write=True)
        if m["variant_status"] == "pending":
            return back(f"/m/{mid}", _("正在生成中，稍等片刻刷新"))
        c.execute("UPDATE mistakes SET variant_status = 'pending' WHERE id = ?", (mid,))
    background.add_task(run_variants, mid, p.id)
    return back(f"/m/{mid}", _("AI 正在出变式题，约半分钟后刷新页面"))


def run_variants(mid: int, by: int | None) -> None:
    with db.tx() as c:
        m = c.execute("SELECT m.*, u.grade FROM mistakes m JOIN users u ON u.id = m.child_id WHERE m.id = ?",
                      (mid,)).fetchone()
        imgs = photo_bytes(c, mid)
    try:
        vs = ai.make_variants(m["subject"], m["grade"], m["topic"], m["question_text"], m["answer_text"], imgs)
        if not vs:
            raise ai.AIError(_("模型没有给出题目"))
    except Exception as e:  # noqa: BLE001
        log.warning("variants failed for mistake %s: %s", mid, e)
        with db.tx() as c:
            c.execute("UPDATE mistakes SET variant_status = ? WHERE id = ?", (f"failed:{str(e)[:150]}", mid))
        return
    with db.tx() as c:
        for v in vs:
            c.execute("""INSERT INTO mistakes(child_id, subject, topic, causes, question_text, answer_text, parent_id,
                           source, approved, created_by, created_at) VALUES (?,?,?,?,?,?,?, 'ai_variant', 0, ?, ?)""",
                      (m["child_id"], m["subject"], m["topic"], "", v["question"].strip(),
                       v["answer"].strip(), mid, by, db.now_iso()))
        c.execute("UPDATE mistakes SET variant_status = '' WHERE id = ?", (mid,))


@app.post("/v/{vid}/approve")
def approve_variant(request: Request, vid: int, question_text: str = Form(""), answer_text: str = Form(""),
                    next: str = Form("")):
    p = auth.require(request, "parent", "tutor")
    with db.tx() as c:
        v = get_mistake(c, vid, p, write=True)
        if v["approved"]:
            return back(f"/m/{v['parent_id']}")
        if not question_text.strip():
            return back(next if next.startswith("/") else f"/m/{v['parent_id']}", _("变式题的题目不能为空"))
        c.execute("UPDATE mistakes SET approved=1, question_text=?, answer_text=?, due_date=?, created_at=? WHERE id=?",
                  (clip("question_text", question_text), clip("answer_text", answer_text), schedule.first_due(db.now().date()), db.now_iso(), vid))
    return back(next if next.startswith("/") else f"/m/{v['parent_id']}", _("变式题已加入复习（明天出现）"))


@app.post("/v/{vid}/reject")
def reject_variant(request: Request, vid: int, next: str = Form("")):
    p = auth.require(request, "parent", "tutor")
    with db.tx() as c:
        v = get_mistake(c, vid, p, write=True)
        if v["approved"]:
            raise HTTPException(400, _("已采用的变式题请在题目页归档"))
        c.execute("DELETE FROM mistakes WHERE id = ?", (vid,))
    return back(next if next.startswith("/") else f"/m/{v['parent_id']}", _("已丢弃"))


# ------------------------------------------------------------------------------------------ book
def _book_query(p: Principal, q: dict) -> tuple[str, list]:
    where, args = subject_filter(p)
    sql = f"""SELECT m.*, u.name child_name FROM mistakes m JOIN users u ON u.id = m.child_id
              WHERE {where} AND m.approved = 1"""
    if p.role == "child":
        sql += " AND m.child_id = ?"
        args.append(p.id)
    elif q.get("child", "").isdigit():
        sql += " AND m.child_id = ?"
        args.append(int(q["child"]))
    if q.get("subject"):
        sql += " AND m.subject = ?"
        args.append(q["subject"])
    status = q.get("status", "active")
    if status != "all":
        sql += " AND m.status = ?"
        args.append(status)
    if q.get("topic"):
        sql += " AND m.topic = ?"
        args.append(q["topic"])
    if q.get("cause"):
        sql += " AND (',' || m.causes || ',') LIKE ?"
        args.append(f"%,{q['cause']},%")
    return sql + " ORDER BY m.created_at DESC LIMIT 300", args


@app.get("/book", response_class=HTMLResponse)
def book(request: Request):
    p = auth.require(request)
    q = dict(request.query_params)
    q.setdefault("status", "active")
    with db.tx() as c:
        sql, args = _book_query(p, q)
        rows = [dict(r, photos=photos_of(c, r["id"])) for r in c.execute(sql, args).fetchall()]
        kids = children(c) if p.role != "child" else []
        subs = p.visible_subjects()
        topic_opts = sorted({t for s in subs for t in db.topics(c, s)})
        cause_list = db.causes(c)
    qs = urlencode({k: v for k, v in q.items() if v and k != "msg"})
    return render(request, "book.html", p, rows=rows, q=q, kids=kids, subs=subs, topic_opts=topic_opts,
                  cause_list=cause_list, qs=qs)


@app.get("/book/print", response_class=HTMLResponse)
def book_print(request: Request):
    p = auth.require(request)
    q = dict(request.query_params)
    q.setdefault("status", "active")
    with db.tx() as c:
        sql, args = _book_query(p, q)
        rows = [dict(r, photos=photos_of(c, r["id"])) for r in c.execute(sql, args).fetchall()]
    return render(request, "print.html", p, rows=rows, today=db.today())


@app.get("/photo/{rel:path}")
def photo(request: Request, rel: str):
    p = auth.require(request)
    with db.tx() as c:
        row = c.execute("SELECT mistake_id FROM photos WHERE filename = ?", (rel,)).fetchone()
        if not row:
            raise HTTPException(404)
        m = get_mistake(c, row["mistake_id"], p)
        if p.role == "child" and m["child_id"] != p.id:
            raise HTTPException(403)
    return FileResponse(images.photo_path(rel), media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400"})


# ------------------------------------------------------------------------------------------ child
@app.get("/me", response_class=HTMLResponse)
def me_page(request: Request):
    p = auth.require(request, "child")
    with db.tx() as c:
        items = due_items(c, p, child_id=p.id)
        tasks = c.execute("""SELECT * FROM tasks WHERE child_id = ? AND (
                               (status = 'todo' AND due_date <= ?) OR (status IN ('done','confirmed') AND done_at >= ?))
                             ORDER BY status != 'todo', due_date""",
                          (p.id, (db.now().date() + timedelta(days=7)).isoformat(), db.today())).fetchall()
        stats = c.execute("""SELECT SUM(status='active') open, SUM(status='mastered') mastered,
                             SUM(status='mastered' AND mastered_at >= ?) week FROM mistakes
                             WHERE child_id = ? AND approved = 1""",
                          (report.week_bounds()[0].isoformat(), p.id)).fetchone()
    return render(request, "me.html", p, items=items, tasks=tasks, stats=stats, today=db.today())


# ------------------------------------------------------------------------------------------ tasks
@app.get("/tasks", response_class=HTMLResponse)
def tasks_page(request: Request, child: str = ""):
    p = auth.require(request, "parent", "tutor")
    child = int(child) if child.isdigit() else None
    where, args = subject_filter(p, "t.subject")
    sql = f"""SELECT t.*, u.name child_name FROM tasks t JOIN users u ON u.id = t.child_id
              WHERE {where} AND (t.status IN ('todo','done') OR t.confirmed_at >= ?)"""
    args.append((db.now().date() - timedelta(days=7)).isoformat())
    if child:
        sql += " AND t.child_id = ?"
        args.append(child)
    with db.tx() as c:
        rows = c.execute(sql + " ORDER BY t.status = 'confirmed', t.due_date, t.id", args).fetchall()
        kids = children(c)
    return render(request, "tasks.html", p, rows=rows, kids=kids, child=child, subs=p.visible_subjects(),
                  today=db.today())


@app.post("/tasks")
def create_task(request: Request, child_ids: list[str] = Form([]), subject: str = Form(""),
                title: str = Form(""), detail: str = Form(""), due_date: str = Form(""), repeat: str = Form("none")):
    p = auth.require(request, "parent", "tutor")
    if subject not in db.SUBJECTS or not p.can_subject(subject):
        return back("/tasks", _("请选择科目"))
    if not title.strip():
        return back("/tasks", _("请填写任务内容"))
    if repeat not in ("none", "daily", "weekly"):
        repeat = "none"
    try:
        due = date.fromisoformat(due_date)
    except ValueError:
        return back("/tasks", _("请选择截止日期"))
    if due < db.now().date():
        return back("/tasks", _("截止日期不能早于今天"))
    with db.tx() as c:
        kids = [k for cid in dict.fromkeys(child_ids) if (k := active_child(c, cid))]
        if not kids:
            return back("/tasks", _("请选择孩子"))
        for k in kids:
            c.execute("""INSERT INTO tasks(child_id, subject, title, detail, due_date, repeat, created_by, created_at)
                         VALUES (?,?,?,?,?,?,?,?)""", (k["id"], subject, clip("title", title), clip("detail", detail),
                                                        due.isoformat(), repeat, p.id, db.now_iso()))
    return back("/tasks", _("任务已布置"))


def _task(c: sqlite3.Connection, tid: int, p: Principal) -> sqlite3.Row:
    t = c.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
    if not t:
        raise HTTPException(404)
    if (p.role == "child" and t["child_id"] != p.id) or (p.role == "tutor" and not p.can_subject(t["subject"])):
        raise HTTPException(403, _("没有权限"))
    return t


@app.post("/t/{tid}/{action}")
def task_action(request: Request, tid: int, action: str, next: str = Form("")):
    p = auth.require(request)
    with db.tx() as c:
        t = _task(c, tid, p)
        if action == "done" and t["status"] == "todo":
            c.execute("UPDATE tasks SET status='done', done_at=? WHERE id=?", (db.now_iso(), tid))
            if t["repeat"] != "none":
                step = 1 if t["repeat"] == "daily" else 7
                nxt = max(date.fromisoformat(t["due_date"]) + timedelta(days=step),
                          db.now().date() + timedelta(days=1))
                c.execute("""INSERT INTO tasks(child_id, subject, title, detail, due_date, repeat, created_by, created_at)
                             VALUES (?,?,?,?,?,?,?,?)""", (t["child_id"], t["subject"], t["title"], t["detail"],
                                                            nxt.isoformat(), t["repeat"], t["created_by"], db.now_iso()))
            msg = _("完成啦 👍")
        elif action == "done":
            msg = _("这个任务已经完成了")
        elif not p.is_staff:
            raise HTTPException(403, _("没有权限"))
        elif action == "confirm" and t["status"] in ("todo", "done"):
            c.execute("UPDATE tasks SET status='confirmed', confirmed_at=?, done_at=COALESCE(done_at, ?) WHERE id=?",
                      (db.now_iso(), db.now_iso(), tid))
            msg = _("已确认")
        elif action == "reopen":
            c.execute("UPDATE tasks SET status='todo', done_at=NULL, confirmed_at=NULL WHERE id=?", (tid,))
            msg = _("已退回给孩子")
        elif action == "cancel":
            c.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (tid,))
            msg = _("已取消")
        else:
            raise HTTPException(400, _("无法执行"))
    default = "/me" if p.role == "child" else "/tasks"
    return back(next if next.startswith("/") else default, msg)


# ------------------------------------------------------------------------------------------ report
@app.get("/report", response_class=HTMLResponse)
def report_page(request: Request, week: str = "0"):
    p = auth.require(request, "parent", "tutor")
    week = max(-520, min(0, int(week))) if week.lstrip("-").isdigit() else 0
    start, end = report.week_bounds(week)
    with db.tx() as c:
        kids = children(c)
        data = [(k, [(s, report.child_subject(c, k["id"], s, start, end)) for s in p.visible_subjects()])
                for k in kids]
    return render(request, "report.html", p, data=data, start=start, end=end, week=week)


# ------------------------------------------------------------------------------------------ settings
@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    p = auth.require(request, "parent")
    with db.tx() as c:
        users = c.execute("SELECT * FROM users ORDER BY active DESC, role, name").fetchall()
        cause_list = db.causes(c)
        topic_cfg = {s: db.kv_get(c, f"topics:{s}", []) for s in db.SUBJECTS}
        model_over = db.kv_get(c, "model_config", {}) or {}
    return render(request, "settings.html", p, users=users, cause_list=cause_list, topic_cfg=topic_cfg,
                  model=ai.config(), model_over=model_over, backups=backup.list_backups())


@app.post("/settings/users")
def save_user(request: Request, uid: int = Form(0), name: str = Form(""), role: str = Form(""),
              subjects: list[str] = Form([]), grade: str = Form(""), pin: str = Form(""), active: str = Form("1")):
    p = auth.require(request, "parent")
    name, pin, on = clip("name", name), pin.strip(), active == "1"
    if role not in auth.MIN_PIN or not name:
        return back("/settings", _("请填写姓名和角色"))
    if (uid == 0 or pin) and (err := auth.validate_pin(pin, role)):
        return back("/settings", err)
    g = int(grade) if grade.isdigit() else None
    if g is not None and not 1 <= g <= 12:
        return back("/settings", _("年级要在 1 到 12 之间"))
    subs = ",".join(s for s in db.SUBJECTS if s in subjects) if role == "tutor" else ""
    if role == "tutor" and not subs:
        return back("/settings", _("请给老师选择至少一个科目"))
    with db.tx() as c:
        if c.execute("SELECT 1 FROM users WHERE lower(name) = lower(?) AND active = 1 AND id != ?",
                     (name, uid)).fetchone():
            return back("/settings", _("已经有叫 {n} 的账号了", n=name))
        if uid == 0:
            c.execute("""INSERT INTO users(name, role, subjects, grade, pin_hash, created_at)
                         VALUES (?,?,?,?,?,?)""", (name, role, subs, g if role == "child" else None,
                                                    auth.hash_pin(pin), db.now_iso()))
            return back("/settings", _("已添加 {n}", n=name))
        u = c.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        if not u:
            return back("/settings", _("账号不存在"))
        if uid == p.id and (role != "parent" or not on):
            return back("/settings", _("不能停用或降级自己"))
        if u["role"] == "parent" and (role != "parent" or not on) and c.execute(
                "SELECT COUNT(*) FROM users WHERE role = 'parent' AND active = 1 AND id != ?", (uid,)).fetchone()[0] == 0:
            return back("/settings", _("至少要保留一个家长账号"))
        if u["role"] == "child" and role != "child" and (
                c.execute("SELECT 1 FROM mistakes WHERE child_id = ?", (uid,)).fetchone()
                or c.execute("SELECT 1 FROM tasks WHERE child_id = ?", (uid,)).fetchone()):
            return back("/settings", _("这个孩子已有错题或任务，不能改成其他角色"))
        c.execute("UPDATE users SET name=?, role=?, subjects=?, grade=?, active=? WHERE id=?",
                  (name, role, subs, g if role == "child" else None, 1 if on else 0, uid))
        if pin:
            c.execute("""UPDATE users SET pin_hash=?, session_version=session_version+1, failed_logins=0,
                         locked_until=NULL WHERE id=?""", (auth.hash_pin(pin), uid))
        if not on or role != u["role"]:
            c.execute("UPDATE users SET session_version=session_version+1 WHERE id=?", (uid,))
    return back("/settings", _("已保存 {n}", n=name) + (_("（PIN 已更新，旧登录失效）") if pin else ""))


@app.post("/settings/lists")
def save_lists(request: Request, causes: str = Form(""), topics_chinese: str = Form(""),
               topics_english: str = Form(""), topics_math: str = Form("")):
    auth.require(request, "parent")

    def lines(s: str) -> list[str]:
        return list(dict.fromkeys(x.strip().replace(",", "，") for x in s.splitlines() if x.strip()))

    with db.tx() as c:
        db.kv_set(c, "causes", lines(causes) or db.default_causes())
        for s, v in (("chinese", topics_chinese), ("english", topics_english), ("math", topics_math)):
            db.kv_set(c, f"topics:{s}", lines(v))
    return back("/settings", _("已保存"))


@app.post("/settings/model")
def save_model(request: Request, provider: str = Form(""), model: str = Form(""),
               base_url: str = Form(""), api_key: str = Form(""), clear_key: str = Form("")):
    auth.require(request, "parent")
    if provider not in ("", "anthropic", "openai_compatible", "mock"):
        raise HTTPException(400)
    api_key = "".join(api_key.split())  # pasted keys often carry spaces / line breaks
    with db.tx() as c:
        db.kv_set(c, "model_config", {"provider": provider, "model": model.strip(), "base_url": base_url.strip()})
        target = provider or ai.config().provider
        if target and api_key:
            db.kv_set(c, f"api_key:{target}", api_key)
        elif target and clear_key:
            c.execute("DELETE FROM kv WHERE key = ?", (f"api_key:{target}",))
    cfg = ai.config()
    return back("/settings", _("模型设置已保存：{l}", l=cfg.label) + (f"（{cfg.problem}）" if cfg.problem else ""))


@app.post("/settings/model/test")
def test_model(request: Request):
    auth.require(request, "parent")
    try:
        return back("/settings", "✓ " + ai.ping())
    except (ai.AIError, ValueError, KeyError) as e:  # ValueError covers JSON that does not parse
        return back("/settings", _("✗ 测试失败：{e}", e=e))


@app.post("/settings/lang")
def save_lang(request: Request, lang: str = Form(...)):
    auth.require(request, "parent")
    if lang not in i18n.LANGS:
        raise HTTPException(400)
    i18n.set_lang(lang)
    with db.tx() as c:  # an untouched default cause list follows the language
        if db.kv_get(c, "causes") in (db.DEFAULT_CAUSES, db.DEFAULT_CAUSES_EN):
            c.execute("DELETE FROM kv WHERE key = 'causes'")
    return back("/settings", _("语言已切换"))


@app.post("/settings/backup")
def backup_now(request: Request):
    auth.require(request, "parent")
    name = backup.run(force=True)
    return back("/settings", _("已备份：{n}", n=name))


def _json(v: object) -> str:
    return json.dumps(v, ensure_ascii=False)


templates.env.filters["json"] = _json
