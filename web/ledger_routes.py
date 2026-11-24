"""Ledger panel routes. Everything under /ledger sits behind its own password (on top of Cloudflare Access)."""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from datetime import date
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse

from radar import ledger

router = APIRouter(prefix="/ledger")
COOKIE = "ledger_s"
_fails: list[float] = []  # recent wrong-password timestamps (single-user app → global limiter)
MAX_FAILS, WINDOW = 5, 600


def _render(request, name, ctx):
    from web.app import templates
    r = templates.TemplateResponse(request, name, {**ctx, "active": "ledger", "sync_age": ledger.sync_age_days(), "me": ledger.ME,
                                                      "due": ledger.due_tasks() if ctx.get("mode") is None else []})
    r.headers["Cache-Control"] = "no-store"
    return r


def _unlocked(request: Request) -> bool:
    if not ledger.db.db_path().exists():
        ledger.ensure()
    return ledger.has_password() and ledger.valid_token(request.cookies.get(COOKIE))


def _gate(request: Request, error: str = ""):
    mode = "login" if ledger.has_password() else "setup"
    return _render(request, "ledger_gate.html", {"mode": mode, "error": error})


def _secure(request: Request) -> bool:
    return request.headers.get("x-forwarded-proto") == "https" or "cf-connecting-ip" in request.headers


def _session(request: Request, to: str = "/ledger"):
    r = RedirectResponse(to, 303)
    r.set_cookie(COOKIE, ledger.make_token(), max_age=ledger.SESSION_HOURS * 3600, path="/ledger",
                 httponly=True, samesite="strict", secure=_secure(request))
    return r


def _locked_out() -> bool:
    now = time.time()
    _fails[:] = [t for t in _fails if now - t < WINDOW]
    return len(_fails) >= MAX_FAILS


@router.post("/setup")
async def setup(request: Request, pw: str = Form(""), pw2: str = Form("")):
    ledger.ensure()
    if ledger.has_password():
        raise HTTPException(403, "Password already set")
    if len(pw) < 8:
        return _gate(request, "Password must be at least 8 characters")
    if pw != pw2:
        return _gate(request, "The two entries don’t match")
    ledger.set_password(pw)
    return _session(request)


@router.post("/login")
async def login(request: Request, pw: str = Form("")):
    ledger.ensure()
    if _locked_out():
        return _gate(request, "Too many wrong attempts, try again in 10 minutes")
    if not await asyncio.to_thread(ledger.check_password, pw):
        _fails.append(time.time())
        return _gate(request, f"Wrong password ( {MAX_FAILS - len(_fails)} tries left)")
    _fails.clear()
    return _session(request)


@router.post("/logout")
def logout():
    r = RedirectResponse("/ledger", 303)
    r.delete_cookie(COOKIE, path="/ledger")
    return r


@router.post("/password")
async def change_password(request: Request, old: str = Form(""), pw: str = Form(""), pw2: str = Form("")):
    if not _unlocked(request):
        return _gate(request)
    err = ("Old password is wrong" if not ledger.check_password(old) else "New password must be at least 8 characters" if len(pw) < 8
           else "The two entries don’t match" if pw != pw2 else "")
    if err:
        return RedirectResponse(f"/ledger/sync?pwerr={err}", 303)
    ledger.set_password(pw)  # rotates the session secret → other devices are logged out
    return _session(request, "/ledger/sync?pwok=1")


@router.post("/bootstrap")
async def bootstrap(request: Request):
    """One-time: load the old Mac app's bookkeeping.db. Only while the ledger is empty and has no password."""
    ledger.ensure()
    if ledger.has_password() or not ledger.is_empty():
        raise HTTPException(403, "ledger already initialised")
    try:
        n = await asyncio.to_thread(ledger.restore_db, await request.body())
    except Exception as e:  # not SQLite, no transactions table, ...
        raise HTTPException(400, f"bad database: {e}") from e
    return {"transactions": n}


@router.get("")
def overview(request: Request, m: str = "", y: str = ""):
    ledger.ensure()
    if not _unlocked(request):
        return _gate(request)
    today = ledger.local_today()
    if y:
        years = _years()
        yy = int(y) if y.isdigit() else years[0]
        return _render(request, "ledger.html", {"tab": "year", "v": ledger.year_insights(yy), "years": years,
                                                "st": ledger.stats()})
    st = ledger.stats()
    latest = min(today.strftime("%Y-%m"), (st["last"] or today.isoformat())[:7])  # last month that has data
    ym = m if len(m) == 7 and m[4] == "-" else latest
    v = ledger.month_view(ym)
    return _render(request, "ledger.html", {"tab": "month", "v": v, "st": st, "years": _years(),
                                            "prev": ledger._prev_month(ym), "next": _next(ym),
                                            "is_current": ym == today.strftime("%Y-%m")})


