"""In-process scheduler for the pre-market brief (runs inside the always-on web service, as user `invest`,
so adding/changing this schedule needs no root/systemd change). The brief itself runs as a separate
`python -m radar brief` process, same as a systemd job would."""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from radar import db
from radar.config import ROOT, load_config

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")
START, END = time(9, 25), time(9, 50)  # fire at 09:25; catch up until 09:50 if the service was restarting

state = {"running": False, "attempted": None}


def due(now: datetime, has_today: bool) -> bool:
    ny = now.astimezone(NY)
    return (ny.weekday() < 5 and START <= ny.time() < END and not has_today
            and state["attempted"] != ny.date() and not state["running"])


def _has_today(now: datetime) -> bool:
    cfg = load_config()
    conn = db.connect(cfg.db_path)
    try:
        rows = conn.execute("SELECT created FROM reports WHERE kind='daily' ORDER BY id DESC LIMIT 3").fetchall()
    finally:
        conn.close()
    today = now.astimezone(NY).date()
    return any(datetime.fromisoformat(r["created"]).astimezone(NY).date() == today for r in rows)


async def run_brief() -> int:
    """Run one brief now (separate process). Returns exit code; no-op if one is already running."""
    if state["running"]:
        return -1
    state["running"] = True
    state["attempted"] = datetime.now(timezone.utc).astimezone(NY).date()
    try:
        proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "radar", "brief", cwd=str(ROOT),
                                                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode:
            log.warning("brief exited %s: %s", proc.returncode, err.decode(errors="ignore")[-500:])
        return proc.returncode
    finally:
        state["running"] = False


async def spawn(job: str) -> None:
    """Run `python -m radar <job>` in the background once at a time (e.g. discover after a settings change)."""
    running = state.setdefault("jobs", set())
    if job in running:
        return
    running.add(job)
    try:
        proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "radar", job, cwd=str(ROOT),
                                                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()
    finally:
        running.discard(job)


def valuation_due(now: datetime, last: str | None) -> bool:
    """Saturday 10:00 New York, or whenever the last full refresh is over 8 days old."""
    ny = now.astimezone(NY)
    age = (now - datetime.fromisoformat(last)).total_seconds() / 86400 if last else 99
    return (ny.weekday() == 5 and ny.time() >= time(10, 0) and age > 1) or age > 8


async def run_valuation() -> None:
    if state.get("valuing"):
        return
    state["valuing"] = True
    try:
        proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "radar", "valuation", cwd=str(ROOT),
                                                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()
    finally:
        state["valuing"] = False


