import math
import os
import random
import statistics
import tempfile
import unittest
import zipfile
from datetime import date, datetime, timedelta

from intraday.data import (Bar, Day, detect_label, detect_rolls, read_bars, rescale, sessions,
                           third_friday)
from intraday.metrics import summarize
from intraday.replay import DayPath, _Account, day_paths, run_eval
from intraday.strategies import Costs, LastHalfHour, NoiseArea, OpeningRangeBreakout
from propfirm.rules import Drawdown, EvalRules

FREE = Costs(slip_ticks=0, commission=0)
ONE_TICK = Costs(slip_ticks=1, commission=0)


def make_day(d: date, closes, prev_close=None, opens=None, highs=None, lows=None):
    """A session from a list of closes. Each bar opens at the previous close
    unless `opens` says otherwise; highs and lows default to the body."""
    bars, prev = [], closes[0] if opens is None else opens[0]
    t0 = datetime.combine(d, datetime.min.time()).replace(hour=9, minute=30)
    for i, c in enumerate(closes):
        o = opens[i] if opens is not None else prev
        h = highs[i] if highs is not None else max(o, c)
        lo = lows[i] if lows is not None else min(o, c)
        bars.append(Bar(t0 + timedelta(minutes=i), o, h, lo, c, 1.0))
        prev = c
    return Day(d, bars, prev_close)


def random_walk_days(n_days: int, seed: int = 7, sd: float = 2.0, start: float = 10_000.0):
    rng = random.Random(seed)
    days, price, d = [], start, date(2020, 1, 1)
    prev_close = None
    for _ in range(n_days):
        d += timedelta(days=1)
        closes, opens, highs, lows = [], [], [], []
        price += rng.gauss(0, sd * 10)          # overnight move
        for _ in range(390):
            o = price
            path = [o + rng.gauss(0, sd / 2), o + rng.gauss(0, sd / 2)]
            price = o + rng.gauss(0, sd)
            opens.append(o)
            closes.append(price)
            highs.append(max(o, price, *path))
            lows.append(min(o, price, *path))
        day = make_day(d, closes, prev_close, opens, highs, lows)
        days.append(day)
        prev_close = day.close
    return days


