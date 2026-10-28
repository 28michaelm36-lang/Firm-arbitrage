"""Headlines from RSS/Atom feeds, scored against each market's keywords."""
from __future__ import annotations

import hashlib
import html
import re
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache

from .config import Market, NewsSettings
from .data import http_get

ATOM = "{http://www.w3.org/2005/Atom}"
TITLE_POINTS, SUMMARY_POINTS, IMPACT_POINTS = 3, 1, 1


@dataclass(frozen=True)
class NewsItem:
    id: str                     # hash of the normalised headline: the same story from two outlets is one item
    title: str
    link: str
    source: str
    published: datetime | None
    summary: str = ""


@dataclass(frozen=True)
class Match:
    market: str
    score: float
    keywords: tuple[str, ...]
    impact: tuple[str, ...]
    tone: int                   # up-words minus down-words in the headline


def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def split_source(title: str, source: str = "") -> tuple[str, str]:
    """Google News appends ' - Outlet' to headlines; move it to the source."""
    head, sep, tail = title.rpartition(" - ")
    if sep and head and source and tail.strip().lower() == source.strip().lower():
        return head.strip(), source
    return title.strip(), source


def item_id(title: str) -> str:
    normal = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    return hashlib.sha1(normal.encode()).hexdigest()[:16]


def _date(text: str | None) -> datetime | None:
    if not text:
        return None
    text = text.strip()
    try:
        parsed = parsedate_to_datetime(text)                        # RSS: RFC 822
    except (TypeError, ValueError, IndexError):
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))  # Atom: RFC 3339
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _text(element, tag: str) -> str:
    child = element.find(tag)
    return (child.text or "").strip() if child is not None else ""


def parse_feed(body: bytes, default_source: str = "") -> list[NewsItem]:
    """RSS 2.0 or Atom -> items. Malformed entries are skipped, not fatal."""
    root = ET.fromstring(body)
    items = []
    if root.tag == f"{ATOM}feed":
        feed_title = _text(root, f"{ATOM}title") or default_source
        for entry in root.iter(f"{ATOM}entry"):
            link = entry.find(f"{ATOM}link[@rel='alternate']")
            if link is None:
                link = entry.find(f"{ATOM}link")
            title, source = split_source(strip_html(_text(entry, f"{ATOM}title")), feed_title)
            summary = strip_html(_text(entry, f"{ATOM}summary") or _text(entry, f"{ATOM}content"))
            published = _date(_text(entry, f"{ATOM}published") or _text(entry, f"{ATOM}updated"))
            if title:
                items.append(NewsItem(item_id(title), title, link.get("href", "") if link is not None else "",
                                      source, published, summary))
        return items
    channel = root.find("channel")
    feed_title = (_text(channel, "title") if channel is not None else "") or default_source
    for entry in root.iter("item"):
        source_el = entry.find("source")
        source = (source_el.text or "").strip() if source_el is not None else ""
        title, source = split_source(strip_html(_text(entry, "title")), source or feed_title)
        if title:
            items.append(NewsItem(item_id(title), title, _text(entry, "link"), source,
                                  _date(_text(entry, "pubDate")), strip_html(_text(entry, "description"))))
    return items


def google_news_url(query: str, days: int = 2) -> str:
    params = {"q": f"{query} when:{days}d", "hl": "en-US", "gl": "US", "ceid": "US:en"}
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(params)


@lru_cache(maxsize=4096)
def _pattern(term: str) -> re.Pattern:
    # Whole words, case-insensitive, with simple inflections: hog/hogs, surge/surged.
    return re.compile(r"(?<!\w)" + re.escape(term) + r"(?:s|es|d|ed|ing)?(?!\w)", re.IGNORECASE)


def match_terms(terms: Iterable[str], text: str) -> list[str]:
    """Terms found in text. A term inside a longer matched term does not count again:
    'soybean oil' in a headline matches 'soybean oil', not also 'soybean'."""
    found, taken = [], []
    for term in sorted(set(terms), key=len, reverse=True):
        spans = [m.span() for m in _pattern(term).finditer(text)]
        free = [s for s in spans if not any(s[0] < e and b < s[1] for b, e in taken)]
        if free:
            found.append(term)
            taken.extend(free)
    return found


class Scorer:
    def __init__(self, markets: Sequence[Market], settings: NewsSettings):
        self.markets = markets
        self.settings = settings

    def score(self, item: NewsItem) -> list[Match]:
        """One Match per market the item mentions, best first."""
        s = self.settings
        impact = tuple(match_terms(s.impact_terms, f"{item.title} {item.summary}"))
        tone = len(match_terms(s.up_words, item.title)) - len(match_terms(s.down_words, item.title))
        matches = []
        for market in self.markets:
            in_title = match_terms(market.keywords, item.title)
            in_summary = [k for k in match_terms(market.keywords, item.summary) if k not in in_title]
            if not in_title and not in_summary:
                continue
            score = TITLE_POINTS * len(in_title) + SUMMARY_POINTS * len(in_summary) + IMPACT_POINTS * len(impact)
            matches.append(Match(market.code, score, tuple(in_title + in_summary), impact, tone))
        # Ties go to the more specific match: "feeder cattle" beats "cattle".
        return sorted(matches, key=lambda m: (-m.score, -sum(map(len, m.keywords))))

    def is_fresh(self, item: NewsItem, now: datetime) -> bool:
        return (item.published is not None
                and now - item.published <= timedelta(hours=self.settings.max_age_hours))


class GoogleNews:
    name = "google-news"

    def __init__(self, get=http_get):
        self.get = get

    def fetch(self, market: Market) -> list[NewsItem]:
        return parse_feed(self.get(google_news_url(market.query())), "Google News")


class Feed:
    def __init__(self, url: str, get=http_get):
        self.url = url
        self.get = get

    def fetch(self) -> list[NewsItem]:
        return parse_feed(self.get(self.url), urllib.parse.urlparse(self.url).netloc)


# (headline, hours old). Each market gets one that scores high enough to alert and one that doesn't.
DEMO_ALERTING = (
    ("{kw} futures surge as USDA trims supply outlook", 3),
    ("Traders weigh {kw} after EPA proposal on mandates", 6),
    ("{kw} prices slump on weak demand and a growing glut", 11),
)
DEMO_QUIET = ("Weekly outlook: what to watch in {kw}", 20)


class DemoNews:
    """Synthetic headlines, tagged (demo), so the page has something to show offline."""
    name = "demo"

    def __init__(self, now: datetime | None = None):
        self.now = now or datetime.now(timezone.utc)

    def fetch(self, market: Market) -> list[NewsItem]:
        keyword = market.keywords[0] if market.keywords else market.name
        keyword = keyword[0].upper() + keyword[1:]
        pick = hash_int(market.code)
        items = []
        for template, hours in (DEMO_ALERTING[pick % len(DEMO_ALERTING)], DEMO_QUIET):
            title = template.format(kw=keyword) + " (demo)"
            items.append(NewsItem(item_id(title), title, "", "Demo wire",
                                  self.now - timedelta(hours=hours + pick % 5),
                                  "Synthetic headline for demo mode."))
        return items


def hash_int(text: str) -> int:
    return int(hashlib.sha1(text.encode()).hexdigest()[:8], 16)
