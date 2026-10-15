"""The web app: mobile-first pages that work in any phone browser, so the camera works
without installing anything."""

from __future__ import annotations

import calendar as cal
import json
import os
import secrets
import tempfile
import threading
import time
from datetime import date, timedelta
from pathlib import Path

from flask import (Flask, abort, flash, g, redirect, render_template, request, send_file, session, url_for)
from werkzeug.utils import secure_filename

from .. import db, demo, pdfs
from .. import services as S
from ..agent import watchdog
from ..capture import extract as X
from ..capture import llm, ocr, textlog
from ..rules import engine as E

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
TYPE_OPTIONS = [(k, v) for k, v in E.ACTIVITY_LABELS.items()]
EXEMPTION_QUESTIONS = [
    # (key, question, date key or None, date question, private)
    ("pregnant", "I'm pregnant", None, None, False),
    ("postpartum", "I gave birth in the last 12 months", "baby_birth_date", "Baby's birth date", False),
    ("caregiver_child", "I care for a child under 14", "child_birth_date", "Youngest child's birth date", False),
    ("caregiver_disabled", "I care for a person with a disability", None, None, False),
    ("tribal", "I'm American Indian or Alaska Native", None, None, False),
    ("veteran_total_disability", "I'm a veteran with a 100% disability rating", None, None, False),
    ("snap_tanf_work", "I already meet the SNAP (food stamps) or TANF work rules", None, None, False),
    ("former_foster_youth", "I was in foster care at 18 and I'm under 26", None, None, False),
    ("released_incarceration", "I was released from jail or prison recently", "release_date", "Release date", True),
    ("hospital_stay", "I had a hospital or inpatient stay recently", "hospital_end_date", "Date I left", True),
    ("medically_frail", "I have a serious health condition that makes 80 hours a month hard", None, None, True),
    ("treatment", "I'm in a drug or alcohol treatment program", None, None, True),
]
DATE_ONLY = {"postpartum": "baby_birth_date", "released_incarceration": "release_date",
             "hospital_stay": "hospital_end_date"}
ONBOARD_STEPS = ["About you", "Exemptions", "Your activities", "Reminders", "Your plan"]


