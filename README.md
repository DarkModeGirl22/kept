# Kept

[![Watch the Kept demo](https://img.youtube.com/vi/N7mMnSc8it8/hqdefault.jpg)](https://www.youtube.com/watch?v=N7mMnSc8it8)

**▶ [Watch the Kept demo on YouTube](https://www.youtube.com/watch?v=N7mMnSc8it8)**

*Medicaid is the public health insurance for people with low incomes in the United States. Kept is built for the US rule that starts in 2027, and for the people it hits hardest: low-income workers whose hours change from week to week, like delivery drivers, cash jobs, volunteers and people with several small jobs.*

**Try it online:** https://kept-hlcc.onrender.com (open Keisha's demo). It runs on a free server, so the first load can take up to a minute if nobody has visited recently.

**People don't lose Medicaid because they don't work. They lose it because they can't prove it.**

From January 1, 2027, adults aged 19 to 64 covered by Medicaid expansion must show, every month, 80 hours of work, volunteering, job training or school, or at least $580 of pay. The state checks when you apply and at every renewal (every 6 months), looking back at the months just before. If its own data can't confirm your hours, it sends a letter and you have 30 days to send proof.

Kept is an agent that keeps that proof for you, day by day, warns you while a month can still be fixed, and has everything ready when the state asks.

## Why

- **About 18.5 million people** will be subject to the rule each year (CBO, cited by KFF).
- **Nearly 1 in 5** eligible adults in expansion states are at risk because their hours are too few or too irregular (Boston University, *JAMA Health Forum*, 2026).
- **Arkansas, 2018:** about 18,000 people lost coverage in seven months, yet a Harvard study (*NEJM*, 2019) found about 97% of those aged 30 to 49 were already working or should have been exempt. In February 2019, 88% of the people required to report didn't. Only 11% got their coverage back the next year.
- **Georgia** asks for hours every month: each caseworker had 200 to 300 people a month who forgot.
- **Louisiana** texted 13,000 people a link to verify their income: fewer than 7% completed it (KFF Health News, 2025).

The people the state's data can't see are the ones with irregular work: delivery apps, cash shifts, volunteering, several small jobs. Kept is built for them.

## What the agent does, every day, without being asked

1. **Letters:** a state letter with a deadline is always shown, with a countdown.
2. **Exemptions:** warns 30 days before an exemption ends.
3. **This month:** on set days, if the month is at risk, says how to fix it with *her own* routine ("your Saturday pantry shifts on Oct 24 and Oct 31 add 8h"), and whether the $580 income path is closer.
4. **State check:** 45, 30, 14, 7, 3 and 1 days before a renewal, lists the months that count and their status.
5. **Month close:** on the 1st, closes last month and prepares the volunteer page for the organization to sign, now, not three months later.
6. **Proof:** lists work logged without a document.
7. **Nudge:** if nothing was logged for 3 days (full mode only).
8. **Recheck:** asks every 60 days if her routine is still the same.

Then it ranks the messages and rations them: urgent ones always go out, the rest share a weekly limit she chose, so she doesn't learn to ignore it. Every decision, including messages held back, is written to **What Kept checked**.

Different people, different behaviour: Kept picks a mode from the setup answers: full (irregular work), light (payroll job the state can see), exemption, student (half-time school counts on its own) or seasonal (6-month income average).

## Design decisions

- **Rules decide, the AI only helps read.** Hours, money, months, deadlines and exemptions come from a versioned rules file per state (`kept/rules/states/MT.json`). Every result carries its rules version.
- **Never invent.** Below a confidence of 0.75, Kept asks instead of logging. A value from the AI is kept only if it appears in the document's own text.
- **The human makes the final click.** Kept fills in forms; a real person signs; she submits. Kept never signs or sends anything.
- **Nothing counted twice.** A pay stub replaces the lines she typed for the same days. The same photo can't be added twice.
- **Pay periods across two months** are split by days, because the state looks at calendar months.
- **Memory keyed on what repeats.** Her routine ("pantry, Saturdays, 4h") plans the month and is re-confirmed after 60 days.
- **Private answers stay private.** Health and justice answers are stored apart and never printed unless she claims that exemption.
- **Works with the AI off.** Every AI step has a rules-only path, a time limit and checks. The screen says who read each value: Kept's rules, the AI helper, or you.

## Run it on Windows

1. Install **Python 3.11 or newer** from python.org. During setup, tick **"Add Python to PATH"**.
2. Unzip the project, open the folder, and double-click **`start.bat`**. The first time, it installs what it needs (a minute or two).
3. Open **http://localhost:8000** in your browser.
4. On your phone (same Wi-Fi), open the second link the window prints, for example `http://192.168.1.20:8000`. If Windows asks about the firewall, allow **private networks**. The camera button works in the phone's browser.

Or by hand: `python -m venv .venv`, `.venv\Scripts\activate`, `pip install -r requirements.txt`, `python run.py`.

### Reading photos: Tesseract (free, recommended)

Install it from the UB Mannheim page: https://github.com/UB-Mannheim/tesseract/wiki (keep the default folder). Kept finds it automatically. If it's elsewhere, set `KEPT_TESSERACT_CMD` to the full path of `tesseract.exe`. Without Tesseract, photos are still saved and you type the values.

### AI helper: Ollama (free, optional)

Install Ollama from https://ollama.com/download, then in a terminal: `ollama pull qwen2.5:3b` (about 2 GB). Kept detects it; you can turn it off in **More, My profile**. Works on 8 GB of RAM. Use another model with `KEPT_MODEL`.

### Run the daily check without opening the app

`python -m kept.agent.daily` (schedule it once a day with Task Scheduler). The app also runs it whenever it's opened.

## The demo

Open the app and choose **Open Keisha's demo**. Keisha is fictional: a Montana gig worker with two delivery apps, cash shifts at a corner store, Saturday volunteering and a Tuesday GED class. Her one-line texts are replayed on the days she sent them, and the agent runs for every day the clock moves. **More, Demo controls** has the four moments:

1. **October 20:** 49.5 of 80 hours, at risk. The agent plans with her own Saturday and Tuesday activities.
2. **Add proof:** a Dashly weekly statement replaces two lines she typed. Total unchanged, proof added.
3. **November 1:** October closes at 81 hours, done. The pantry page is ready for the coordinator to sign.
4. **November 25:** a letter asks for October and November proof by December 23. 28 days left, folder ready.

The sample documents in `samples/` are fictional and marked SAMPLE.

## Tests

`python -m unittest discover tests` runs 77 tests: the rules (thresholds, income path, seasonal average, pay periods across months, duplicates, months checked, deadlines, exemptions), reading (text, photos with Tesseract, letters, the AI's answers being checked), the agent (the whole demo story, weekly limits, modes) and every screen.

## What's live and what's planned

Live: everything above, Montana rules plus a federal-minimum fallback for other states, Tesseract and Ollama on your own computer, PDFs (reporting log, volunteer page, proof packet).
Planned: sending reminders by SMS or WhatsApp (they appear in the app today), Spanish, more state rule files, voice notes, forwarding earnings emails, food-stamp (SNAP) work rules.

## Project layout

```
kept/rules/engine.py      the rules: months, thresholds, exemptions, checks
kept/rules/states/*.json  one rules file per state, versioned
kept/capture/             text messages, photo reading, letters, the optional AI helper
kept/agent/watchdog.py    the daily agent: steps, ranking, weekly limit, decision log
kept/services.py          saving entries and documents, proof, sign-offs
kept/pdfs.py              printable reporting log, volunteer page, proof packet
kept/web/                 the app (Flask) and its screens
kept/demo.py              Keisha's month
tests/                    77 tests
```

## Sources

- CMS interim final rule, June 2026: https://www.hklaw.com/en/insights/publications/2026/06/cms-issues-interim-final-rule-implementing-medicaid-community
- KFF, work requirement provisions: https://www.kff.org/60c1aad
- KFF, state policy decisions (months checked): https://www.kff.org/medicaid/an-early-look-at-policy-decisions-as-states-get-ready-to-implement-work-requirements/
- CBPP, verification and the 30-day notice: https://www.cbpp.org/research/health/a-guide-to-reducing-coverage-losses-through-effective-implementation-of-medicaids
- Montana documentation list: https://dphhs.mt.gov/medicaidchanges/VerificationMatrix
- Arkansas (NEJM study): https://kffhealthnews.org/medicaid/study-arkansas-medicaid-work-requirements-hit-those-already-employed/
- Arkansas reporting data: https://www.kff.org/medicaid/issue-brief/state-data-for-medicaid-work-requirements-in-arkansas/
- Georgia: https://kffhealthnews.org/news/article/georgia-medicaid-work-requirement-red-tape/
- Louisiana pilot: https://kffhealthnews.org/news/article/medicaid-eligibility-tool-pilot-test-work-requirements-ai-louisiana-arizona-georgia/
- 1 in 5 at risk: https://www.bu.edu/sph/news/articles/2026/nearly-1-in-5-medicaid-eligible-adults-in-expansion-states-are-at-risk-of-losing-health-coverage-due-to-insufficient-or-inconsistent-work-hours/

Kept isn't affiliated with any government agency and isn't legal advice. MIT licence.
