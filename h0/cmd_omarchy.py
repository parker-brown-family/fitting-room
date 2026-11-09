"""h0 omarchy audit-theme <dir>"""

from __future__ import annotations

import json
from pathlib import Path

from h0.omarchy import audit_theme


def cmd_audit(args) -> int:
    findings = audit_theme(Path(args.dir))
    if args.json:
        print(json.dumps(findings, indent=1))
    else:
        for f in findings:
            loc = f"{f['file']}:{f['line']}" if "line" in f else f["file"]
            print(f"{f['kind']:<16} {loc:<28} {f.get('why') or f.get('text', '')}")
        print(f"{len(findings)} findings" if findings else "no code, network or credential references found")
    risky = any(f["kind"] in ("network", "credential", "addressed-to-ai") for f in findings)
    return 2 if risky else 0


def register(sp) -> None:
    o = sp.add_parser("omarchy", help="Omarchy-specific checks")
    osp = o.add_subparsers(dest="omarchy_cmd")
    a = osp.add_parser("audit-theme", help="report code, network and credential references in a theme folder")
    a.add_argument("dir")
    a.add_argument("--json", action="store_true")
    a.set_defaults(func=cmd_audit)