def _next(ym: str) -> str:
    y, mm = map(int, ym.split("-"))
    return f"{y + 1}-01" if mm == 12 else f"{y}-{mm + 1:02d}"


def _years() -> list[int]:
    r = ledger.db.query_one("SELECT MIN(substr(occurred_at,1,4)) AS a, MAX(substr(occurred_at,1,4)) AS b "
                            "FROM transactions WHERE deleted_at IS NULL")
    if not r or not r["a"]:
        return [ledger.local_today().year]
    return list(range(int(r["b"]), int(r["a"]) - 1, -1))


@router.get("/tx")
def tx(request: Request, q: str = "", m: str = "", type: str = "", cat: str = "", account: str = ""):
    if not _unlocked(request):
        return _gate(request)
    d0 = d1 = None
    if len(m) == 7:
        d0, d1 = ledger._month_bounds(m)
    elif len(m) == 4 and m.isdigit():
        d0, d1 = f"{m}-01-01", f"{int(m) + 1}-01-01"
    rows = ledger.transactions(q=q.strip(), d0=d0, d1=d1, type_=type or None, cat=cat or None,
                               account=account or None, limit=300)
    return _render(request, "ledger_tx.html", {"rows": rows, "f": {"q": q, "m": m, "type": type, "cat": cat,
                                                                    "account": account},
                                               "types": ledger.TYPE_ZH})


@router.get("/accounts")
def accounts(request: Request):
    if not _unlocked(request):
        return _gate(request)
    rows = ledger.balances()
    cutoff = ledger.local_today().replace(year=ledger.local_today().year - 2).isoformat()
    owners: dict[str, list] = {}
    for r in rows:
        if (r["last"] or "") >= cutoff:
            owners.setdefault(r["owner"] or "Shared/unassigned", []).append(r)
    idle = [r for r in rows if (r["last"] or "") < cutoff]
    return _render(request, "ledger_accounts.html", {"owners": owners, "idle": idle, "rates": ledger.usd_rates()})


@router.get("/sync")
def sync_page(request: Request, pwerr: str = "", pwok: str = ""):
    if not _unlocked(request):
        return _gate(request)
    return _render(request, "ledger_sync.html", {"imports": ledger.imports(), "st": ledger.stats(),
                                                 "pwerr": pwerr, "pwok": pwok, "result": None})


@router.post("/sync")
async def sync_upload(request: Request, file: UploadFile = File(...), force: str = Form("")):
    if not _unlocked(request):
        return _gate(request)
    suffix = Path(file.filename or "").suffix.lower()
    if suffix != ".csv":
        result = {"error": "Please upload a CSV file (.csv); see the column list on this page"}
    else:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            shutil.copyfileobj(file.file, tmp)
            path = Path(tmp.name)
        try:
            result = await asyncio.to_thread(ledger.sync, path, file.filename, force == "1")
        except Exception as e:  # malformed file etc.
            result = {"error": f"Import failed: {type(e).__name__}: {e}"[:300]}
        finally:
            path.unlink(missing_ok=True)
    return _render(request, "ledger_sync.html", {"imports": ledger.imports(), "st": ledger.stats(),
                                                 "pwerr": "", "pwok": "", "result": result,
                                                 "filename": file.filename})


@router.post("/demo")
async def demo(request: Request):
    """Fill an empty ledger with the fictional demo household (radar/ledger_demo.py). Refused once there is data."""
    if not _unlocked(request):
        return _gate(request)
    from radar import ledger_demo
    try:
        await asyncio.to_thread(ledger_demo.load)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    return RedirectResponse("/ledger", 303)


@router.get("/todo")
def todo(request: Request, edit: int = 0):
    if not _unlocked(request):
        return _gate(request)
    return _render(request, "ledger_todo.html", {"tasks": ledger.tasks(), "edit": edit,
                                                 "ym": ledger.local_today().strftime("%Y-%m")})


def _task_fields(title: str, day: str) -> tuple[str, int]:
    title = title.strip()[:100]
    d = int(day) if day.strip().isdigit() else 10
    if not title or not 1 <= d <= 28:
        raise HTTPException(400, "Needs a title and a day 1–28")
    return title, d


@router.post("/todo")
async def todo_add(request: Request, title: str = Form(""), note: str = Form(""), day: str = Form("10"),
                   m: list[int] = Form([])):
    if not _unlocked(request):
        return _gate(request)
    t, d = _task_fields(title, day)
    ledger.add_task(t, note.strip()[:500], d, m)
    return RedirectResponse("/ledger/todo", 303)


