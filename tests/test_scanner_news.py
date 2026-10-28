import unittest
from datetime import datetime, timedelta, timezone

from scanner.config import Market, NewsSettings
from scanner.news import NewsItem, Scorer, google_news_url, item_id, match_terms, parse_feed

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>"soybean oil" - Google News</title>
<item>
  <title>Soybean oil futures jump as EPA lifts biofuel mandates - Reuters</title>
  <link>https://news.google.com/rss/articles/abc</link>
  <guid>abc</guid>
  <pubDate>Wed, 28 Oct 2026 14:05:00 GMT</pubDate>
  <description>&lt;a href="https://example.com"&gt;Soybean oil futures jump&lt;/a&gt;&amp;nbsp;&lt;font color="#6f6f6f"&gt;Reuters&lt;/font&gt;</description>
  <source url="https://www.reuters.com">Reuters</source>
</item>
<item>
  <title>Palm oil - what next? A long read</title>
  <link>https://example.com/palm</link>
  <pubDate>not a date</pubDate>
</item>
<item><title></title><link>https://example.com/empty</link></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>farmdoc daily</title>
<entry>
  <title>Revisiting the 45Z credit and soybean oil demand</title>
  <link rel="alternate" href="https://farmdoc.example/45z"/>
  <published>2026-10-27T09:30:00Z</published>
  <summary type="html">&lt;p&gt;Renewable diesel margins and the RVO.&lt;/p&gt;</summary>
</entry>
</feed>"""


def market(code, *keywords):
    return Market(code=code, symbol=f"{code}=F", name=code, keywords=keywords)


SETTINGS = NewsSettings(min_score=4, max_age_hours=36, impact_terms=("EPA", "mandate", "drought"),
                        up_words=("jump", "surge"), down_words=("plunge",))


class Feeds(unittest.TestCase):
    def test_rss(self):
        items = parse_feed(RSS)
        self.assertEqual(len(items), 2)          # the item with an empty title is skipped
        first = items[0]
        self.assertEqual(first.title, "Soybean oil futures jump as EPA lifts biofuel mandates")
        self.assertEqual(first.source, "Reuters")
        self.assertEqual(first.published, datetime(2026, 10, 28, 14, 5, tzinfo=timezone.utc))
        self.assertEqual(first.summary, "Soybean oil futures jump Reuters")
        # " - what next?" is part of the headline, not an outlet name.
        self.assertEqual(items[1].title, "Palm oil - what next? A long read")
        self.assertIsNone(items[1].published)

    def test_atom(self):
        [item] = parse_feed(ATOM)
        self.assertEqual(item.link, "https://farmdoc.example/45z")
        self.assertEqual(item.source, "farmdoc daily")
        self.assertEqual(item.summary, "Renewable diesel margins and the RVO.")
        self.assertEqual(item.published, datetime(2026, 10, 27, 9, 30, tzinfo=timezone.utc))

    def test_same_story_same_id(self):
        self.assertEqual(item_id("Cocoa hits a record!"), item_id("cocoa hits a record"))

    def test_google_news_url(self):
        url = google_news_url('"soybean oil" OR biodiesel')
        self.assertTrue(url.startswith("https://news.google.com/rss/search?q="))
        self.assertIn("when%3A2d", url)


class Matching(unittest.TestCase):
    def test_whole_words_and_inflections(self):
        self.assertEqual(match_terms(["hog"], "Hogs rally"), ["hog"])
        self.assertEqual(match_terms(["surge"], "Cocoa surged"), ["surge"])
        self.assertEqual(match_terms(["cane"], "Hurricane season"), [])
        self.assertEqual(match_terms(["oat"], "Goats and boats"), [])

    def test_longest_match_wins(self):
        self.assertEqual(match_terms(["soybean", "soybean oil"], "Soybean oil climbs"), ["soybean oil"])
        self.assertEqual(sorted(match_terms(["soybean", "soybean oil"], "Soybean oil and soybean exports")),
                         ["soybean", "soybean oil"])


class Scoring(unittest.TestCase):
    def setUp(self):
        self.scorer = Scorer([market("ZL", "soybean oil", "biodiesel", "renewable diesel"),
                              market("LE", "cattle"), market("GF", "feeder cattle", "cattle")], SETTINGS)

    def item(self, title, summary="", hours=1):
        return NewsItem(item_id(title), title, "https://example.com", "Wire",
                        datetime(2026, 10, 28, 12, tzinfo=timezone.utc) - timedelta(hours=hours), summary)

    def test_points(self):
        [m] = self.scorer.score(self.item("Soybean oil futures jump as EPA lifts mandates",
                                          "Biodiesel makers cheer"))
        self.assertEqual(m.market, "ZL")
        self.assertEqual(m.keywords, ("soybean oil", "biodiesel"))
        self.assertEqual(m.impact, ("mandate", "EPA"))
        self.assertEqual(m.score, 3 + 1 + 2)       # title keyword 3, summary-only keyword 1, impact 1 each
        self.assertEqual(m.tone, 1)

    def test_no_keyword_no_match(self):
        self.assertEqual(self.scorer.score(self.item("EPA announces drought relief")), [])

    def test_specific_market_first_on_a_tie(self):
        matches = self.scorer.score(self.item("Feeder cattle plunge"))
        self.assertEqual([m.market for m in matches], ["GF", "LE"])
        self.assertEqual(matches[0].tone, -1)

    def test_freshness(self):
        now = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
        self.assertTrue(self.scorer.is_fresh(self.item("x", hours=35), now))
        self.assertFalse(self.scorer.is_fresh(self.item("x", hours=37), now))
        undated = NewsItem("id", "x", "", "", None)
        self.assertFalse(self.scorer.is_fresh(undated, now))


if __name__ == "__main__":
    unittest.main()