def create_app(conn=None) -> Flask:
    app = Flask(__name__)
    key_file = db.DATA_DIR / "secret.key"
    db.DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not key_file.exists():
        key_file.write_text(secrets.token_hex(32))
    app.secret_key = key_file.read_text().strip()
    app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024
    shared = {"conn": conn}
    app.jinja_env.filters["dshort"] = lambda s: watchdog.fmt_d(date.fromisoformat(s)) if s else ""
    app.jinja_env.filters["dlong"] = lambda s: (lambda d: f"{d.strftime('%B')} {d.day}, {d.year}")(date.fromisoformat(s)) if s else ""

    # Public mode (an online link for judges): every browser gets its own private
    # database, deleted after a day, so two visitors never see each other's demo.
    public = os.environ.get("KEPT_PUBLIC") == "1" and conn is None
    app.config["PUBLIC"] = public
    sessions_dir = db.DATA_DIR / "sessions"
    cache: dict = {}
    lock = threading.Lock()

    def _cleanup():
        cutoff = time.time() - 24 * 3600
        for f in sessions_dir.glob("*.sqlite3"):
            if f.stat().st_mtime < cutoff and f.stem not in cache:
                f.unlink(missing_ok=True)

    def get_conn():
        if not public:
            if shared["conn"] is None:
                shared["conn"] = db.connect()
            return shared["conn"]
        sid = session.get("sid")
        if not sid or not all(ch in "0123456789abcdef" for ch in sid):
            sid = secrets.token_hex(16)
            session["sid"] = sid
            session.permanent = True
            sessions_dir.mkdir(parents=True, exist_ok=True)
            _cleanup()
        with lock:
            if sid not in cache:
                if len(cache) > 300:  # keep memory bounded
                    old = next(iter(cache))
                    cache.pop(old).close()
                cache[sid] = db.connect(sessions_dir / f"{sid}.sqlite3")
            return cache[sid]

    # ------------------------------------------------------------ request setup

    @app.before_request
    def load():
        g.conn = get_conn()
        g.today = db.today(g.conn)
        g.person = db.get_person(g.conn)
        g.demo = bool(g.person and g.person["is_demo"])
        if g.person and not g.demo:
            watchdog.catch_up(g.conn, g.person, g.today)  # the agent's daily run

    @app.context_processor
    def globals_():
        return dict(today=g.get("today"), person=g.get("person"), is_demo=g.get("demo"), public=app.config["PUBLIC"],
                    E=E, fmt_h=watchdog.fmt_h, fmt_d=watchdog.fmt_d, ai_on=llm.available(),
                    ai_enabled=llm.is_enabled(), WEEKDAYS=WEEKDAYS, TYPE_OPTIONS=TYPE_OPTIONS,
                    activity_labels=E.ACTIVITY_LABELS, nav=request.endpoint)

    def need_person():
        if not g.person:
            return redirect(url_for("welcome"))
        return None

    def ctx():
        return S.context(g.conn, g.person, g.today)

    def after_change():
        watchdog.rerun_today(g.conn, g.person, g.today)

    # ------------------------------------------------------------ welcome / onboarding

    @app.get("/welcome")
    def welcome():
        own = g.conn.execute("SELECT * FROM person WHERE is_demo=0 ORDER BY id").fetchall()
        return render_template("welcome.html", own=own)

    @app.post("/switch/<int:pid>")
    def switch(pid):
        p = db.get_person(g.conn, pid)
        if not p:
            abort(404)
        db.set_setting(g.conn, "active_person", pid)
        db.set_setting(g.conn, "sim_today", None if not p["is_demo"] else db.get_setting(g.conn, "sim_today"))
        return redirect(url_for("home"))

    @app.post("/demo/start")
    def demo_start():
        demo.reset(g.conn)
        flash("You're looking at Keisha's phone on October 20, 2026. She's a gig worker in Montana.")
        return redirect(url_for("home"))

    @app.get("/onboarding/<int:step>")
    def onboarding(step):
        if step > 1 and not g.person:
            return redirect(url_for("onboarding", step=1))
        person = g.person if g.person and not g.demo else None
        answers = db.all_exemption_answers(g.conn, person) if person else {}
        acts = [dict(a) for a in db.activities(g.conn, person["id"])] if person else []
        plan = None
        if step == 5 and person:
            c = ctx()
            plan = dict(mode=S.mode(c), mode_text=S.MODE_TEXT[S.mode(c)],
                        exemptions=E.evaluate_exemptions(c.answers, g.today, c.rules),
                        check=c.check_date(), months=c.checked_months(), rules=c.rules)
        return render_template("onboarding.html", step=step, steps=ONBOARD_STEPS, p=person, answers=answers,
                               acts=acts, questions=EXEMPTION_QUESTIONS, states=E.supported_states(), plan=plan)

    @app.post("/onboarding/<int:step>")
    def onboarding_save(step):
        f = request.form
        if step == 1:
            vals = (f.get("name", "").strip() or "Me", f.get("state", "MT"), f.get("birth_date") or None,
                    f.get("status", "enrolled"), f.get("last_renewal") or None, f.get("application_date") or None)
            if g.person and not g.demo:
                g.conn.execute("UPDATE person SET name=?,state=?,birth_date=?,status=?,last_renewal=?,application_date=? WHERE id=?",
                               vals + (g.person["id"],))
            else:
                cur = g.conn.execute(
                    "INSERT INTO person(name,state,birth_date,status,last_renewal,application_date,created_at) VALUES(?,?,?,?,?,?,?)",
                    vals + (db.now_iso(),))
                db.set_setting(g.conn, "active_person", cur.lastrowid)
                db.set_setting(g.conn, "sim_today", None)  # leave demo time
            g.conn.commit()
            return redirect(url_for("onboarding", step=2))
        if not g.person:
            return redirect(url_for("onboarding", step=1))
        pid = g.person["id"]
        if step == 2:
            public, private = {}, {}
            for key, _q, dkey, _dq, is_private in EXEMPTION_QUESTIONS:
                target = private if is_private else public
                checked = bool(f.get(key))
                if key not in DATE_ONLY:
                    target[key] = checked
                if dkey:
                    target[dkey] = f.get(dkey) if checked and f.get(dkey) else None
            g.conn.execute("UPDATE person SET answers=? WHERE id=?", (json.dumps(public), pid))
            for k, v in private.items():
                db.set_private(g.conn, pid, k, v)
            g.conn.commit()
            return redirect(url_for("onboarding", step=3))
        if step == 3:
            if f.get("name"):
                _save_activity(g.conn, pid, f, g.today)
                flash(f"Added {f.get('name')}.")
                return redirect(url_for("onboarding", step=3))
            if not db.activities(g.conn, pid):
                flash("Add at least one activity: a job, an app, volunteering or school.")
                return redirect(url_for("onboarding", step=3))
            return redirect(url_for("onboarding", step=4))
        if step == 4:
            g.conn.execute("UPDATE person SET channel=?,language=?,quiet_start=?,quiet_end=?,max_alerts_week=?,seasonal=? WHERE id=?",
                           (f.get("channel", "text"), f.get("language", "en"), int(f.get("quiet_start", 21)),
                            int(f.get("quiet_end", 8)), int(f.get("max_alerts_week", 4)), 1 if f.get("seasonal") else 0, pid))
            g.conn.commit()
            return redirect(url_for("onboarding", step=5))
        return redirect(url_for("home"))

    @app.post("/activity/<int:aid>/remove")
    def activity_remove(aid):
        g.conn.execute("UPDATE activity SET active=0 WHERE id=? AND person_id=?", (aid, g.person["id"]))
        g.conn.commit()
        return redirect(request.form.get("next") or url_for("profile"))

    @app.post("/activity/<int:aid>/confirm")
    def activity_confirm(aid):
        g.conn.execute("UPDATE activity SET last_confirmed=? WHERE id=? AND person_id=?",
                       (g.today.isoformat(), aid, g.person["id"]))
        g.conn.commit()
        flash("Thanks, noted.")
        return redirect(url_for("profile"))

    # ------------------------------------------------------------ home

    @app.get("/")
    def home():
        r = need_person()
        if r:
            return r
        c = ctx()
        month = E.month_key(g.today)
        s = c.summary(month)
        alerts = g.conn.execute(
            "SELECT * FROM alert WHERE person_id=? AND status='sent' AND day>=? AND day<=? ORDER BY day DESC, priority, id DESC",
            (g.person["id"], (g.today - timedelta(days=10)).isoformat(), g.today.isoformat())).fetchall()
        # newest message of each kind only, and only while it's still true; older ones are in Messages
        seen, kept = set(), []
        for a in alerts:
            age = (g.today - date.fromisoformat(a["day"])).days
            if a["kind"] == "month" and (s.status != "at_risk" or age > 5):
                continue  # the month is fixed, or a newer check will speak
            if a["kind"] == "letter":
                lid = int(a["key"].split(":")[1])
                st = g.conn.execute("SELECT status FROM letter WHERE id=?", (lid,)).fetchone()
                if not st or st["status"] != "open":
                    continue
            if a["kind"] in ("check", "nudge") and age > 3:
                continue
            if a["kind"] not in ("month", "letter", "check", "nudge") and age > 7:
                continue
            if a["kind"] not in seen:
                seen.add(a["kind"])
                kept.append(a)
        kept.sort(key=lambda a: (not a["urgent"], a["priority"]))
        alerts = kept[:2]
        check = c.check_date()
        recent = g.conn.execute(
            "SELECT e.*, a.name, a.type FROM entry e JOIN activity a ON a.id=e.activity_id WHERE e.person_id=? "
            "AND e.superseded_by IS NULL AND e.start_date<=? ORDER BY e.start_date DESC, e.id DESC LIMIT 4",
            (g.person["id"], g.today.isoformat())).fetchall()
        return render_template("home.html", s=s, month=month, cells=_timecard(c, month, s), alerts=alerts,
                               check=check, check_left=(check - g.today).days if check else None,
                               months=c.checked_months(), mode=S.mode(c), recent=recent,
                               proven=lambda r: S.is_proven(g.conn, r))

    @app.post("/alert/<int:aid>/dismiss")
    def alert_dismiss(aid):
        g.conn.execute("UPDATE alert SET status='dismissed' WHERE id=? AND person_id=?", (aid, g.person["id"]))
        g.conn.commit()
        return redirect(request.referrer or url_for("home"))

    # ------------------------------------------------------------ add proof

    @app.get("/add")
    def add():
        r = need_person()
        if r:
            return r
        return render_template("add.html", acts=db.activities(g.conn, g.person["id"]),
                               ocr_ok=ocr.available(), preset=request.args.get("activity", type=int),
                               letter=request.args.get("letter"))

    @app.post("/add/text")
    def add_text():
        text = request.form.get("text", "").strip()
        if not text:
            flash("Type what you did, for example: Dashly 3-8pm $85.")
            return redirect(url_for("add"))
        acts = [dict(a) for a in db.activities(g.conn, g.person["id"])]
        d = textlog.parse(text, g.today, acts)
        return render_template("review_text.html", d=d, acts=acts, ask_below=X.ASK_BELOW)

    @app.post("/add/manual")
    def add_manual():
        f = request.form
        try:
            aid = int(f["activity_id"])
            start = date.fromisoformat(f["start"])
            end = date.fromisoformat(f.get("end") or f["start"])
        except (KeyError, ValueError):
            flash("Pick an activity and a date.")
            return redirect(url_for("add"))
        hours = _float(f.get("hours"))
        income = _float(f.get("income"))
        act = g.conn.execute("SELECT * FROM activity WHERE id=? AND person_id=?", (aid, g.person["id"])).fetchone()
        err = _check_values(act, start, end, hours, income, g.today)
        if err:
            flash(err)
            return redirect(url_for("add"))
        S.add_entry(g.conn, g.person["id"], aid, start, end, hours,
                    income if act["type"] in S.INCOME_TYPES else None,
                    f.get("source", "form"), f.get("read_by", "manual"), note=f.get("note"))
        after_change()
        flash(f"Logged: {_entry_words(act, start, end, hours, income)}.")
        return redirect(url_for("home"))

    @app.post("/add/photo")
    def add_photo():
        file = next((fl for fl in (request.files.get("photo_cam"), request.files.get("photo_file"),
                                   request.files.get("photo")) if fl and fl.filename), None)
        if not file:
            flash("Choose a photo or take one first.")
            return redirect(url_for("add"))
        with tempfile.NamedTemporaryFile(delete=False, suffix=Path(file.filename).suffix) as tmp:
            file.save(tmp.name)
            path = Path(tmp.name)
        return _process_upload(path, secure_filename(file.filename) or "photo.jpg",
                               is_letter=bool(request.form.get("is_letter")),
                               activity_id=request.form.get("activity_id", type=int))

    def _process_upload(path: Path, name: str, is_letter=False, activity_id=None):
        doc_id, err = S.store_upload(g.conn, g.person["id"], path, name)
        if err == "duplicate":
            flash("You already added this exact photo. It's counted once.")
            return redirect(url_for("review_doc", doc_id=doc_id))
        if err:
            flash("That file couldn't be opened. Try a JPG or PNG photo.")
            return redirect(url_for("add"))
        c = ctx()
        doc = g.conn.execute("SELECT * FROM document WHERE id=?", (doc_id,)).fetchone()
        if is_letter or (doc["ocr_text"] and X.classify(doc["ocr_text"]) == "letter"):
            lid = S.save_letter(g.conn, g.person["id"], doc_id, c)
            after_change()
            return redirect(url_for("letter_page", lid=lid))
        S.analyse_document(g.conn, doc_id, c)
        if activity_id:
            g.conn.execute("UPDATE document SET activity_id=? WHERE id=?", (activity_id, doc_id))
            g.conn.commit()
        return redirect(url_for("review_doc", doc_id=doc_id))

    @app.get("/review/<int:doc_id>")
    def review_doc(doc_id):
        doc = g.conn.execute("SELECT * FROM document WHERE id=? AND person_id=?", (doc_id, g.person["id"])).fetchone()
        if not doc:
            abort(404)
        fields = json.loads(doc["fields"] or "{}")
        conf = json.loads(doc["confidences"] or "{}")
        if doc["activity_id"] and not fields.get("activity_id"):
            fields["activity_id"], conf["activity_id"] = doc["activity_id"], 1.0
        return render_template("review_doc.html", doc=doc, f=fields, conf=conf, ask_below=X.ASK_BELOW,
                               acts=db.activities(g.conn, g.person["id"]), ocr_ok=bool(doc["ocr_ok"]))

    @app.post("/review/<int:doc_id>")
    def review_doc_save(doc_id):
        f = request.form
        if f.get("action") == "reject":
            g.conn.execute("UPDATE document SET status='rejected' WHERE id=? AND person_id=?", (doc_id, g.person["id"]))
            g.conn.commit()
            flash("Removed. Nothing from that photo was counted.")
            return redirect(url_for("add"))
        try:
            aid = int(f["activity_id"])
            start = date.fromisoformat(f["start"])
            end = date.fromisoformat(f.get("end") or f["start"])
        except (KeyError, ValueError):
            flash("Please pick the activity and the dates.")
            return redirect(url_for("review_doc", doc_id=doc_id))
        hours, income = _float(f.get("hours")), _float(f.get("income"))
        act = g.conn.execute("SELECT * FROM activity WHERE id=? AND person_id=?", (aid, g.person["id"])).fetchone()
        err = _check_values(act, start, end, hours, income, g.today)
        if err:
            flash(err)
            return redirect(url_for("review_doc", doc_id=doc_id))
        res = S.confirm_document(g.conn, g.person["id"], doc_id, aid, start, end, hours, income)
        after_change()
        msg = f"Saved with proof: {_entry_words(act, start, end, hours, income)}."
        if res["replaced"]:
            n = len(res["replaced"])
            msg += f" It replaces {n} line{'s' if n > 1 else ''} you typed for the same days, so nothing is counted twice."
        flash(msg)
        return redirect(url_for("folder_month", month=start.strftime("%Y-%m")))

    @app.get("/files/<int:doc_id>")
    def file(doc_id):
        doc = g.conn.execute("SELECT * FROM document WHERE id=? AND person_id=?", (doc_id, g.person["id"])).fetchone()
        if not doc:
            abort(404)
        return send_file(db.UPLOAD_DIR / doc["filename"])

    @app.post("/entry/<int:eid>/delete")
    def entry_delete(eid):
        g.conn.execute("DELETE FROM entry WHERE id=? AND person_id=?", (eid, g.person["id"]))
        g.conn.execute("UPDATE entry SET superseded_by=NULL WHERE superseded_by=? AND person_id=?", (eid, g.person["id"]))
        g.conn.commit()
        after_change()
        flash("Removed.")
        return redirect(request.referrer or url_for("calendar_page"))

    # ------------------------------------------------------------ calendar

    @app.get("/calendar")
    def calendar_page():
        r = need_person()
        if r:
            return r
        month = request.args.get("month") or E.month_key(g.today)
        m0 = E.parse_month(month)
        c = ctx()
        rows = g.conn.execute(
            "SELECT e.*, a.name, a.type FROM entry e JOIN activity a ON a.id=e.activity_id WHERE e.person_id=? "
            "AND e.superseded_by IS NULL AND e.start_date<=? AND e.end_date>=? AND e.start_date<=? ORDER BY e.start_date",
            (g.person["id"], E.month_end(m0).isoformat(), m0.isoformat(), g.today.isoformat())).fetchall()
        by_day = {}
        for r_ in rows:
            d0, d1 = date.fromisoformat(r_["start_date"]), date.fromisoformat(r_["end_date"])
            d = max(d0, m0)
            while d <= min(d1, E.month_end(m0)):
                by_day.setdefault(d.day, []).append(r_)
                d += timedelta(days=1)
        sel = request.args.get("day", type=int)
        weeks = cal.Calendar(firstweekday=0).monthdayscalendar(m0.year, m0.month)
        return render_template("calendar.html", month=month, m0=m0, weeks=weeks, by_day=by_day, sel=sel,
                               s=c.summary(month), prev=E.month_key(E.add_months(m0, -1)),
                               next=E.month_key(E.add_months(m0, 1)), checked=c.checked_months(),
                               proven=lambda r: S.is_proven(g.conn, r))

    # ------------------------------------------------------------ folder

    @app.get("/folder")
    def folder():
        r = need_person()
        if r:
            return r
        c = ctx()
        checked = c.checked_months()
        cur = E.month_key(g.today)
        first = g.conn.execute("SELECT MIN(start_date) d FROM entry WHERE person_id=?", (g.person["id"],)).fetchone()["d"]
        start = E.parse_month(first[:7]) if first else E.parse_month(cur)
        keys, d = [], E.parse_month(cur)
        while d >= start and len(keys) < 12:
            keys.append(E.month_key(d))
            d = E.add_months(d, -1)
        for m in checked:
            if m not in keys:
                keys.append(m)
        keys.sort(reverse=True)
        months = [(m, c.summary(m)) for m in keys]
        check = c.check_date()
        return render_template("folder.html", months=months, checked=checked, check=check,
                               check_left=(check - g.today).days if check else None, rules=c.rules)

    @app.get("/folder/<month>")
    def folder_month(month):
        r = need_person()
        if r:
            return r
        c = ctx()
        s = c.summary(month)
        acts = []
        for a in c.activities:
            hrs = sum(v for (aid, _n, _t), v in s.hours_by_activity.items() if aid == a["id"])
            ents = g.conn.execute(
                "SELECT * FROM entry WHERE person_id=? AND activity_id=? AND superseded_by IS NULL AND start_date<=? "
                "AND end_date>=? AND start_date<=?",
                (g.person["id"], a["id"], E.month_end(E.parse_month(month)).isoformat(), E.parse_month(month).isoformat(),
                 g.today.isoformat())).fetchall()
            if not ents and not a["active"]:
                continue
            proven = sum(1 for e in ents if S.is_proven(g.conn, e))
            so = g.conn.execute("SELECT * FROM signoff WHERE person_id=? AND activity_id=? AND month=?",
                                (g.person["id"], a["id"], month)).fetchone()
            if not ents:
                continue
            acts.append(dict(a=a, hours=hrs, n=len(ents), proven=proven, signoff=so,
                             need=c.rules["proof_by_activity"].get(a["type"], "")))
        docs = g.conn.execute(
            "SELECT DISTINCT d.* FROM document d JOIN entry e ON e.document_id=d.id WHERE d.person_id=? AND d.status='confirmed' "
            "AND e.superseded_by IS NULL AND substr(e.start_date,1,7)<=? AND substr(e.end_date,1,7)>=?",
            (g.person["id"], month, month)).fetchall()
        return render_template("folder_month.html", month=month, s=s, acts=acts, docs=docs,
                               counted=month in c.checked_months(), cells=_timecard(c, month, s),
                               can_sign=E.parse_month(month) < E.month_start(g.today) or True)

    @app.post("/folder/<month>/signoff/<int:aid>")
    def make_signoff(month, aid):
        c = ctx()
        s = c.summary(month)
        hrs = sum(v for (a, _n, _t), v in s.hours_by_activity.items() if a == aid)
        sid = S.prepare_signoff(g.conn, g.person["id"], aid, month, hrs)
        return redirect(url_for("pdf_signoff", sid=sid))

    @app.get("/pdf/log/<months>")
    def pdf_log(months):
        ms = months.split(",")
        data = pdfs.reporting_log(g.conn, ctx(), ms)
        return _pdf(data, f"reporting-log-{months.replace(',', '_')}.pdf")

    @app.get("/pdf/packet/<months>")
    def pdf_packet(months):
        ms = months.split(",")
        data = pdfs.proof_packet(g.conn, ctx(), ms)
        return _pdf(data, f"proof-packet-{months.replace(',', '_')}.pdf")

    @app.get("/pdf/signoff/<int:sid>")
    def pdf_signoff(sid):
        so = g.conn.execute("SELECT * FROM signoff WHERE id=? AND person_id=?", (sid, g.person["id"])).fetchone()
        if not so:
            abort(404)
        c = ctx()
        data = pdfs.volunteer_page(g.conn, c, c.activity(so["activity_id"]), so["month"], so["hours"])
        return _pdf(data, f"volunteer-page-{so['month']}.pdf")

    # ------------------------------------------------------------ letters

    @app.get("/letters")
    def letters_page():
        r = need_person()
        if r:
            return r
        rows = [dict(r, left=(date.fromisoformat(r["deadline"]) - g.today).days) for r in g.conn.execute(
            "SELECT * FROM letter WHERE person_id=? ORDER BY status, deadline", (g.person["id"],))]
        return render_template("letters.html", letters=rows, title_of=S.letter_title)

    @app.get("/letters/<int:lid>")
    def letter_page(lid):
        L = g.conn.execute("SELECT * FROM letter WHERE id=? AND person_id=?", (lid, g.person["id"])).fetchone()
        if not L:
            abort(404)
        c = ctx()
        months = json.loads(L["months"] or "[]")
        sums = [(m, c.summary(m)) for m in months]
        left = (date.fromisoformat(L["deadline"]) - g.today).days
        return render_template("letter.html", L=L, sums=sums, left=left, title=S.letter_title(L["kind"]),
                               months=months, rules=c.rules)

    @app.post("/letters/<int:lid>/deadline")
    def letter_deadline(lid):
        d = request.form.get("deadline")
        if d:
            g.conn.execute("UPDATE letter SET deadline=?, respond_by=? WHERE id=? AND person_id=?", (d, d, lid, g.person["id"]))
            g.conn.commit()
            after_change()
            flash("Deadline updated.")
        return redirect(url_for("letter_page", lid=lid))

    @app.post("/letters/<int:lid>/responded")
    def letter_responded(lid):
        g.conn.execute("UPDATE letter SET status='responded' WHERE id=? AND person_id=?", (lid, g.person["id"]))
        g.conn.commit()
        after_change()
        flash("Marked as answered. Kept will keep your copy in the folder.")
        return redirect(url_for("letters_page"))

    # ------------------------------------------------------------ profile, agent, help, more

    @app.get("/profile")
    def profile():
        r = need_person()
        if r:
            return r
        c = ctx()
        return render_template("profile.html", c=c, mode=S.mode(c), mode_text=S.MODE_TEXT[S.mode(c)],
                               exemptions=E.evaluate_exemptions(c.answers, g.today, c.rules),
                               acts=db.activities(g.conn, g.person["id"]), check=c.check_date(),
                               months=c.checked_months(), ollama=llm.status(force=True), model=llm.MODEL,
                               ocr_ok=ocr.available())

    @app.post("/profile/ai")
    def profile_ai():
        llm.set_enabled(request.form.get("on") == "1")
        flash("AI helper turned on." if llm.is_enabled() else "AI helper turned off. Kept uses its rules only.")
        return redirect(request.referrer or url_for("profile"))

    @app.post("/profile/delete")
    def profile_delete():
        if request.form.get("confirm") != "DELETE":
            flash("Type DELETE to confirm.")
            return redirect(url_for("profile"))
        db.delete_person(g.conn, g.person["id"])
        db.set_setting(g.conn, "sim_today", None)
        flash("All your data was deleted from this device.")
        return redirect(url_for("welcome"))

    @app.get("/agent")
    def agent_page():
        r = need_person()
        if r:
            return r
        days = [r_["day"] for r_ in g.conn.execute(
            "SELECT DISTINCT day FROM agent_log WHERE person_id=? ORDER BY day DESC LIMIT 14", (g.person["id"],))]
        sel = request.args.get("day") or (days[0] if days else None)
        logs = g.conn.execute("SELECT * FROM agent_log WHERE person_id=? AND day=? ORDER BY id",
                              (g.person["id"], sel)).fetchall() if sel else []
        alerts = g.conn.execute("SELECT * FROM alert WHERE person_id=? AND day=? ORDER BY priority",
                                (g.person["id"], sel)).fetchall() if sel else []
        return render_template("agent.html", days=days, sel=sel, logs=logs, alerts=alerts)

    @app.get("/inbox")
    def inbox():
        r = need_person()
        if r:
            return r
        rows = g.conn.execute("SELECT * FROM alert WHERE person_id=? AND status IN ('sent','dismissed') AND day<=? "
                              "ORDER BY day DESC, priority LIMIT 60", (g.person["id"], g.today.isoformat())).fetchall()
        return render_template("inbox.html", alerts=rows)

    @app.get("/help")
    def help_page():
        return render_template("help.html", ollama=llm.status(), model=llm.MODEL, ocr_ok=ocr.available())

    @app.get("/more")
    def more():
        return render_template("more.html")

    # ------------------------------------------------------------ demo controls

    @app.get("/demo")
    def demo_page():
        return render_template("demo.html", samples=_samples())

    @app.post("/demo/go")
    def demo_go():
        if not g.demo:
            demo.reset(g.conn)
        target = request.form.get("to")
        if target == "next":
            t = g.today + timedelta(days=1)
        elif target == "week":
            t = g.today + timedelta(days=7)
        else:
            t = date.fromisoformat(target)
        demo.advance_to(g.conn, t)
        flash(f"Demo date: {t.strftime('%A, %B %d, %Y')}. The agent ran for every day in between.")
        return redirect(request.form.get("next") or url_for("home"))

    @app.post("/demo/letter")
    def demo_letter():
        if not g.demo:
            demo.reset(g.conn)
        lid = demo.receive_letter(g.conn)
        return redirect(url_for("letter_page", lid=lid))

    @app.post("/demo/sample/<name>")
    def demo_sample(name):
        if name not in _samples():
            abort(404)
        if not g.demo:
            demo.reset(g.conn)
        # each sample covers dates that must already be in the past on the demo clock
        ready = {"sample_gig_statement": date(2026, 10, 20), "sample_pay_stub": date(2026, 11, 3),
                 "sample_volunteer_sheet": date(2026, 11, 1), "sample_state_letter": date(2026, 11, 25)}[name]
        if db.today(g.conn) < ready:
            demo.advance_to(g.conn, ready)
            flash(f"Demo date moved to {ready.strftime('%B')} {ready.day} so this document's dates are in the past.")
        g.person = db.get_person(g.conn)
        g.today = db.today(g.conn)
        if name == "sample_state_letter":
            return redirect(url_for("letter_page", lid=demo.receive_letter(g.conn)))
        return _process_upload(demo.SAMPLES / f"{name}.png", f"{name}.png", is_letter=name.endswith("letter"))

    @app.get("/samples/<name>.png")
    def sample_image(name):
        if name not in _samples():
            abort(404)
        return send_file(demo.SAMPLES / f"{name}.png")

    @app.errorhandler(413)
    def too_big(_e):
        flash("That file is too big (25 MB max). Try a normal photo.")
        return redirect(url_for("add"))

    return app