class TestReading(unittest.TestCase):
    def _csv(self, text):
        f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False)
        f.write(text)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_column_zone_and_us_dates(self):
        path = self._csv("timestamp ET,open,high,low,close,volume,Vwap_RTH\n"
                         "12/27/2022 9:30,100,101,99,100.5,10,100\n"
                         "12/27/2022 9:31:00 AM,100.5,102,100,101,5,100\n"
                         "12/27/2022 1:05 PM,101,101,101,101,1,100\n")
        bars, info = read_bars([path], label="start")
        self.assertEqual(info.data_tz, "America/New_York")
        self.assertEqual([b.t for b in bars], [datetime(2022, 12, 27, 9, 30),
                                               datetime(2022, 12, 27, 9, 31),
                                               datetime(2022, 12, 27, 13, 5)])
        self.assertEqual(bars[0].c, 100.5)

    def test_utc_conversion_follows_dst(self):
        path = self._csv("time,close,high,low,open,volume\n"
                         "2019-01-02 14:30:00,1,1,1,1,1\n"      # EST: 09:30
                         "2019-07-02 13:30:00,1,1,1,1,1\n"      # EDT: 09:30
                         "2019-07-02T13:31:00+00:00,1,1,1,1,1\n"
                         "2019-07-02 13:31:00,2,2,2,2,1\n")     # duplicate, dropped
        bars, info = read_bars([path], label="start")
        self.assertEqual([b.t.time().isoformat() for b in bars], ["09:30:00", "09:30:00", "09:31:00"])
        self.assertEqual(info.duplicates, 1)
        self.assertEqual(bars[-1].c, 1.0)

    def test_excel_workbook(self):
        """A first sheet as Excel writes it: shared strings for the header,
        timestamps as serial day counts, a blank cell skipped."""
        ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
        rel = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
        header = ["timestamp ET", "open", "high", "low", "close", "volume"]
        strings = "".join(f"<si><t>{h}</t></si>" for h in header)
        serial = (date(2022, 12, 27) - date(1899, 12, 30)).days + 9.5 / 24
        cells = "".join(f'<c r="{c}1" t="s"><v>{i}</v></c>' for i, c in enumerate("ABCDEF"))
        rows = [f'<row r="1">{cells}</row>']
        for k in range(3):
            t = serial + k / 1440
            rows.append(f'<row r="{k + 2}"><c r="A{k + 2}"><v>{t!r}</v></c>'
                        f'<c r="B{k + 2}"><v>{100 + k}</v></c><c r="C{k + 2}"><v>{101 + k}</v></c>'
                        f'<c r="D{k + 2}"><v>{99 + k}</v></c><c r="E{k + 2}"><v>{100.5 + k}</v></c>'
                        f'<c r="F{k + 2}"><v>{10 * (k + 1)}</v></c></row>')
        path = os.path.join(tempfile.mkdtemp(), "nq.xlsx")
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("xl/workbook.xml", f'<workbook {ns} {rel}><sheets>'
                       f'<sheet name="NQ" sheetId="1" r:id="rId1"/></sheets></workbook>')
            z.writestr("xl/_rels/workbook.xml.rels",
                       '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                       '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
            z.writestr("xl/sharedStrings.xml", f"<sst {ns}>{strings}</sst>")
            z.writestr("xl/worksheets/sheet1.xml",
                       f"<worksheet {ns}><sheetData>{''.join(rows)}</sheetData></worksheet>")
        self.addCleanup(os.unlink, path)
        bars, info = read_bars([path], label="start")
        self.assertEqual(info.data_tz, "America/New_York")
        self.assertEqual([b.t for b in bars], [datetime(2022, 12, 27, 9, 30 + k) for k in range(3)])
        self.assertEqual((bars[2].o, bars[2].c, bars[2].v), (102.0, 102.5, 30.0))

    def test_bom_and_zipped_csv(self):
        text = "\ufefftimestamp ET,open,high,low,close,volume\n2023-01-03 09:30:00,1,2,0.5,1.5,7\n"
        path = self._csv(text)
        bars, info = read_bars([path], label="start")
        self.assertEqual((info.data_tz, bars[0].c), ("America/New_York", 1.5))
        zipped = path + ".zip"
        with zipfile.ZipFile(zipped, "w") as z:
            z.write(path, "nq.csv")
        self.addCleanup(os.unlink, zipped)
        self.assertEqual(read_bars([zipped], label="start")[0], bars)

    def test_end_labelled_bars_are_detected(self):
        rows = ["time,open,high,low,close,volume"]
        for day in range(2, 6):
            for minute, vol in ((28, 10), (29, 10), (30, 10), (31, 500), (32, 300)):
                rows.append(f"2023-01-0{day} 09:{minute}:00,1,1,1,1,{vol}")
        path = self._csv("\n".join(rows) + "\n")
        bars, info = read_bars([path], data_tz="America/New_York")
        self.assertEqual(info.label, "end")
        self.assertEqual(bars[0].t.minute, 27)
        start_bars = [Bar(b.t, 1, 1, 1, 1, b.v) for b in bars]
        self.assertEqual(detect_label(start_bars), "start")
        # no spike at all: keep the default
        flat = [Bar(b.t, 1, 1, 1, 1, 10 + (b.t.minute == 31)) for b in bars]
        self.assertEqual(detect_label(flat), "start")


class TestSessions(unittest.TestCase):
    def test_gaps_filled_and_prev_close(self):
        d1, d2 = datetime(2023, 3, 1, 9, 30), datetime(2023, 3, 2, 9, 30)
        bars = [Bar(d1, 10, 11, 9, 10.5), Bar(d1 + timedelta(minutes=2), 10.5, 12, 10, 11),
                Bar(d2, 11, 11, 11, 11)]
        days, _ = sessions(bars, min_bars=1)
        self.assertEqual(len(days[0].bars), 3)
        filler = days[0].bars[1]
        self.assertEqual((filler.o, filler.h, filler.l, filler.c, filler.v), (10.5,) * 4 + (0.0,))
        self.assertEqual(days[1].prev_close, 11)
        self.assertIsNone(days[0].prev_close)

    def test_roll_jump_adjusts_previous_close(self):
        expiry = third_friday(2023, 6)
        self.assertEqual(expiry, date(2023, 6, 16))
        bars, price = [], 15_000.0
        t = datetime(2023, 5, 25, 18, 0)
        rng = random.Random(1)
        roll_at = datetime(2023, 6, 8, 18, 0)
        while t < datetime(2023, 6, 20):
            if t == roll_at:
                price += 160.0                     # switch to the September contract
            o = price
            price += rng.gauss(0, 0.5)
            bars.append(Bar(t, o, max(o, price), min(o, price), price, 1))
            t += timedelta(minutes=1)
            if t.hour == 17:                       # daily halt
                t += timedelta(hours=1)
        rolls = detect_rolls(bars)
        self.assertEqual(len(rolls), 1)
        self.assertEqual(rolls[0][0], roll_at)
        self.assertAlmostEqual(rolls[0][1], 160.0, delta=2)
        days, _ = sessions(bars, rolls=rolls)
        after = next(d for d in days if d.date == date(2023, 6, 9))
        self.assertTrue(after.roll and after.usable)
        self.assertLess(abs(after.open - after.prev_close), 20)


    def test_rescale_keeps_signals(self):
        days = random_walk_days(40, seed=8, start=2_000.0)
        scaled = rescale(days, 15_000.0)
        self.assertAlmostEqual(scaled[5].prev_close, 15_000.0)
        for strat in (NoiseArea(), LastHalfHour(), OpeningRangeBreakout()):
            a = [(t.day, t.side, t.entry_index, t.exit_index) for t in strat.run(days, FREE)]
            b = [(t.day, t.side, t.entry_index, t.exit_index) for t in strat.run(scaled, FREE)]
            self.assertEqual(a, b, strat.name)


class TestFills(unittest.TestCase):
    def test_last_half_hour(self):
        closes = [100.0] * 390
        for i in range(330, 390):
            closes[i] = 101.0 + (i - 330) * 0.01
        day = make_day(date(2023, 1, 3), closes, prev_close=99.0)
        (t,) = LastHalfHour("rod").run([day], ONE_TICK)
        self.assertEqual(t.side, 1)
        self.assertEqual(t.entry_index, 360)
        self.assertAlmostEqual(t.entry, day.bars[360].o + 0.25)
        self.assertEqual(t.exit_index, 389)
        self.assertAlmostEqual(t.exit, day.bars[389].c - 0.25)
        # Gao's signal: price at 10:00 is above the previous close too.
        self.assertEqual(LastHalfHour("first").run([day], ONE_TICK)[0].side, 1)

    def test_orb_stop_target_and_gaps(self):
        up5 = [100.0, 100.5, 101.0, 101.5, 102.0]   # first candle up; low = open of bar 0
        rest = [102.0] * 385
        opens = [99.0] + [None] * 389

        def day_with(changes, lows=None, highs=None):
            closes = up5 + rest
            for k, v in changes.items():
                closes[k] = v
            o = [opens[0]] + closes[:-1]
            return make_day(date(2023, 1, 3), closes, 100.0, o, highs, lows)

        # stop: low of the first candle is 99; price falls through it on bar 20
        day = day_with({20: 98.0})
        (t,) = OpeningRangeBreakout().run([day], ONE_TICK)
        self.assertEqual(t.reason, "stop")
        self.assertAlmostEqual(t.entry, 102.25)
        self.assertAlmostEqual(t.stop_distance, 3.25)
        self.assertAlmostEqual(t.exit, 99.0 - 0.25)
        # a bar that opens below the stop fills at its open
        closes = up5 + rest
        o = [99.0] + closes[:-1]
        o[30] = 97.0
        closes[30] = 97.5
        day = make_day(date(2023, 1, 3), closes, 100.0, o)
        (t,) = OpeningRangeBreakout().run([day], ONE_TICK)
        self.assertAlmostEqual(t.exit, 97.0 - 0.25)
        # target: 10R above entry, needs one tick through
        target = 102.25 + 10 * 3.25
        day = day_with({50: target + 0.25})
        (t,) = OpeningRangeBreakout().run([day], ONE_TICK)
        self.assertEqual(t.reason, "target")
        self.assertAlmostEqual(t.exit, target)
        # a bar touching both counts as the stop
        closes = up5 + rest
        highs = [max(a, b) for a, b in zip([99.0] + closes[:-1], closes)]
        lows = [min(a, b) for a, b in zip([99.0] + closes[:-1], closes)]
        highs[60], lows[60] = target + 1, 98.0
        day = make_day(date(2023, 1, 3), closes, 100.0, [99.0] + closes[:-1], highs, lows)
        (t,) = OpeningRangeBreakout().run([day], ONE_TICK)
        self.assertEqual(t.reason, "stop")

    def test_noise_area_bands_entry_and_vwap_exit(self):
        days = []
        d = date(2023, 1, 1)
        # 14 quiet days: each minute is 0.1% above the open, so sigma = 0.001
        for i in range(14):
            d += timedelta(days=1)
            closes = [100.0] + [100.1] * 389
            days.append(make_day(d, closes, prev_close=100.0, opens=[100.0] * 390))
        d += timedelta(days=1)
        closes = [100.0] * 390
        for i in range(25, 390):
            closes[i] = 100.5                     # breaks above the band before 10:00
        for i in range(70, 390):
            closes[i] = 100.2                     # back below VWAP by 10:30... 11:00
        day = make_day(d, closes, prev_close=100.0, opens=[100.0] + closes[:-1])
        days.append(day)
        strat = NoiseArea()
        sig = list(strat.bands(days))[-1][1]
        s, upper, lower = sig[30]
        self.assertAlmostEqual(s, 0.001)
        self.assertAlmostEqual(upper, 100.1)
        self.assertAlmostEqual(lower, 99.9)
        trades = [t for t in strat.run(days, FREE) if t.day == day.date]
        t = trades[0]
        self.assertEqual((t.side, t.entry_index), (1, 30))
        # at 10:30 price 100.5 is above VWAP; at 11:00 it is 100.2, below VWAP
        self.assertEqual((t.reason, t.exit_index), ("trail", 90))
        self.assertAlmostEqual(t.points, 100.2 - 100.5)
        # still above the band, so the paper's rule buys again at the next
        # check and the VWAP stop takes it out at the one after
        self.assertEqual([(u.entry_index, u.exit_index) for u in trades[1:3]], [(120, 150), (180, 210)])

    def test_no_lookahead(self):
        """Scrambling every bar after an entry cannot change that entry."""
        days = random_walk_days(40, seed=3)
        for strat in (NoiseArea(), NoiseArea(exit="base"), LastHalfHour("rod"),
                      LastHalfHour("first"), OpeningRangeBreakout()):
            base = strat.run(days, ONE_TICK)
            self.assertTrue(base, strat.name)
            rng = random.Random(5)
            for t in base[:: max(1, len(base) // 8)]:
                i = next(k for k, d in enumerate(days) if d.date == t.day)
                day = days[i]
                cut = t.entry_index
                bars = day.bars[:cut] + [Bar(b.t, b.o + 50, b.h + 80, b.l - 80, b.c - 50 * rng.random())
                                         for b in day.bars[cut:]]
                bars[cut] = Bar(bars[cut].t, day.bars[cut].o, bars[cut].h, bars[cut].l, bars[cut].c)
                changed = days[:i] + [Day(day.date, bars, day.prev_close)] + days[i + 1:]
                again = [u for u in strat.run(changed, ONE_TICK) if u.day == t.day]
                first = next(u for u in again if u.entry_index >= cut)
                self.assertEqual((first.entry_index, first.side), (t.entry_index, t.side), strat.name)
                self.assertAlmostEqual(first.entry, t.entry)

    def test_random_walk_has_no_edge(self):
        days = random_walk_days(400, seed=11)
        for strat in (NoiseArea(), LastHalfHour("rod"), OpeningRangeBreakout()):
            trades = strat.run(days[15:], FREE) if strat.name != "noise" else strat.run(days, FREE)
            pts = [t.points for t in trades]
            se = statistics.stdev(pts) / math.sqrt(len(pts))
            self.assertLess(abs(statistics.fmean(pts)), 3 * se, strat.name)
            # one tick per fill costs half a point per round trip
            costly = [t.points for t in strat.run(days, ONE_TICK)]
            self.assertLess(statistics.fmean(costly), statistics.fmean(pts) + 3 * se)


class TestReplay(unittest.TestCase):
    def rules(self, mode="intraday_trailing", dll=None, dll_mode="pause", consistency=None):
        return EvalRules(start_balance=50_000, profit_target=3_000, fee=50,
                         drawdown=Drawdown(2_000, mode), daily_loss_limit=dll,
                         daily_loss_mode=dll_mode, consistency=consistency)

    def test_trailing_floor_follows_open_equity(self):
        acct = _Account(self.rules())
        # up $1,500 open, then back to -$600: the floor trailed to 49,500
        alive = acct.trade_day(DayPath(((1_500, -600),), -600, True))
        self.assertFalse(alive)
        self.assertEqual(acct.balance, 49_500)
        eod = _Account(self.rules("eod_trailing"))
        self.assertTrue(eod.trade_day(DayPath(((1_500, -600),), -600, True)))
        self.assertEqual(eod.floor, 48_000)

    def test_daily_loss_limit_pause_or_fail(self):
        acct = _Account(self.rules(dll=1_000))
        self.assertTrue(acct.trade_day(DayPath(((0, -1_200),), -1_200, True)))
        self.assertEqual(acct.balance, 49_000)
        acct = _Account(self.rules(dll=1_000, dll_mode="fail"))
        self.assertFalse(acct.trade_day(DayPath(((0, -1_200),), -1_200, True)))
        # the trailing floor, if higher, is hit first
        acct = _Account(self.rules(dll=1_000))
        self.assertFalse(acct.trade_day(DayPath(((1_500, -1_200),), -1_200, True)))

    def test_eval_pass_and_consistency(self):
        win = DayPath(((1_000, 900),), 1_000, True)
        big = DayPath(((3_000, 2_900),), 3_000, True)
        flat = DayPath((), 0.0, False)
        paths = [win, flat, win, win, flat]
        self.assertEqual(run_eval(paths, 0, self.rules())[:2], ("pass", 4))
        # one $3,000 day breaks a 50% consistency rule until more profit comes in
        paths = [big, win, win, win, win]
        self.assertEqual(run_eval(paths, 0, self.rules(consistency=0.5))[:2], ("pass", 4))
        self.assertEqual(run_eval([win, flat], 0, self.rules())[0], "open")

    def test_day_paths_match_brute_force(self):
        days = random_walk_days(30, seed=2)
        trades = NoiseArea().run(days, ONE_TICK) + LastHalfHour().run(days, ONE_TICK)
        costs = Costs()
        paths = day_paths(trades, days, costs, 2)
        for day, p in zip(days, paths):
            mine = [t for t in trades if t.day == day.date]
            if not mine:
                self.assertFalse(p.traded)
                continue
            self.assertAlmostEqual(p.final, sum(costs.dollars(t.points, 2) for t in mine))
            # every minute's worst open equity is above the lowest segment low,
            # and the highest best equals the last segment's top
            lows, highs = [], []
            for m in range(390):
                closed = sum(costs.dollars(t.points, 2) for t in mine if t.exit_index < m)
                best = worst = closed
                open_any = False
                for t in mine:
                    if t.entry_index <= m <= t.exit_index:
                        b, w = t.path[m - t.entry_index]
                        best += 2 * (b * costs.point_value - costs.commission)
                        worst += 2 * (w * costs.point_value - costs.commission)
                        open_any = True
                if open_any:
                    lows.append(worst)
                    highs.append(best)
            self.assertAlmostEqual(min(w for _, w in p.segments), min(lows))
            self.assertAlmostEqual(p.segments[-1][0], max(highs))

    def test_summary_counts(self):
        days = random_walk_days(30, seed=4)
        trades = LastHalfHour().run(days, ONE_TICK)
        s = summarize(trades, days, Costs())
        self.assertEqual(s.trades, len(trades))
        self.assertAlmostEqual(s.trades_per_week, len(trades) / len(days) * 5)
        self.assertAlmostEqual(s.total, sum(Costs().dollars(t.points) for t in trades))


if __name__ == "__main__":
    unittest.main()
