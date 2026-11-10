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


class OmarchyVersionTests(unittest.TestCase):
    def test_the_version_comes_from_omarchy_itself_before_any_stale_file(self):
        # 2026-11-10: /usr/share/omarchy/version said 4.0.0.alpha on a box running 4.5.0-3, and the
        # model was told the older version in every session
        import os
        import tempfile
        from unittest import mock
        from h0.context import omarchy_version
        d = tempfile.mkdtemp()
        cmd = os.path.join(d, "omarchy-version")
        with open(cmd, "w") as f:
            f.write("#!/bin/sh\necho 9.9.9-1\n")
        os.chmod(cmd, 0o755)
        with mock.patch.dict(os.environ, {"PATH": d + os.pathsep + os.environ.get("PATH", "")}):
            self.assertEqual(omarchy_version(), "9.9.9-1")
        with open(cmd, "w") as f:
            f.write("#!/bin/sh\nexit 1\n")
        with mock.patch.dict(os.environ, {"PATH": d + os.pathsep + os.environ.get("PATH", "")}):
            self.assertNotEqual(omarchy_version(), "9.9.9-1")     # a failing command falls back to the files