# ---------------------------------------------------------------- helpers

def _samples():
    return {"sample_gig_statement": "Dashly weekly earnings, Oct 5 to 11",
            "sample_pay_stub": "Corner Market pay stub, Oct 19 to 31",
            "sample_volunteer_sheet": "Pantry hours sheet, signed, October",
            "sample_state_letter": "Letter from the state, Nov 23"}


def _dismissed(aid):
    return bool(g.conn.execute("SELECT 1 FROM alert WHERE id=? AND status='dismissed'", (aid,)).fetchone())


def _pdf(data: bytes, name: str):
    from io import BytesIO
    return send_file(BytesIO(data), mimetype="application/pdf", download_name=name, as_attachment=False)


def _float(v):
    try:
        return round(float(str(v).replace("$", "").replace(",", "").strip()), 2) if v not in (None, "") else None
    except ValueError:
        return None


def _check_values(act, start, end, hours, income, today):
    if not act:
        return "Pick one of your activities."
    if end < start:
        return "The end date is before the start date."
    if start > today:
        return "That date is in the future. Kept only logs work you've done."
    days = (end - start).days + 1
    if hours is not None and (hours < 0 or hours > 24 * days):
        return f"{hours:g} hours doesn't fit in {days} day(s). Please check the number."
    if hours is None and (income is None or act["type"] not in S.INCOME_TYPES):
        return "Add the hours (or the pay, for a paid job)."
    if income is not None and income < 0:
        return "Pay can't be negative."
    return None


