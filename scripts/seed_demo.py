"""Local demo data: two kids, three tutors. PINs: kids 1111/2222, tutors 333333…, parent from INIT_PARENT_PIN."""
from cuoti import auth, db

db.init_db()
auth.ensure_parent()
with db.tx() as c:
    if not c.execute("SELECT 1 FROM users WHERE role = 'child'").fetchone():
        for name, role, pin, subs, grade in [
            ("哥哥", "child", "1111", "", 5), ("妹妹", "child", "2222", "", 4),
            ("语文老师", "tutor", "333333", "chinese", None), ("English Tutor", "tutor", "444444", "english", None),
            ("数学老师", "tutor", "555555", "math", None),
        ]:
            c.execute("INSERT INTO users(name, role, subjects, grade, pin_hash, created_at) VALUES (?,?,?,?,?,?)",
                      (name, role, subs, grade, auth.hash_pin(pin), db.now_iso()))
print("seeded")
