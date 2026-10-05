"""
The desk's $ volume floor: equities on the larger of their latest ET day so far and the day before,
crypto on its last 24 hours against a quarter of the floor ($2M / $0.5M at the default, as the README
and /api/scan always said).

The desk passed its floor to data.passes_volume_filter, which applied it to crypto unchanged, and both
were measured on the ET day so far: at 00:55 ET only 9% of the desk's crypto names cleared it, and in
premarket, once a stock first traded, 27% of its stocks at 04:30 ET and 9.5% at 09:00.
engine.js carries the same rules for the Pages demo (tests/test_engine_parity.py).
"""

import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app                                                                            # noqa: E402
from data import min_dollar_volume_for, passes_volume_filter, session_dollar_volume  # noqa: E402

ET = ZoneInfo("America/New_York")


def bars(start, end, dollars_per_bar, price=100.0):
    """5m bars from `start` through `end` (ET), each trading `dollars_per_bar` of notional."""
    idx = pd.date_range(start, end, freq="5min")
    n = len(idx)
    return pd.DataFrame({"Open": [price] * n, "High": [price * 1.001] * n, "Low": [price * 0.999] * n,
                         "Close": [price] * n, "Volume": [dollars_per_bar / price] * n}, index=idx)


class TestLiquidityFloor(unittest.TestCase):

    def test_crypto_gets_a_quarter_of_the_desk_floor(self):
        self.assertEqual((min_dollar_volume_for("AAPL", 2e6), min_dollar_volume_for("BTC-USD", 2e6)), (2e6, 5e5))
        self.assertEqual((min_dollar_volume_for("AAPL"), min_dollar_volume_for("SOL-USD")), (2e6, 5e5))
        self.assertEqual(min_dollar_volume_for("BTC-USD", 8e6), 2e6)      # a stricter desk is stricter on crypto too

    def test_crypto_volume_is_its_last_24_hours(self):
        # $5k a bar; ten minutes after ET midnight the new ET day holds three bars
        df = bars(datetime(2026, 9, 14, 0, 0, tzinfo=ET), datetime(2026, 9, 15, 0, 10, tzinfo=ET), 5_000)
        self.assertAlmostEqual(session_dollar_volume(df, "BTC-USD"), 288 * 5_000)
        self.assertEqual(passes_volume_filter("BTC-USD", df, 2e6), (True, 288 * 5_000))   # $1.44M ≥ $0.5M
        # as a stock: the larger of the new day's three bars ($15k) and the whole day before
        self.assertEqual(passes_volume_filter("AAPL", df, 2e6), (False, 288 * 5_000))     # $1.44M < $2M
        self.assertTrue(passes_volume_filter("AAPL", df, 0)[0])                    # 0 turns the floor off

    def test_a_stock_in_premarket_keeps_its_prior_session(self):
        """Its first premarket bars no longer drop a liquid stock under the floor; a thin one still needs a
        heavy day, and gets in once today's volume clears it."""
        def day(d, until, dollars):
            start, end = datetime(2026, 10, d, 4, 0, tzinfo=ET), datetime(2026, 10, d, *until, tzinfo=ET)
            return bars(start, end, dollars / len(pd.date_range(start, end, freq="5min")))
        liquid = pd.concat([day(2, (19, 55), 3e6), day(5, (4, 25), 30e3)])     # Friday $3M, Monday 04:00-04:25
        news = pd.concat([day(2, (19, 55), 0.5e6), day(5, (10, 0), 2.5e6)])    # a thin name's heavy morning
        quiet = pd.concat([day(2, (19, 55), 0.5e6), day(5, (4, 25), 30e3)])
        ok, dvol = passes_volume_filter("LIQ", liquid, 2e6)
        self.assertTrue(ok)
        self.assertAlmostEqual(dvol, 3e6)
        self.assertEqual(passes_volume_filter("NEWS", news, 2e6)[0], True)
        self.assertAlmostEqual(passes_volume_filter("NEWS", news, 2e6)[1], 2.5e6)
        self.assertEqual(passes_volume_filter("QUIET", quiet, 2e6)[0], False)

    def test_desk_flags_each_name_against_its_own_floor(self):
        """A coin and a stock that each traded $1M: the coin clears its $0.5M floor, the stock misses $2M."""
        coin = bars(datetime(2026, 9, 14, 0, 0, tzinfo=ET), datetime(2026, 9, 15, 23, 55, tzinfo=ET), 1e6 / 288)
        stock = pd.concat([bars(datetime(2026, 9, d, 4, 0, tzinfo=ET), datetime(2026, 9, d, 19, 55, tzinfo=ET),
                                1e6 / 192) for d in (14, 15)])
        fetched = ({"COIN-USD": coin, "THIN": stock}, {}, {}, {}, {})
        with mock.patch.object(app, "batch_fetch", return_value=fetched), mock.patch.object(app, "LEDGER_ON", False):
            res = app.run_scan(["COIN-USD", "THIN"], max_n=5, grade_min="C", min_dvol=2e6)
        rows = {r["ticker"]: r for r in res["results"]}
        self.assertAlmostEqual(rows["COIN-USD"]["dollar_vol"], 1e6, delta=1)
        self.assertAlmostEqual(rows["THIN"]["dollar_vol"], 1e6, delta=1)
        self.assertEqual((rows["COIN-USD"]["illiquid"], rows["THIN"]["illiquid"]), (False, True))
        self.assertEqual((res["meta"]["min_dvol"], res["meta"]["min_dvol_crypto"]), (2e6, 5e5))


if __name__ == "__main__":
    unittest.main()
