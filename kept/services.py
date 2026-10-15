"""Operations shared by the screens and the agent: building the engine's view of a
person, saving entries, confirming documents."""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from . import db
from .capture import extract as X
from .capture import letters as L
from .capture import ocr
from .rules import engine as E

INCOME_TYPES = ("work", "gig", "cash", "self_employment")


@dataclass
class Context:
    person: object
    rules: dict
    today: date
    activities: list
    entries: list          # engine.Entry
    answers: dict          # exemption answers (incl. private)
    half_time: bool
    seasonal: bool

    def summary(self, month: str) -> E.MonthSummary:
        return E.summarize_month(self.entries, month, self.today, self.rules,
                                 exemption_answers=self.answers, seasonal=self.seasonal,
                                 half_time_student=self.half_time)

    @property
    def applying(self) -> bool:
        return self.person["status"] == "applying"

    def check_date(self) -> date | None:
        p = self.person
        if self.applying:
            return date.fromisoformat(p["application_date"]) if p["application_date"] else None
        if p["last_renewal"]:
            return E.next_check(date.fromisoformat(p["last_renewal"]), self.today, self.rules)
        return None

    def checked_months(self) -> list[str]:
        cd = self.check_date()
        return E.months_checked(cd, self.rules, applying=self.applying) if cd else []

    def activity(self, aid):
        return next((a for a in self.activities if a["id"] == aid), None)


def context(conn, person, today: date | None = None) -> Context:
    today = today or db.today(conn)
    acts = [dict(a) for a in db.activities(conn, person["id"], include_inactive=True)]
    by_id = {a["id"]: a for a in acts}
    rows = conn.execute(
        "SELECT * FROM entry WHERE person_id=? AND superseded_by IS NULL AND start_date<=?",
        (person["id"], today.isoformat())).fetchall()
    entries = []
    for r in rows:
        a = by_id.get(r["activity_id"])
        if not a:
            continue
        entries.append(E.Entry(id=r["id"], activity_id=a["id"], activity_type=a["type"],
                               start=date.fromisoformat(r["start_date"]), end=date.fromisoformat(r["end_date"]),
                               hours=r["hours"], income=r["income"], proven=is_proven(conn, r),
                               activity_name=a["name"]))
    half_time = any(a["type"] == "education" and a["half_time"] and a["active"] for a in acts)
    return Context(person=person, rules=E.load_rules(person["state"]), today=today, activities=acts,
                   entries=entries, answers=db.all_exemption_answers(conn, person),
                   half_time=half_time, seasonal=bool(person["seasonal"]))


def is_proven(conn, entry_row) -> bool:
    """Proof is a confirmed document linked to the entry, or a signed volunteer page
    covering that month."""
    if entry_row["document_id"]:
        d = conn.execute("SELECT status FROM document WHERE id=?", (entry_row["document_id"],)).fetchone()
        if d and d["status"] == "confirmed":
            return True
    month = entry_row["start_date"][:7]
    s = conn.execute("SELECT status FROM signoff WHERE person_id=? AND activity_id=? AND month=?",
                     (entry_row["person_id"], entry_row["activity_id"], month)).fetchone()
    return bool(s and s["status"] == "signed")


def mode(ctx: Context) -> str:
    """How the agent treats this person. Different people, different behaviour."""
    if any(r.meets for r in E.evaluate_exemptions(ctx.answers, ctx.today, ctx.rules)):
        return "exempt"
    if ctx.half_time:
        return "student"
    if ctx.seasonal:
        return "seasonal"
    types = {a["type"] for a in ctx.activities if a["active"]}
    if types and types <= {"work"}:
        return "payroll"
    return "full"


MODE_TEXT = {
    "exempt": "Exemption mode: Kept helps you document your exemption and warns you before it ends.",
    "student": "Student mode: half-time school counts on its own; Kept keeps your school proof and term dates.",
    "seasonal": "Seasonal mode: Kept also tracks your 6-month income average.",
    "payroll": "Light mode: the state can probably see your payroll; Kept checks in once a month.",
    "full": "Full mode: irregular work the state can't see, so Kept tracks every day and warns you early.",
}


# ---------------------------------------------------------------- entries

def add_entry(conn, pid, activity_id, start: date, end: date, hours, income, source, read_by,
              document_id=None, note=None) -> int:
    cur = conn.execute(
        "INSERT INTO entry(person_id,activity_id,start_date,end_date,hours,income,source,read_by,document_id,note,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (pid, activity_id, start.isoformat(), end.isoformat(), hours, income, source, read_by,
         document_id, note, db.now_iso()))
    conn.commit()
    return cur.lastrowid


def supersede_with_statement(conn, pid, activity_id, start: date, end: date, new_entry_id: int) -> list:
    """A statement (pay stub, app earnings) is better proof than what she typed. Typed
    entries for the same activity inside the statement's dates are replaced by it, so the
    same work is never counted twice. They are kept, marked as replaced."""
    rows = conn.execute(
        "SELECT * FROM entry WHERE person_id=? AND activity_id=? AND superseded_by IS NULL AND id<>?"
        " AND document_id IS NULL AND start_date>=? AND end_date<=?",
        (pid, activity_id, new_entry_id, start.isoformat(), end.isoformat())).fetchall()
    for r in rows:
        conn.execute("UPDATE entry SET superseded_by=? WHERE id=?", (new_entry_id, r["id"]))
    conn.commit()
    return rows


# ---------------------------------------------------------------- documents

