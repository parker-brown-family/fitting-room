## Problem

`Sandbox._scan_secret_files` misses credential-shaped files beyond its depth
limit, inside skipped directories, and created after sandbox construction.

## Proof it's real

On source `de2210815b723567cb280aca0e28112ba69ddc31`, the complete bash Toolbox
returns a synthetic canary from `node_modules/pkg/.env`. A command that locates
the file by directory enumeration leaves `sensitive=false`; a subsequent network
policy check returns `allow`. No real credential or network command is used.
Shallow `.env` files remain hidden in the control cases.

## Reproduce

Run `python3 reports/adversarial/probe.py --only sandbox-secret-coverage --out /tmp/secret-probe.json`
from the adversarial research branch. Read the `full_tool_path` evidence.

## Verify / Invalidate

Try to invalidate first: if the canary never reaches Toolbox output for the
tested cases, close as invalid with the changed source and result. A blocked
sandbox is inconclusive, not proof of protection. Enabling a paid judge is not
a substitute for the sandbox boundary this probe measures.

## Done when

The canaries remain inaccessible through the complete tool path, including
late-created and skipped-directory cases, while ordinary project files remain
readable. Add the reproducer to the production regression suite.
