"""
The desk's $ volume floor: equities on their latest ET session, crypto on its last 24 hours against a
quarter of the floor ($2M / $0.5M at the default, as the README and /api/scan always said).

The desk passed its floor to data.passes_volume_filter, which applied it to crypto unchanged, and
crypto's "session" was its ET day so far: at 00:55 ET only 9% of the desk's crypto names cleared it.
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
        self.assertAlmostEqual(session_dollar_volume(df), 15_000)                 # the ET day so far
        self.assertAlmostEqual(session_dollar_volume(df, "BTC-USD"), 288 * 5_000)
        self.assertEqual(passes_volume_filter("BTC-USD", df, 2e6), (True, 288 * 5_000))   # $1.44M ≥ $0.5M
        self.assertEqual(passes_volume_filter("AAPL", df, 2e6), (False, 15_000))
        self.assertTrue(passes_volume_filter("AAPL", df, 0)[0])                    # 0 turns the floor off

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