def store_upload(conn, pid, src_path: Path, original_name: str) -> tuple[int | None, str | None]:
    """Save a photo, refuse exact duplicates, read its text. Returns (document_id, error)."""
    raw = Path(src_path).read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    dup = conn.execute("SELECT id FROM document WHERE person_id=? AND sha256=? AND status<>'rejected'",
                       (pid, sha)).fetchone()
    if dup:
        return dup["id"], "duplicate"
    name = f"{uuid.uuid4().hex}.jpg"
    dest = db.UPLOAD_DIR / name
    try:
        ocr.normalize_image(Path(src_path), dest)
    except Exception:
        if original_name.lower().endswith(".pdf"):
            name = f"{uuid.uuid4().hex}.pdf"
            dest = db.UPLOAD_DIR / name
            shutil.copy(src_path, dest)
        else:
            return None, "unreadable"
    text, ok = ("", False) if name.endswith(".pdf") else ocr.read_text(dest)
    cur = conn.execute(
        "INSERT INTO document(person_id,filename,original_name,sha256,ocr_text,ocr_ok,created_at) VALUES(?,?,?,?,?,?,?)",
        (pid, name, original_name, sha, text, int(ok), db.now_iso()))
    conn.commit()
    return cur.lastrowid, None


def analyse_document(conn, doc_id, ctx: Context, force_kind: str | None = None) -> dict:
    doc = conn.execute("SELECT * FROM document WHERE id=?", (doc_id,)).fetchone()
    text = doc["ocr_text"] or ""
    res = X.extract(text, ctx.today, ctx.activities) if text else {
        "kind": force_kind or "other", "fields": {}, "confidence": {}, "read_by": "manual",
        "ask": ["period_start", "period_end", "hours", "income", "activity_id"]}
    if force_kind:
        res["kind"] = force_kind
    fields = {k: (v.isoformat() if isinstance(v, date) else v) for k, v in res["fields"].items()}
    conn.execute("UPDATE document SET kind=?, fields=?, confidences=?, read_by=? WHERE id=?",
                 (res["kind"], json.dumps(fields), json.dumps(res["confidence"]), res["read_by"], doc_id))
    conn.commit()
    res["fields"] = fields
    return res


def confirm_document(conn, pid, doc_id, activity_id, start: date, end: date, hours, income) -> dict:
    """She checked the values. Save them as one entry carrying the document as proof."""
    act = conn.execute("SELECT * FROM activity WHERE id=?", (activity_id,)).fetchone()
    if act["type"] not in INCOME_TYPES:
        income = None
    eid = add_entry(conn, pid, activity_id, start, end, hours, income, "photo",
                    conn.execute("SELECT read_by FROM document WHERE id=?", (doc_id,)).fetchone()["read_by"],
                    document_id=doc_id)
    conn.execute("UPDATE document SET status='confirmed', activity_id=? WHERE id=?", (activity_id, doc_id))
    conn.commit()
    replaced = supersede_with_statement(conn, pid, activity_id, start, end, eid)
    if act["type"] == "volunteer":
        # a signed volunteer sheet is the month's proof
        m = start.replace(day=1)
        while m <= end:
            key = f"{m.year:04d}-{m.month:02d}"
            conn.execute(
                "INSERT INTO signoff(person_id,activity_id,month,hours,status,document_id,created_at) VALUES(?,?,?,?,?,?,?)"
                " ON CONFLICT(person_id,activity_id,month) DO UPDATE SET status='signed', document_id=excluded.document_id",
                (pid, activity_id, key, hours, "signed", doc_id, db.now_iso()))
            m = E.add_months(m, 1)
        conn.commit()
    return {"entry_id": eid, "replaced": replaced}


def save_letter(conn, pid, doc_id, ctx: Context) -> int:
    doc = conn.execute("SELECT * FROM document WHERE id=?", (doc_id,)).fetchone()
    info = L.read_letter(doc["ocr_text"] or "", ctx.today)
    deadline = E.letter_deadline(ctx.today, info["respond_by"], ctx.rules)
    cur = conn.execute(
        "INSERT INTO letter(person_id,document_id,kind,letter_date,received_date,respond_by,deadline,months,"
        "explanation,read_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (pid, doc_id, info["kind"], info["letter_date"].isoformat() if info["letter_date"] else None,
         ctx.today.isoformat(), info["respond_by"].isoformat() if info["respond_by"] else None,
         deadline.isoformat(), json.dumps(info["months"] or ctx.checked_months()), info["explanation"],
         info["read_by"], db.now_iso()))
    conn.execute("UPDATE document SET kind='letter', status='confirmed' WHERE id=?", (doc_id,))
    conn.commit()
    return cur.lastrowid


def letter_title(kind: str) -> str:
    return L.TITLES.get(kind, L.TITLES["other"])


# ---------------------------------------------------------------- sign-offs

def prepare_signoff(conn, pid, activity_id, month: str, hours: float) -> int:
    conn.execute(
        "INSERT INTO signoff(person_id,activity_id,month,hours,created_at) VALUES(?,?,?,?,?)"
        " ON CONFLICT(person_id,activity_id,month) DO UPDATE SET hours=CASE WHEN status='prepared' THEN excluded.hours ELSE hours END",
        (pid, activity_id, month, hours, db.now_iso()))
    conn.commit()
    return conn.execute("SELECT id FROM signoff WHERE person_id=? AND activity_id=? AND month=?",
                        (pid, activity_id, month)).fetchone()["id"]
