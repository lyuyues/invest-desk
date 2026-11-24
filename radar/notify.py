"""ntfy push. Uses JSON publishing so Chinese titles need no header encoding."""
from __future__ import annotations

from collections.abc import Callable

import httpx

from radar.models import Fire

PRIORITY = {"urgent": 4, "notice": 3, "log": 2}
TAGS = {"urgent": ["rotating_light"], "notice": ["chart_with_upwards_trend"], "log": []}


# rule_id prefix → page the notification opens (derived from rule_id so deferred alerts keep their link)
PAGES = {"port": "/scan/{0}", "paper": "/paper/{0}", "weekly": "/reports/{0}", "daily": "/daily/{0}",
         "ledger": "/ledger/reports/{0}", "enote": "/earnings#e-{0}-{1}"}


def click_path(f: Fire) -> str:
    kind, _, rest = f.rule_id.partition(":")
    if kind in PAGES and rest:
        return PAGES[kind].format(*rest.split(":"), "")
    return f"/t/{f.ticker}" if f.ticker not in ("*", "SYSTEM", "MACRO") else ("/macro" if f.ticker == "MACRO" else "/")


def make_pusher(server: str, topic: str, public_url: str | None = None,
                post: Callable = httpx.post) -> Callable[[Fire], None]:
    def push(f: Fire) -> None:
        payload = {
            "topic": topic,
            "title": f.title,
            "message": f.body or f.title,
            "priority": PRIORITY[f.level],
            "tags": TAGS[f.level],
        }
        if public_url:
            payload["click"] = public_url + click_path(f)
        resp = post(server, json=payload, timeout=10)
        resp.raise_for_status()

    return push
