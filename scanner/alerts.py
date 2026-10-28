"""Alerts, and their delivery to a webhook (ntfy, Slack, Discord or raw JSON)."""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone

from .config import AlertSettings
from .data import USER_AGENT


@dataclass(frozen=True)
class Alert:
    key: str                    # dedup key: one alert per key, ever
    kind: str                   # "signal" or "news"
    markets: tuple[str, ...]
    title: str
    body: str
    url: str = ""
    strategy: str = ""
    direction: int = 0
    provisional: bool = False   # fired on a bar that is still forming
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    id: int | None = None

    def with_id(self, id: int) -> "Alert":
        return replace(self, id=id)

    def as_dict(self) -> dict:
        return dict(asdict(self), created_at=self.created_at.isoformat(timespec="seconds"),
                    markets=list(self.markets))


def webhook_request(alert: Alert, settings: AlertSettings) -> urllib.request.Request:
    """The POST that delivers `alert` in the configured format."""
    fmt, url = settings.webhook_format, settings.webhook_url
    text = alert.body + (f"\n{alert.url}" if alert.url else "")
    if fmt == "ntfy":
        # ntfy's JSON API takes the topic in the body, so headlines need no header encoding.
        parts = urllib.parse.urlsplit(url)
        base, _, topic = parts.path.rstrip("/").rpartition("/")
        payload = {"topic": topic, "title": alert.title, "message": alert.body,
                   "tags": ["chart_with_upwards_trend" if alert.kind == "signal" else "newspaper"]}
        if alert.url:
            payload["click"] = alert.url
        url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, base + "/", "", ""))
    elif fmt == "slack":
        payload = {"text": f"*{alert.title}*\n{text}"}
    elif fmt == "discord":
        payload = {"content": f"**{alert.title}**\n{text}"[:2000]}
    else:
        payload = alert.as_dict()
    return urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                  headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})


def _post(request: urllib.request.Request):
    with urllib.request.urlopen(request, timeout=15) as response:
        response.read()


class Notifier:
    """Logs every alert and posts it to the webhook, if one is set. Delivery errors are logged, not raised."""

    def __init__(self, settings: AlertSettings, post=_post, log=None, echo: bool = True):
        self.settings = settings
        self.post = post
        self.log = log or (lambda line: print(line, file=sys.stderr, flush=True))
        self.echo = echo

    def send(self, alert: Alert):
        if self.echo:
            flag = " (provisional)" if alert.provisional else ""
            self.log(f"[alert] {alert.title}{flag}: {alert.body}")
        if not self.settings.webhook_url:
            return
        try:
            self.post(webhook_request(alert, self.settings))
        except Exception as exc:     # a dead webhook must not stop the scanner
            self.log(f"[alert] webhook delivery failed: {exc}")