@router.post("/todo/{tid}")
async def todo_update(request: Request, tid: int, title: str = Form(""), note: str = Form(""), day: str = Form("10"),
                      m: list[int] = Form([])):
    if not _unlocked(request):
        return _gate(request)
    t, d = _task_fields(title, day)
    ledger.update_task(tid, t, note.strip()[:500], d, m)
    return RedirectResponse("/ledger/todo", 303)


@router.post("/todo/{tid}/delete")
def todo_delete(request: Request, tid: int):
    if not _unlocked(request):
        return _gate(request)
    ledger.delete_task(tid)
    return RedirectResponse("/ledger/todo", 303)


@router.post("/todo/{tid}/done")
async def todo_done(request: Request, tid: int, back: str = Form("/ledger/todo")):
    if not _unlocked(request):
        return _gate(request)
    ledger.toggle_done(tid)
    return RedirectResponse(back if back.startswith("/ledger") else "/ledger/todo", 303)


@router.get("/reports")
def reports_page(request: Request):
    if not _unlocked(request):
        return _gate(request)
    from radar import ledger_report
    return _render(request, "ledger_reports.html", {"reports": ledger_report.reports(), "report": None,
                                                    "running": _report_running()})


@router.get("/reports/{rid}")
def report_page(request: Request, rid: int):
    if not _unlocked(request):
        return _gate(request)
    from radar import ledger_report
    from web.app import _md
    r = ledger_report.get(rid)
    if not r:
        raise HTTPException(404)
    return _render(request, "ledger_reports.html", {"reports": ledger_report.reports(), "report": r,
                                                    "html": _md(r["markdown"]),
                                                    "raw": _md(r["bundle"]) if r.get("bundle") else None, "running": _report_running(),
                                                    "checks": ledger_report.checks(rid), "status_zh": ledger_report.STATUS_ZH})


@router.post("/reports/{rid}/checks/{cid}")
async def check_save(request: Request, rid: int, cid: int, reply: str = Form(""), status: str = Form("open")):
    if not _unlocked(request):
        return _gate(request)
    from radar import ledger_report
    if not any(c["id"] == cid for c in ledger_report.checks(rid)):
        raise HTTPException(404)
    ledger_report.save_check(cid, reply, status)
    return RedirectResponse(f"/ledger/reports/{rid}#c{cid}", 303)


_report_task: dict = {}


def _report_running() -> bool:
    t = _report_task.get("t")
    return bool(t and not t.done())


@router.post("/reports/run")
async def report_run(request: Request, ym: str = Form("")):
    if not _unlocked(request):
        return _gate(request)
    import re
    if not re.fullmatch(r"\d{4}-\d{2}", ym):
        ym = ledger.local_today().strftime("%Y-%m")
    if not _report_running():
        from web import scheduler
        _report_task["t"] = asyncio.create_task(scheduler.spawn_args("ledger_report", ym))
    return RedirectResponse("/ledger/reports", 303)


@router.post("/agent")
async def ledger_agent(request: Request):
    """Same chat as /agent, but with ledger context; only when unlocked (cookie path=/ledger)."""
    import re as _re

    from fastapi.responses import StreamingResponse

    from radar import agent, ledger_agent
    from radar.config import load_config
    from web.app import _agent_slots, _sse

    if not _unlocked(request):
        raise HTTPException(403, "Ledger is locked")
    body = await request.json()
    q = str(body.get("q") or "").strip()[:4000]
    sid = body.get("sid") or None
    if sid is not None and not _re.fullmatch(r"[0-9a-f\-]{36}", str(sid)):
        sid = None
    page = str(body.get("page") or "/ledger")[:200]
    if not q:
        raise HTTPException(400, "empty question")
    rid = ledger_agent.target_report(page)
    prompt = ledger_agent.context(page) + "\n\n# Question\n" + q
    argv = agent.agent_argv(sid)
    workdir = load_config().db_path.parent / "agent"

    async def gen():
        text = ""
        if _agent_slots.locked():
            yield _sse({"t": "status", "v": "Queued…"})
        async with _agent_slots:
            async for ev in agent.run(argv, prompt, workdir):
                if await request.is_disconnected():
                    return
                if ev["t"] == "text":
                    text += ev["v"]
                yield _sse(ev)
        saved = ledger_agent.apply_replies(text, rid)
        if saved:
            yield _sse({"t": "saved", "v": saved})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------- Financial independence ----------------

