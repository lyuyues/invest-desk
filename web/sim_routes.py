"""策略实验室: /sim (backtest + rule editor) and /paper (live paper accounts). Educational, no real orders."""
from __future__ import annotations

import asyncio
import json
import math
from datetime import date, datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from radar import backtest, db, paper
from radar.config import load_config
from radar.notes import TICKER_RE

router = APIRouter()
COLORS = ["var(--muted)", "var(--accent)", "#d97706", "#8b5cf6", "#0d9488", "#db2777"]


def _render(request, name, ctx, active="sim"):
    from web.app import templates
    return templates.TemplateResponse(request, name, {**ctx, "active": active})


def _conn():
    return db.connect(load_config().db_path)


def chart(lines: list[dict], dates: list[date], w: int = 720, h: int = 220, log: bool = True,
          marks: list[dict] | None = None) -> dict:
    """Server-side SVG line chart. lines: [{"label", "color", "values"}]; marks: [{"i", "v", "side"}]."""
    f = (lambda v: math.log(v)) if log else (lambda v: v)
    vals = [f(v) for ln in lines for v in ln["values"] if v and v > 0]
    if len(dates) < 2 or not vals:
        return {}
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.05 or 0.1
    lo, hi = lo - pad, hi + pad
    n = len(dates)
    X = lambda i: round(i * w / (n - 1), 1)
    Y = lambda v: round(h - (f(v) - lo) / (hi - lo) * h, 1)
    out = {"w": w, "h": h, "lines": [], "yt": [], "xt": [], "marks": []}
    step = max(1, n // 900)   # thin long series; always keep the last point
    for ln in lines:
        idx = list(range(0, n, step)) + ([n - 1] if (n - 1) % step else [])
        out["lines"].append({**ln, "pts": " ".join(f"{X(i)},{Y(ln['values'][i])}" for i in idx)})
    raw_lo, raw_hi = (math.exp(lo), math.exp(hi)) if log else (lo, hi)
    ticks = _nice_ticks(raw_lo, raw_hi, log)
    out["yt"] = [{"y": Y(t), "label": _tick_label(t)} for t in ticks if raw_lo <= t <= raw_hi]
    seen = set()
    for i, d in enumerate(dates):
        key = d.year if (dates[-1] - dates[0]).days > 600 else (d.year, d.month)
        if key not in seen:
            seen.add(key)
            if i > n * 0.03:
                out["xt"].append({"x": X(i), "label": str(d.year) if isinstance(key, int) else f"{d.month}月"})
    if len(out["xt"]) > 12:
        out["xt"] = out["xt"][::math.ceil(len(out["xt"]) / 12)]
    for m in marks or []:
        out["marks"].append({"x": X(m["i"]), "y": Y(m["v"]), "side": m["side"], "tip": m.get("tip", "")})
    return out


def _nice_ticks(lo, hi, log):
    if log and hi / lo > 3:
        out, v = [], 10 ** math.floor(math.log10(lo))
        while v <= hi:
            for m in (1, 2, 5):
                out.append(v * m)
            v *= 10
        return out
    span = hi - lo
    step = 10 ** math.floor(math.log10(span / 4)) if span > 0 else 1
    for m in (1, 2, 5, 10):
        if span / (step * m) <= 6:
            step *= m
            break
    start = math.ceil(lo / step) * step
    return [start + i * step for i in range(int((hi - start) / step) + 1)]


def _tick_label(v):
    return f"{v:,.0f}" if v >= 100 else f"{v:.2f}".rstrip("0").rstrip(".") if v < 10 else f"{v:.0f}"


def _f(v, lo, hi, default):
    try:
        x = float(v)
        return min(hi, max(lo, x))
    except (TypeError, ValueError):
        return default


# ---------- backtest ----------

@router.get("/sim")
async def sim_page(request: Request):
    qp = request.query_params
    ticker = (qp.get("ticker") or "SPY").upper().strip()
    years = _f(qp.get("years"), 1, 30, 10)
    cost_bps = _f(qp.get("cost"), 0, 200, 10)
    split = _f(qp.get("split"), 30, 95, 70)
    chosen = qp.getlist("s") or ["p:trend", "p:chase"]
    conn = _conn()
    try:
        strategies = paper.list_strategies(conn)
    finally:
        conn.close()
    by_id = {s["id"]: s for s in strategies}
    chosen = [c for c in chosen if c in by_id][:5]
    ctx = {"ticker": ticker, "years": years, "cost": cost_bps, "split": split, "chosen": chosen,
           "strategies": strategies, "describe": backtest.describe, "error": None, "res": None}
    if not TICKER_RE.match(ticker):
        ctx["error"] = "股票代码格式不对"
        return _render(request, "sim.html", ctx)
    try:
        dates, closes = await asyncio.to_thread(backtest.fetch_history, ticker, years)
    except Exception as e:
        dates, closes, ctx["error"] = [], [], f"拉取 {ticker} 历史价格失败：{e}"
    if not ctx["error"] and len(dates) < 60:
        ctx["error"] = f"{ticker} 的历史数据太少（{len(dates)} 天）"
    if ctx["error"]:
        return _render(request, "sim.html", ctx)
    val_only = [by_id[c]["spec"]["name"] for c in chosen if backtest.uses_valuation(by_id[c]["spec"])]
    if val_only:
        ctx["val_note"] = (f"「{'」「'.join(val_only)}」用了估值 / 市值条件，没法回测：只有今天的 DCF 合理价和市值，没有历史数据，"
                           "拿今天的数字去判断过去的买卖等于偷看未来。请直接开一个「策略模拟」往前跑。")
    specs = [(c, by_id[c]["spec"]) for c in chosen if not backtest.uses_valuation(by_id[c]["spec"])]
    res = await asyncio.to_thread(backtest.run, dates, closes, specs, cost_bps / 10000, split / 100)
    rows = []
    for i, r in enumerate(res["results"]):
        color = COLORS[i % len(COLORS)]
        marks = []
        if r.key != "hold":
            idx = {d: j for j, d in enumerate(dates)}
            for t in r.trades:
                marks.append({"i": idx[t.entry], "v": t.entry_px, "side": "buy", "tip": f"{t.entry} 买 {t.entry_px:.2f}"})
                if t.exit:
                    marks.append({"i": idx[t.exit], "v": t.exit_px, "side": "sell", "tip": f"{t.exit} 卖 {t.exit_px:.2f}"})
        sens = r.sens
        rows.append({"r": r, "color": color, "name": r.spec["name"],
                     "desc": backtest.describe(r.spec) if r.key != "hold" else None,
                     "price_chart": chart([{"label": ticker, "color": "var(--muted)", "values": closes}], dates, h=150,
                                          marks=marks) if r.key != "hold" else None,
                     "robust": (sum(x["out"] > 0 for x in sens), len(sens)) if sens else None,
                     "excess": r.m["cagr"] - res["hold"]["cagr"], "excess_out": r.m_out["cagr"] - res["hold_out"]["cagr"]})
    eq_chart = chart([{"label": x["name"], "color": x["color"], "values": x["r"].equity} for x in rows], dates)
    eq_chart["cut_x"] = round(res["cut"] * eq_chart["w"] / (len(dates) - 1), 1) if eq_chart else None
    ctx.update(res=res, rows=rows, eq=eq_chart, first=dates[0], last=dates[-1], n=len(dates))
    return _render(request, "sim.html", ctx)


@router.get("/sim/edit")
def sim_edit(request: Request, id: str = "", src: str = "p:trend"):
    conn = _conn()
    try:
        cur = paper.get_strategy(conn, id) if id else None
        base = cur or paper.get_strategy(conn, src) or paper.get_strategy(conn, "p:trend")
    finally:
        conn.close()
    if id and not cur:
        raise HTTPException(404)
    spec = dict(base["spec"])
    if not cur:
        spec["name"] = spec["name"] + "（我的版本）"
    types = {k: {"label": v[0], "params": [{"name": p[0], "label": p[1], "default": p[2], "kind": p[3]} for p in v[1]],
                 "pos": v[2]} for k, v in backtest.CONDS.items()}
    return _render(request, "sim_edit.html", {"sid": id if cur and not cur["preset"] else "", "spec": spec,
                                               "types_json": json.dumps(types, ensure_ascii=False),
                                               "spec_json": json.dumps(spec, ensure_ascii=False)})


@router.post("/sim/strategy")
async def sim_save(request: Request):
    body = await request.json()
    conn = _conn()
    try:
        sid = paper.save_strategy(conn, body.get("spec") or {}, datetime.now(timezone.utc), body.get("id") or None)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    finally:
        conn.close()
    return {"id": sid}


@router.post("/sim/strategy/{sid}/delete")
def sim_delete(sid: str):
    if not sid.isdigit():
        raise HTTPException(400)
    conn = _conn()
    try:
        paper.delete_strategy(conn, sid, datetime.now(timezone.utc))
    finally:
        conn.close()
    return RedirectResponse("/sim", status_code=303)


# ---------- paper accounts ----------

@router.get("/paper")
def paper_page(request: Request, s: str = "", ticker: str = "", err: str = ""):
    conn = _conn()
    try:
        rows = paper.accounts(conn)
        last = {r["account_id"]: r for r in conn.execute(
            "SELECT e.* FROM paper_equity e JOIN (SELECT account_id, MAX(date) d FROM paper_equity GROUP BY account_id) m "
            "ON e.account_id=m.account_id AND e.date=m.d")}
        strategies = paper.list_strategies(conn)
        fills = {r["account_id"]: r["n"] for r in conn.execute(
            "SELECT account_id, COUNT(*) n FROM paper_fills GROUP BY account_id")}
    finally:
        conn.close()
    accts = []
    for a in rows:
        e = last.get(a["id"])
        accts.append({"a": a, "equity": e["equity"] if e else a["capital"], "hold": e["hold"] if e else a["capital"],
                      "price": e["price"] if e else None, "fills": fills.get(a["id"], 0)})
    from radar import scan
    conn = _conn()
    try:
        ports = []
        for p in scan.accounts(conn):
            e = conn.execute("SELECT * FROM port_equity WHERE account_id=? ORDER BY date DESC LIMIT 1", (p["id"],)).fetchone()
            ports.append({"a": p, "equity": e["equity"] if e else p["capital"], "bench": e["bench"] if e else None,
                          "n_pos": e["n_pos"] if e else 0})
        cov = scan.coverage(conn)
    finally:
        conn.close()
    port_strats = []
    for st in strategies:
        try:
            scan.check_spec(st["spec"])
            port_strats.append(st)
        except ValueError:
            pass
    return _render(request, "paper.html", {"accts": accts, "strategies": strategies, "pick": s,
                                            "ticker": ticker.upper(), "err": err, "ports": ports, "cov": cov,
                                            "port_strats": port_strats}, "paper")


@router.post("/paper")
async def paper_open(request: Request):
    from urllib.parse import parse_qs
    f = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
    ticker = (f.get("ticker") or "").upper().strip()
    if not TICKER_RE.match(ticker):
        raise HTTPException(400, "bad ticker")
    capital = _f(f.get("capital"), 100, 1e9, 10000)
    cost = _f(f.get("cost"), 0, 200, 10) / 10000
    conn = _conn()
    try:
        st = paper.get_strategy(conn, f.get("strategy", ""))
        if not st:
            raise HTTPException(400, "bad strategy")
        aid = paper.open_account(conn, st, ticker, capital, cost, datetime.now(timezone.utc))
    except ValueError as e:
        from urllib.parse import quote
        return RedirectResponse(f"/paper?s={quote(f.get('strategy', ''))}&ticker={ticker}&err={quote(str(e))}", status_code=303)
    finally:
        conn.close()
    return RedirectResponse(f"/paper/{aid}", status_code=303)


def _step_one(aid: int) -> str:
    conn = _conn()
    try:
        from radar.__main__ import _pusher
        return paper.run_job(conn, datetime.now(timezone.utc), _pusher(load_config()), only=aid)
    finally:
        conn.close()


@router.post("/paper/{aid}/refresh")
async def paper_refresh(aid: int):
    await asyncio.to_thread(_step_one, aid)
    return RedirectResponse(f"/paper/{aid}", status_code=303)


@router.post("/paper/{aid}/stop")
def paper_stop(aid: int):
    conn = _conn()
    try:
        paper.stop_account(conn, aid, datetime.now(timezone.utc))
    finally:
        conn.close()
    return RedirectResponse(f"/paper/{aid}", status_code=303)


@router.get("/paper/{aid}")
async def paper_detail(request: Request, aid: int):
    conn = _conn()
    try:
        got = paper.account(conn, aid)
    finally:
        conn.close()
    if not got:
        raise HTTPException(404)
    a, fills, eq = got
    spec = json.loads(a["spec"])
    live, live_err = None, None
    conn = _conn()
    try:
        fair = paper.fair_for(conn, a["ticker"], spec)
    finally:
        conn.close()
    if a["active"]:
        try:
            dates, closes = await asyncio.to_thread(
                backtest.fetch_history, a["ticker"], paper.history_years(spec, date.fromisoformat(a["start"])), 300)
            live = paper.preview(spec, a, dates, closes, datetime.now(timezone.utc), fair,
                                 await asyncio.to_thread(paper.mcap_for, a["ticker"], spec))
        except Exception as e:
            live_err = str(e)
    eq_chart = None
    if len(eq) >= 2:
        ds = [date.fromisoformat(r["date"]) for r in eq]
        idx = {r["date"]: i for i, r in enumerate(eq)}
        marks = [{"i": idx[f["date"]], "v": f["price"], "side": f["side"]} for f in fills if f["date"] in idx]
        eq_chart = chart([{"label": "策略", "color": "var(--accent)", "values": [r["equity"] for r in eq]},
                          {"label": "买入持有", "color": "var(--muted)", "values": [r["hold"] for r in eq]}], ds, log=False)
        price_chart = chart([{"label": a["ticker"], "color": "var(--muted)", "values": [r["price"] for r in eq]}], ds,
                            log=False, marks=marks)
    else:
        price_chart = None
    cur = eq[-1] if eq else None
    return _render(request, "paper_detail.html", {
        "a": a, "spec": spec, "desc": backtest.describe(spec), "fills": fills, "eq": eq_chart, "price_chart": price_chart,
        "cur": cur, "live": live, "live_err": live_err, "n_days": len(eq), "fair": fair,
        "uses_val": backtest.uses_valuation(spec)}, "paper")


# ---------- market-wide portfolio accounts (/scan) ----------

@router.post("/scan")
async def scan_open(request: Request):
    from urllib.parse import parse_qs, quote
    from radar import scan, trade
    f = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
    capital = _f(f.get("capital"), 1000, 1e9, 100000)
    cost = _f(f.get("cost"), 0, 200, 10) / 10000
    max_pos = int(_f(f.get("max_pos"), 1, 50, 10))
    try:
        spy = await asyncio.to_thread(trade.live_quote, "SPY")
    except Exception:
        spy = None
    def go():
        conn = _conn()
        try:
            st = paper.get_strategy(conn, f.get("strategy", ""))
            if not st:
                raise ValueError("请选一个策略")
            return scan.open_account(conn, st, capital, cost, max_pos, datetime.now(timezone.utc), spy)
        finally:
            conn.close()
    try:
        pid = await asyncio.to_thread(go)
    except ValueError as e:
        return RedirectResponse("/paper?err=" + quote(str(e)) + "#scan", status_code=303)
    from web import scheduler
    asyncio.create_task(scheduler.spawn("scan"))      # start building the universe / fair values right away
    return RedirectResponse(f"/scan/{pid}", status_code=303)


@router.post("/scan/run")
async def scan_run():
    from web import scheduler
    asyncio.create_task(scheduler.spawn("scan"))
    return RedirectResponse("/paper#scan", status_code=303)


def _newer_strategy(conn, a, spec):
    """The saved strategy, when it was edited after this account copied it."""
    st = paper.get_strategy(conn, str(a["strategy_id"])) if a["strategy_id"] else None
    if st and a["active"] and json.dumps(st["spec"], sort_keys=True) != json.dumps(spec, sort_keys=True):
        return {"name": st["spec"]["name"], "desc": backtest.describe(st["spec"])}
    return None


def _scan_view(pid: int):
    from radar import scan
    conn = _conn()
    try:
        a = scan.account(conn, pid)
        if not a:
            return None
        spec = json.loads(a["spec"])
        uni = {r["ticker"]: r for r in conn.execute("SELECT * FROM scan_universe")}
        fairs = scan.fair_map(conn)
        pos = []
        for p in scan.positions(conn, pid):
            u = uni.get(p["ticker"])
            px = u["price"] if u else p["entry_px"]
            fair = fairs.get(p["ticker"])
            pos.append({**dict(p), "name": u["name"] if u else "", "px": px, "value": p["shares"] * px,
                        "pnl": (px / p["entry_px"] - 1), "fair": fair, "upside": (fair / px - 1) * 100 if fair else None})
        pending = conn.execute("SELECT * FROM port_pending WHERE account_id=? ORDER BY side DESC, ticker", (pid,)).fetchall()
        fills = conn.execute("SELECT * FROM port_fills WHERE account_id=? ORDER BY id DESC LIMIT 60", (pid,)).fetchall()
        eq = conn.execute("SELECT * FROM port_equity WHERE account_id=? ORDER BY date", (pid,)).fetchall()
        cands = scan.candidates(conn, spec, 25, aid=a["id"]) if a["active"] else []
        cov = scan.coverage(conn)
        mine = {r["ticker"] for r in conn.execute("SELECT ticker FROM valuation_inputs")}
        newer = _newer_strategy(conn, a, spec)
    finally:
        conn.close()
    equity = a["cash"] + sum(p["value"] for p in pos)
    eq_chart = None
    if len(eq) >= 2:
        ds = [date.fromisoformat(r["date"]) for r in eq]
        lines = [{"label": "组合", "color": "var(--accent)", "values": [r["equity"] for r in eq]}]
        if all(r["bench"] for r in eq):
            lines.insert(0, {"label": "同期 SPY", "color": "var(--muted)", "values": [r["bench"] for r in eq]})
        eq_chart = chart(lines, ds, h=160, log=False)
    held = {p["ticker"] for p in pos}
    return {"a": a, "spec": spec, "desc": backtest.describe(spec), "pos": pos, "pending": pending, "fills": fills,
            "eq": eq_chart, "equity": equity, "bench": eq[-1]["bench"] if eq else None, "cands": cands, "held": held, "momo_on": bool(scan.price_conds(spec, "entry")),
            "newer": newer,
            "cov": cov, "max_upside": scan.MAX_UPSIDE, "mine": mine}


@router.get("/scan/{pid}")
async def scan_detail(request: Request, pid: int):
    v = await asyncio.to_thread(_scan_view, pid)
    if not v:
        raise HTTPException(404)
    v["err"] = request.query_params.get("err")
    return _render(request, "scan_detail.html", v, "paper")


@router.post("/scan/{pid}/sync")
async def scan_sync(pid: int):
    """Follow the saved strategy's latest version; unfilled signals from the last close are re-decided."""
    from urllib.parse import quote
    from radar import scan

    def go():
        conn = _conn()
        try:
            a = scan.account(conn, pid)
            st = paper.get_strategy(conn, str(a["strategy_id"])) if a and a["strategy_id"] else None
            if not st:
                raise ValueError("找不到这个组合用的策略（可能删掉了）")
            return scan.update_spec(conn, pid, st["spec"])
        finally:
            conn.close()
    try:
        await asyncio.to_thread(go)
    except ValueError as e:
        return RedirectResponse(f"/scan/{pid}?err=" + quote(str(e)), status_code=303)
    return RedirectResponse(f"/scan/{pid}", status_code=303)


@router.post("/scan/{pid}/stop")
def scan_stop(pid: int):
    conn = _conn()
    try:
        conn.execute("UPDATE port_accounts SET active=0 WHERE id=?", (pid,))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(f"/scan/{pid}", status_code=303)