def _entry_words(act, start, end, hours, income):
    when = watchdog.fmt_d(start) if start == end else f"{watchdog.fmt_d(start)} to {watchdog.fmt_d(end)}"
    bits = []
    if hours:
        bits.append(f"{watchdog.fmt_h(hours)}h")
    if income and act["type"] in S.INCOME_TYPES:
        bits.append(f"${income:,.2f}")
    return f"{' and '.join(bits)} {act['name']}, {when}"


def _save_activity(conn, pid, f, today):
    def num(k):
        return _float(f.get(k))
    day = f.get("usual_day")
    conn.execute(
        "INSERT INTO activity(person_id,name,type,usual_day,usual_hours,pay_rate,half_time,org_tax_id,org_address,"
        "org_phone,contact_name,keywords,last_confirmed) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, f.get("name").strip(), f.get("type", "work"), int(day) if day not in (None, "") else None,
         num("usual_hours"), num("pay_rate"), 1 if f.get("half_time") else 0, f.get("org_tax_id") or None,
         f.get("org_address") or None, f.get("org_phone") or None, f.get("contact_name") or None,
         f.get("keywords") or "", today.isoformat()))
    conn.commit()


def _timecard(c, month, s):
    """80 cells, one per hour, in the order the hours were done, coloured by activity.
    Proven hours are solid; hours logged without proof are outlined."""
    cells = []
    portions = sorted(E.month_portions(c.entries, month), key=lambda p: p.entry.start)
    hour_types = set(c.rules["hour_activities"])
    for p in portions:
        if p.entry.activity_type not in hour_types or not p.hours:
            continue
        n = int(round(p.hours * 2)) / 2  # half hours
        while n > 0 and len(cells) < 80:
            cells.append(dict(type=p.entry.activity_type, proven=p.entry.proven, half=n < 1,
                              title=f"{p.entry.activity_name}, {watchdog.fmt_d(p.entry.start)}"))
            n -= 1
    while len(cells) < int(s.hours_target):
        cells.append(None)
    return cells[:80]