DEFAULT_ITEMS = [(f"Brokerage account ({ledger.ME})", ledger.ME), ("Household investment account", "Household"), ("Retirement accounts (401k / RRSP etc.)", "Household"),
                 ("Cash / deposits", "Household")]


@router.get("/fi")
def fi_page(request: Request, saved: str = ""):
    if not _unlocked(request):
        return _gate(request)
    from radar import fi
    v = fi.view()
    ym = ledger.local_today().strftime("%Y-%m")
    rows = fi.items(ym) or (fi.items(v["cur_ym"]) if v["cur_ym"] else [])
    prefilled = False
    if not rows:
        rows = fi.ledger_assets() + [{"item": "Cash / deposits", "owner": "Household", "currency": "USD", "amount": None}]
        prefilled = len(rows) > 1
    return _render(request, "ledger_fi.html", {"v": v, "ym": ym, "rows": rows, "saved": saved, "prefilled": prefilled,
                                               "has_this_month": bool(fi.items(ym))})


def _num(x: str) -> float | None:
    x = (x or "").replace(",", "").replace("$", "").strip()
    try:
        return float(x) if x else None
    except ValueError:
        return None


@router.post("/fi/snapshot")
async def fi_snapshot(request: Request):
    if not _unlocked(request):
        return _gate(request)
    from radar import fi
    form = await request.form()
    ym = str(form.get("ym") or "")
    import re as _re
    if not _re.fullmatch(r"\d{4}-\d{2}", ym):
        raise HTTPException(400, "Month format YYYY-MM")
    rows, seen = [], set()
    for name, owner, ccy, amt in zip(form.getlist("item"), form.getlist("owner"), form.getlist("currency"),
                                     form.getlist("amount")):
        name, a = str(name).strip()[:60], _num(str(amt))
        if not name or a is None or name in seen:
            continue
        seen.add(name)
        rows.append({"item": name, "owner": ledger.ME if owner == ledger.ME else "Household",
                     "currency": ccy if ccy in ("USD", "CAD", "CNY", "EUR", "HKD") else "USD", "amount": a})
    fi.save_snapshot(ym, rows)
    return RedirectResponse("/ledger/fi?saved=1", 303)


@router.post("/fi/settings")
async def fi_settings(request: Request):
    if not _unlocked(request):
        return _gate(request)
    from radar import fi
    form = await request.form()
    cfg = fi.settings()
    pct = lambda k, lo, hi: (lambda n: n / 100 if n is not None and lo <= n <= hi else cfg[k])(_num(str(form.get(k) or "")))
    cfg["wr"] = pct("wr", 2, 8)
    cfg["real_return"] = pct("real_return", 0, 10)
    cfg["health"] = _num(str(form.get("health") or "")) or 0.0
    auto = fi.auto_spending()
    changed = lambda n, a: n is not None and abs(n - a) >= 1  # fields are prefilled: only real edits become overrides
    cfg["override"] = {k: n for k, _ in fi.BUCKETS if k != "health"
                       and changed(n := _num(str(form.get("o_" + k) or "")), auto["buckets"][k])}
    n = _num(str(form.get("savings_override") or ""))
    cfg["savings_override"] = n if changed(n, max(auto["savings_monthly"], 0)) else None

    cfg["mortgage_balance"] = _num(str(form.get("mortgage_balance") or ""))
    fi.save_settings(cfg)
    return RedirectResponse("/ledger/fi?saved=1", 303)


@router.post("/fi/goal")
async def fi_goal_save(request: Request):
    if not _unlocked(request):
        return _gate(request)
    from radar import fi
    import re as _re
    form = await request.form()
    name = str(form.get("name") or "").strip()[:60]
    if not name:
        raise HTTPException(400, "A goal needs a name")
    gid = int(form.get("id")) if str(form.get("id") or "").isdigit() else None
    dl = str(form.get("deadline") or "").strip()
    dl = dl if _re.fullmatch(r"\d{4}-\d{2}", dl) else None
    m = _num(str(form.get("monthly") or ""))
    if gid and m is not None and abs(m - fi.auto_yue_monthly()) < 1 and \
            next((g for g in fi.goal_rows() if g["id"] == gid), {}).get("monthly") is None:
        m = None  # prefilled auto value posted back unchanged
    fi.save_goal(gid, name, _num(str(form.get("target") or "")), m, dl, [str(x) for x in form.getlist("items")])
    return RedirectResponse("/ledger/fi?saved=1#goals", 303)


@router.post("/fi/goal/{gid}/delete")
def fi_goal_delete(request: Request, gid: int):
    if not _unlocked(request):
        return _gate(request)
    from radar import fi
    fi.delete_goal(gid)
    return RedirectResponse("/ledger/fi#goals", 303)
