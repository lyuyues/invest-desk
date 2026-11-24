"""Paper trading /trade: manual paper trading with a journal (learning; no real orders)."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from urllib.parse import parse_qs, quote as urlquote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from radar import db, options, trade
from radar.config import load_config
from radar.notes import TICKER_RE
from web.sim_routes import chart

router = APIRouter()


def _render(request, name, ctx):
    from web.app import templates
    r = templates.TemplateResponse(request, name, {**ctx, "active": "trade"})
    r.headers["Cache-Control"] = "no-store"
    return r


def _conn():
    conn = db.connect(load_config().db_path)
    trade.init(conn)
    return conn


async def _form(request: Request) -> dict:
    return {k: v[0] for k, v in parse_qs((await request.body()).decode(), keep_blank_values=True).items()}


@router.get("/trade/quote")
async def trade_quote(t: str):
    t = t.upper().strip()
    if not TICKER_RE.match(t):
        return JSONResponse({"error": "Invalid ticker format"}, status_code=400)
    try:
        px = await asyncio.to_thread(trade.live_quote, t)
    except Exception as e:
        return JSONResponse({"error": f"Could not get a quote for {t}: {e}"}, status_code=404)
    return {"ticker": t, "price": px, "open": trade.market_open(datetime.now(timezone.utc))}


@router.get("/trade")
def trade_home(request: Request):
    conn = _conn()
    try:
        rows = trade.accounts(conn)
        if len([a for a in rows if a["active"]]) == 1 and not request.query_params.get("all"):
            return RedirectResponse(f"/trade/{next(a['id'] for a in rows if a['active'])}", status_code=303)
        last = {r["account_id"]: r["equity"] for r in conn.execute(
            "SELECT e.account_id, e.equity FROM trade_equity e JOIN (SELECT account_id, MAX(date) d FROM trade_equity "
            "GROUP BY account_id) m ON e.account_id=m.account_id AND e.date=m.d")}
    finally:
        conn.close()
    return _render(request, "trade.html", {"accts": [{"a": a, "equity": last.get(a["id"], a["capital"])} for a in rows]})


@router.post("/trade")
async def trade_create(request: Request):
    f = await _form(request)
    try:
        capital = min(1e9, max(1000, float(f.get("capital") or 100000)))
        cost = min(200, max(0, float(f.get("cost") or 5))) / 10000
    except ValueError:
        raise HTTPException(400)
    def go():
        conn = _conn()
        try:
            return trade.open_account(conn, f.get("name", ""), capital, cost, datetime.now(timezone.utc))
        finally:
            conn.close()
    aid = await asyncio.to_thread(go)
    return RedirectResponse(f"/trade/{aid}", status_code=303)


def _view(aid: int):
    conn = _conn()
    now = datetime.now(timezone.utc)
    try:
        a = trade.account(conn, aid)
        if not a:
            return None
        options.sweep_live(conn, aid, now)
        a = trade.account(conn, aid)
        ov, opos, oerrs = options.value(conn, aid, now)
        _, otrips = options.book(conn, aid)
        reserved, locked = options.constraints(conn, aid)
        open_opt = options.orders(conn, aid, "open")
        recent_opt = conn.execute("SELECT * FROM opt_orders WHERE account_id=? AND status IN ('filled','rejected') "
                                  "ORDER BY id DESC LIMIT 20", (aid,)).fetchall()
        pos, trips = trade.book(conn, aid)
        from radar import valuation
        dcf = {}
        for t in pos:
            v = valuation.load(conn, t)
            dcf[t] = (valuation.fair_value(conn, t), bool(v and v["mine"]))
        open_orders = trade.orders(conn, aid, "open")
        recent = conn.execute("SELECT * FROM trade_orders WHERE account_id=? AND status IN ('filled','rejected') "
                              "ORDER BY id DESC LIMIT 30", (aid,)).fetchall()
        eq = conn.execute("SELECT * FROM trade_equity WHERE account_id=? ORDER BY date", (aid,)).fetchall()
    finally:
        conn.close()
    errs = list(oerrs)
    positions = []
    for t, p in sorted(pos.items()):
        try:
            px = trade.live_quote(t)
        except Exception as e:
            px, _ = p["avg"], errs.append(f"{t} quote failed: {e}")
        first = p["orders"][0]
        positions.append({"ticker": t, "qty": p["qty"], "avg": p["avg"], "px": px, "value": p["qty"] * px,
                          "upnl": (px - p["avg"]) * p["qty"], "uret": px / p["avg"] - 1,
                          "stop": first["plan_stop"], "reason": first["reason"],
                          # target: your own number if you set one, else your DCF fair value (follows the Valuation panel)
                          "target": first["plan_target"] or dcf[t][0], "manual_target": first["plan_target"],
                          "target_src": "Manual" if first["plan_target"] else ("My DCF" if dcf[t][1] else "Auto DCF") if dcf[t][0] else None,
                          "opened": first["filled_at"][:10], "locked": locked.get(t, 0)})
    equity = a["cash"] + sum(p["value"] for p in positions) + ov
    try:
        bench = a["capital"] * trade.live_quote(trade.BENCH) / a["bench_px"] if a["bench_px"] else None
    except Exception:
        bench = None
    return {"a": a, "positions": positions, "trips": list(reversed(trips)), "open_orders": open_orders,
            "recent": recent, "eq_rows": eq, "equity": equity, "bench": bench, "errs": errs,
            "st": trade.stats(trips), "opos": opos, "ovalue": ov, "otrips": list(reversed(otrips)),
            "ost": trade.stats(otrips), "reserved": reserved, "free_cash": a["cash"] - reserved,
            "open_opt": open_opt, "recent_opt": recent_opt}


@router.get("/trade/{aid}")
async def trade_account(request: Request, aid: int):
    v = await asyncio.to_thread(_view, aid)
    if not v:
        raise HTTPException(404)
    rows = list(v["eq_rows"])
    series = [(date.fromisoformat(r["date"]), r["equity"], r["bench"]) for r in rows]
    today = datetime.now(timezone.utc).astimezone(trade.NY).date()
    if not series or series[-1][0] < today:
        series.append((today, v["equity"], v["bench"]))
    created = datetime.fromisoformat(v["a"]["created"]).astimezone(trade.NY).date()
    series = [(created, v["a"]["capital"], v["a"]["capital"])] + [s for s in series if s[0] > created]
    eq_chart = None
    if len(series) >= 2:
        lines = [{"label": "My accounts", "color": "var(--accent)", "values": [s[1] for s in series]}]
        if all(s[2] for s in series):
            lines.insert(0, {"label": "SPY (same period)", "color": "var(--muted)", "values": [s[2] for s in series]})
        eq_chart = chart(lines, [s[0] for s in series], h=160, log=False)
    v.update(eq=eq_chart, mdd=trade.max_drawdown([s[1] for s in series]),
             err=request.query_params.get("err"), ok=request.query_params.get("ok"),
             open=trade.market_open(datetime.now(timezone.utc)),
             pre_ticker=request.query_params.get("t", ""), pre_side=request.query_params.get("side", "buy"))
    return _render(request, "trade_account.html", v)


@router.post("/trade/{aid}/order")
async def trade_order(request: Request, aid: int):
    f = await _form(request)
    if not TICKER_RE.match((f.get("ticker") or "").upper().strip()):
        return RedirectResponse(f"/trade/{aid}?err=" + urlquote("Invalid ticker format"), status_code=303)
    def go():
        conn = _conn()
        try:
            oid = trade.place(conn, aid, f, datetime.now(timezone.utc))
            return conn.execute("SELECT * FROM trade_orders WHERE id=?", (oid,)).fetchone()
        finally:
            conn.close()
    try:
        o = await asyncio.to_thread(go)
    except trade.OrderError as e:
        return RedirectResponse(f"/trade/{aid}?err=" + urlquote(str(e)), status_code=303)
    except Exception as e:
        return RedirectResponse(f"/trade/{aid}?err=" + urlquote(f"Order failed: {e}"), status_code=303)
    verb = "Buy" if o["side"] == "buy" else "Sell"
    if o["status"] == "filled":
        msg = f"Filled: {verb} {o['ticker']} {o['qty']:g} shares @ {o['fill_px']:.2f}" + (f"({o['msg']})" if o["msg"] else "")
    elif o["status"] == "rejected":
        return RedirectResponse(f"/trade/{aid}?err=" + urlquote(o["msg"]), status_code=303)
    else:
        msg = f"Pending: {verb} {o['ticker']} {o['qty']:g} shares, {'Limit' if o['kind'] == 'limit' else 'stop price'} {o['px']:.2f}, matched against the daily bar after each close"
    return RedirectResponse(f"/trade/{aid}?ok=" + urlquote(msg), status_code=303)


@router.post("/trade/{aid}/transfer")
async def trade_transfer(request: Request, aid: int):
    f = await _form(request)
    t = (f.get("ticker") or "").upper().strip()
    def go():
        if not TICKER_RE.match(t):
            raise trade.OrderError("Invalid ticker format")
        try:
            qty = float(f.get("qty") or 0)
            cost = float(f["cost"]) if f.get("cost") else None
        except ValueError:
            raise trade.OrderError("Shares / cost must be numbers")
        conn = _conn()
        try:
            trade.transfer_in(conn, aid, t, qty, cost, datetime.now(timezone.utc))
        finally:
            conn.close()
        return qty
    try:
        qty = await asyncio.to_thread(go)
    except trade.OrderError as e:
        return RedirectResponse(f"/trade/{aid}?err=" + urlquote(str(e)), status_code=303)
    return RedirectResponse(f"/trade/{aid}?ok=" + urlquote(f"Transferred in {t} {qty:g} shares" + (", ready to sell covered calls" if qty >= 100 else "")),
                            status_code=303)


@router.post("/trade/{aid}/transfer/{oid}")
async def trade_transfer_fix(request: Request, aid: int, oid: int):
    f = await _form(request)
    def go():
        try:
            qty = float(f.get("qty") or 0)
        except ValueError:
            raise trade.OrderError("Shares must be a number")
        conn = _conn()
        try:
            trade.fix_transfer(conn, aid, oid, qty, datetime.now(timezone.utc))
        finally:
            conn.close()
        return qty
    try:
        qty = await asyncio.to_thread(go)
    except trade.OrderError as e:
        return RedirectResponse(f"/trade/{aid}?err=" + urlquote(str(e)), status_code=303)
    return RedirectResponse(f"/trade/{aid}?ok=" + urlquote("Deleted this transfer" if qty == 0 else f"Changed to {qty:g} shares"),
                            status_code=303)


@router.post("/trade/{aid}/plan")
async def trade_plan(request: Request, aid: int):
    f = await _form(request)
    t = (f.get("ticker") or "").upper().strip()
    def go():
        try:
            stop = float(f["stop"]) if f.get("stop") else None
            target = float(f["target"]) if f.get("target") else None
        except ValueError:
            raise trade.OrderError("Price must be a number")
        conn = _conn()
        try:
            trade.set_plan(conn, aid, t, stop, target)
        finally:
            conn.close()
    try:
        await asyncio.to_thread(go)
    except trade.OrderError as e:
        return RedirectResponse(f"/trade/{aid}?err=" + urlquote(str(e)), status_code=303)
    return RedirectResponse(f"/trade/{aid}?ok=" + urlquote(f"Updated stop / target for {t}"), status_code=303)


@router.post("/trade/{aid}/cancel/{oid}")
def trade_cancel(aid: int, oid: int):
    conn = _conn()
    try:
        trade.cancel(conn, aid, oid)
    finally:
        conn.close()
    return RedirectResponse(f"/trade/{aid}", status_code=303)


@router.post("/trade/{aid}/review/{oid}")
async def trade_review(request: Request, aid: int, oid: int):
    f = await _form(request)
    conn = _conn()
    try:
        trade.set_review(conn, aid, oid, f.get("review", ""))
    finally:
        conn.close()
    return RedirectResponse(f"/trade/{aid}#journal", status_code=303)


@router.post("/trade/{aid}/close")
def trade_close(aid: int):
    conn = _conn()
    try:
        conn.execute("UPDATE trade_accounts SET active=0 WHERE id=?", (aid,))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/trade?all=1", status_code=303)


# ---------- options ----------

def _opt_view(aid: int, u: str, e: str, c: str):
    now = datetime.now(timezone.utc)
    conn = _conn()
    try:
        a = trade.account(conn, aid)
        if not a:
            return None
        opos, _ = options.book(conn, aid)
        reserved, locked = options.constraints(conn, aid, opos)
        stocks, _ = trade.book(conn, aid)
        held_u = sorted({p["underlying"] for p in opos.values()} | set(stocks))
    finally:
        conn.close()
    out = {"a": a, "u": u, "held_u": held_u, "free_cash": a["cash"] - reserved, "rows": [], "exps": [], "e": e,
           "sel": None, "errs": []}
    if not u:
        return out
    try:
        exps = options.expiries(u)
    except Exception as ex:
        out["errs"].append(f"Could not get option expirations for {u}: {ex}")
        return out
    if not exps:
        out["errs"].append(f"{u} has no listed options")
        return out
    today = now.astimezone(trade.NY).date()
    out["exps"] = [(x, (date.fromisoformat(x) - today).days) for x in exps]
    if c:
        try:
            e = options.parse(c)["expiry"].isoformat()
        except trade.OrderError:
            c = ""
    if e not in exps:
        e = next((x for x, d in out["exps"] if d >= 25), exps[-1])
    out["e"] = e
    try:
        ch = options.chain(u, e)
        spot = trade.live_quote(u)
    except Exception as ex:
        out["errs"].append(f"Failed to load option chain: {ex}")
        return out
    t = options.years_left(date.fromisoformat(e), now)
    def dl(r):
        return options.bs(spot, r["strike"], t, r["iv"], r["right"])[1] if r and r["iv"] > 0 else None
    calls = {r["strike"]: r for r in ch["calls"]}
    puts = {r["strike"]: r for r in ch["puts"]}
    ks = sorted(set(calls) | set(puts))
    i = min(range(len(ks)), key=lambda j: abs(ks[j] - spot)) if ks else 0
    for k in ks[max(0, i - 10): i + 11]:
        cr, pr = calls.get(k), puts.get(k)
        out["rows"].append({"k": k, "c": cr, "p": pr, "cd": dl(cr), "pd": dl(pr)})
    out.update(spot=spot, dte=(date.fromisoformat(e) - today).days)
    if c and c in ch["by"]:
        r = ch["by"][c]
        info = options.parse(c)
        out["sel"] = {**r, "label": options.label(info), "delta": dl(r), "pos": opos.get(c, {}).get("qty", 0),
                      "avg": opos.get(c, {}).get("avg"), "shares": stocks.get(u, {}).get("qty", 0),
                      "locked": locked.get(u, 0)}
    return out


@router.get("/trade/{aid}/opt")
async def trade_opt(request: Request, aid: int):
    qp = request.query_params
    u = (qp.get("u") or "").upper().strip()
    if u and not TICKER_RE.match(u):
        u = ""
    v = await asyncio.to_thread(_opt_view, aid, u, qp.get("e", ""), (qp.get("c") or "").upper())
    if not v:
        raise HTTPException(404)
    eq = await asyncio.to_thread(_view, aid)
    v.update(equity=eq["equity"] if eq else v["a"]["capital"], side=qp.get("side", "buy"),
             err=qp.get("err"), ok=qp.get("ok"), open=trade.market_open(datetime.now(timezone.utc)))
    return _render(request, "trade_options.html", v)


@router.post("/trade/{aid}/opt/order")
async def trade_opt_order(request: Request, aid: int):
    f = await _form(request)
    back = f"/trade/{aid}/opt?u={urlquote(f.get('u', ''))}&c={urlquote(f.get('contract', ''))}"
    def go():
        conn = _conn()
        try:
            oid = options.place(conn, aid, f, datetime.now(timezone.utc))
            return conn.execute("SELECT * FROM opt_orders WHERE id=?", (oid,)).fetchone()
        finally:
            conn.close()
    try:
        o = await asyncio.to_thread(go)
    except trade.OrderError as e:
        return RedirectResponse(back + "&err=" + urlquote(str(e)), status_code=303)
    except Exception as e:
        return RedirectResponse(back + "&err=" + urlquote(f"Order failed: {e}"), status_code=303)
    verb = "Buy" if o["side"] == "buy" else "Sell"
    name = options.label(options.parse(o["contract"]))
    if o["status"] == "rejected":
        return RedirectResponse(back + "&err=" + urlquote(o["msg"]), status_code=303)
    if o["status"] == "filled":
        msg = f"Filled: {verb} {name} {o['qty']} contracts @ {o['fill_px']:.2f}" + (f"({o['msg']})" if o["msg"] else "")
    else:
        msg = f"Pending: {verb} {name} {o['qty']} contracts, limit {o['px']:.2f}"
    return RedirectResponse(back + "&ok=" + urlquote(msg), status_code=303)


@router.post("/trade/{aid}/opt/cancel/{oid}")
def trade_opt_cancel(aid: int, oid: int):
    conn = _conn()
    try:
        options.cancel(conn, aid, oid)
    finally:
        conn.close()
    return RedirectResponse(f"/trade/{aid}#opt", status_code=303)


@router.post("/trade/{aid}/opt/review/{oid}")
async def trade_opt_review(request: Request, aid: int, oid: int):
    f = await _form(request)
    conn = _conn()
    try:
        options.set_review(conn, aid, oid, f.get("review", ""))
    finally:
        conn.close()
    return RedirectResponse(f"/trade/{aid}#ojournal", status_code=303)
