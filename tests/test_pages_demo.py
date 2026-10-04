"""
The public demo (index.html + engine.js, mirrored in cf-pages/) must not drift from the desk:
same universe, same engine version, and a results warning whose numbers are the replay files it
links to (one for equities, one for crypto). Engine behaviour itself is covered by
test_engine_parity.py.
"""

import json
import re
import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from data import load_universe          # noqa: E402
from engine import ENGINE_VERSION       # noqa: E402
from providers import looks_crypto      # noqa: E402

HTML = (ROOT / "index.html").read_text(encoding="utf-8")


class TestPagesDemo(unittest.TestCase):

    def test_universe_is_the_desk_universe(self):
        embedded = json.loads(re.search(r"const UNIVERSE = (\[.*?\]);", HTML, re.S).group(1))
        self.assertEqual(embedded, load_universe())

    def test_cloudflare_copy_is_identical(self):
        for name in ("index.html", "engine.js"):
            self.assertEqual((ROOT / "cf-pages" / name).read_bytes(), (ROOT / name).read_bytes(), name)

    def test_page_names_the_engine_version(self):
        title = re.search(r"<title>(.*?)</title>", HTML).group(1)
        self.assertIn(f"v{ENGINE_VERSION}", title)
        self.assertIn('<script src="engine.js"></script>', HTML)

    def test_results_warning_is_the_linked_replays(self):
        """Each sentence's numbers are the replay of this engine it links: equities, then crypto."""
        banner = re.search(r'<div id="honest"[^>]*>(.*?)</div>', HTML, re.S).group(1)
        links = re.findall(r'blob/main/(research/[^"]+\.json)"[^>]*>([^<]+)</a>', banner)
        self.assertEqual([label for _, label in links], ["equities", "crypto"])
        text = " ".join(re.sub(r"<[^>]+>", "", banner).replace("−", "-").split())
        spans, at = [], 0
        for path, label in links:
            replay = json.loads((ROOT / path).read_text())
            self.assertEqual(replay.get("engine_version"), ENGINE_VERSION, path)
            crypto = [looks_crypto(t) for t in replay["tickers"]]
            self.assertTrue(all(crypto) if label == "crypto" else not any(crypto), path)
            first, last = (date.fromisoformat(d) for d in replay["method"]["window"])
            window = f"{first:%b} {first.day} – {last:%b} {last.day}"
            at = text.index(window, at)                                  # each replay's sentence, in order
            spans.append((at, replay["summaries"]["classic"]["net_clustered"], path))
        for i, (start, net, path) in enumerate(spans):
            sentence = text[start: spans[i + 1][0] if i + 1 < len(spans) else len(text)]
            for needle in (f"{net['mean']:.2f}R", f"± {net['se']:.2f}", f"{net['n']} trades",
                           f"{net['clusters']} sessions", f"t {net['t']:.1f}"):
                self.assertIn(needle, sentence, path)


if __name__ == "__main__":
    unittest.main()