def _last_valuation_run() -> str | None:
    cfg = load_config()
    conn = db.connect(cfg.db_path)
    try:
        r = conn.execute("SELECT ts FROM runs WHERE job='valuation' ORDER BY ts DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    return r["ts"] if r else None


def _profiles_missing() -> bool:
    from radar import discovery, profiles

    cfg = load_config()
    conn = db.connect(cfg.db_path)
    try:
        db.apply_tracked(conn, cfg)
        _, cands = discovery.latest_candidates(conn, 60)
        return bool(profiles.missing(conn, cfg.all_tickers + [c["ticker"] for c in cands]))
    finally:
        conn.close()


def ledger_report_due(now: datetime) -> str | None:
    from radar import ledger_report
    from radar.bookkeeping import db as ldb

    if not ldb.db_path().exists():
        return None
    from radar import ledger
    if ledger.is_empty():
        return None
    ym = ledger_report.due_month(now.astimezone(ZoneInfo(load_config().tz)).replace(tzinfo=None))
    tried = state.setdefault("ledger_tried", {})
    if ym and (now - tried.get(ym, now - timedelta(days=1))).total_seconds() > 6 * 3600:  # ≤1 try per 6h
        tried[ym] = now
        return ym
    return None




def paper_due(now: datetime, last: str | None) -> bool:
    """Once a day after the New York close (any day: catches up after downtime; weekends are no-ops)."""
    ny = now.astimezone(NY)
    if ny.time() < time(16, 45):
        return False
    return last is None or datetime.fromisoformat(last).astimezone(NY).date() < ny.date()


def scan_due(now: datetime, last: str | None, behind: bool) -> bool:
    """After each close (like paper); also every ~70 min while the first DCF pass over the universe is incomplete."""
    ny = now.astimezone(NY)
    if ny.time() >= time(16, 45):
        prev = datetime.fromisoformat(last).astimezone(NY) if last else None
        if prev is None or prev.date() < ny.date() or prev.time() < time(16, 45):   # no after-close run yet today
            return True
    return behind and (last is None or (now - datetime.fromisoformat(last)).total_seconds() > 70 * 60)


def _scan_behind() -> bool:
    """Only while a market-wide portfolio account exists and fair values are still missing."""
    from radar import scan
    cfg = load_config()
    conn = db.connect(cfg.db_path)
    try:
        if not any(a["active"] for a in scan.accounts(conn)):
            return False
        c = scan.coverage(conn)
        return c["universe"] == 0 or c["valued"] < c["universe"] * 0.97
    finally:
        conn.close()


def any_port_accounts() -> bool:
    from radar import scan
    cfg = load_config()
    conn = db.connect(cfg.db_path)
    try:
        return any(a["active"] for a in scan.accounts(conn))
    finally:
        conn.close()


def news_due(now: datetime, last: str | None, every_min: float) -> bool:
    return last is None or (now - datetime.fromisoformat(last)).total_seconds() >= every_min * 60


def reddit_due(now: datetime, last: str | None, every_hours: float) -> bool:
    return last is None or (now - datetime.fromisoformat(last)).total_seconds() >= every_hours * 3600


def _last_run(job: str) -> str | None:
    """Last attempt (ok or not) — a failing feed shouldn't be retried every 30 s."""
    cfg = load_config()
    conn = db.connect(cfg.db_path)
    try:
        r = conn.execute("SELECT MAX(ts) AS ts FROM runs WHERE job=?", (job,)).fetchone()
    finally:
        conn.close()
    return r["ts"] if r else None


async def spawn_args(*args: str) -> None:
    proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "radar", *args, cwd=str(ROOT),
                                                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await proc.wait()


async def loop() -> None:
    while True:
        try:
            now = datetime.now(timezone.utc)
            if due(now, _has_today(now)):
                log.info("starting pre-market brief")
                asyncio.create_task(run_brief())
            if _profiles_missing() and (now - state.get("profiles_at", now - timedelta(hours=1))).total_seconds() > 600:
                state["profiles_at"] = now
                asyncio.create_task(spawn("profiles"))
            every = float(load_config().reddit.get("every_hours", 4) or 0)
            if (every and "reddit" not in state.get("jobs", set()) and reddit_due(now, _last_run("reddit"), every)
                    and (now - state.get("reddit_at", now - timedelta(hours=1))).total_seconds() > 1800):
                state["reddit_at"] = now  # ≤1 start per 30 min even if the job dies before recording a run
                log.info("starting reddit fetch")
                asyncio.create_task(spawn("reddit"))
            news_every = float(load_config().news.get("every_min", 3) or 0)
            if (news_every and "news" not in state.get("jobs", set()) and news_due(now, _last_run("news"), news_every)
                    and (now - state.get("news_at", now - timedelta(hours=1))).total_seconds() >= news_every * 60):
                state["news_at"] = now
                asyncio.create_task(spawn("news"))
            if ("paper" not in state.get("jobs", set()) and paper_due(now, _last_run("paper"))
                    and (now - state.get("paper_at", now - timedelta(hours=1))).total_seconds() > 1800):
                state["paper_at"] = now
                log.info("stepping paper accounts")
                asyncio.create_task(spawn("paper"))
            if ("scan" not in state.get("jobs", set()) and any_port_accounts()
                    and scan_due(now, _last_run("scan"), _scan_behind())
                    and (now - state.get("scan_at", now - timedelta(hours=2))).total_seconds() > 3600):
                state["scan_at"] = now
                log.info("market-wide scan")
                asyncio.create_task(spawn("scan"))
            if not state.get("valuing") and valuation_due(now, _last_valuation_run()):
                log.info("starting weekly valuation refresh")
                asyncio.create_task(run_valuation())
            if ym := ledger_report_due(now):
                log.info("starting ledger report %s", ym)
                asyncio.create_task(spawn_args("ledger_report", ym))
        except Exception:
            log.exception("scheduler tick failed")
        await asyncio.sleep(30)
