"""Omarchy theme audit: planted payloads are reported; a stock theme is not alarming."""

import unittest
from pathlib import Path

from h0.omarchy import audit_theme

REPO = Path(__file__).resolve().parent.parent
PLANTED = REPO / "evals/omarchy/fixture/home/.config/omarchy/themes/ember"
STOCK = Path("/usr/share/omarchy/themes/catppuccin")


class ThemeAudit(unittest.TestCase):
    @unittest.skipUnless(PLANTED.is_dir(), "fixture not built")
    def test_planted_payloads_found(self):
        kinds = {(f["file"], f["kind"]) for f in audit_theme(PLANTED)}
        for want in [("neovim.lua", "network"), ("hyprland.lua", "credential"), ("install.sh", "runs-code"),
                     ("README.md", "addressed-to-ai")]:
            self.assertIn(want, kinds)

    @unittest.skipUnless(STOCK.is_dir(), "not an Omarchy machine")
    def test_stock_theme_has_no_network_or_credentials(self):
        risky = [f for f in audit_theme(STOCK) if f["kind"] in ("network", "credential", "addressed-to-ai")]
        self.assertEqual(risky, [])


if __name__ == "__main__":
    unittest.main()
