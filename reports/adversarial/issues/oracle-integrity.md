## Problem

`run_checks` can execute a checker that the evaluated agent was allowed to
rewrite. Initial oracle validation does not preserve its integrity during a trial.

## Proof it's real

The `eval-oracle-tampering` probe starts with `answer() == 0` and a checker
requiring 42. The check fails. Replacing only `check.py` with `SystemExit(0)`
makes the same check pass while `answer.py` remains byte-for-byte broken.
This shows a permitted task construction, not historical cheating.

## Reproduce

Run `python3 reports/adversarial/probe.py --only eval-oracle-tampering --out /tmp/oracle-probe.json`.

## Verify / Invalidate

Attempt to invalidate first: on current source, show either that the agent cannot
change the checker or that final scoring rejects its changed contents. If this
task design is intentionally unsupported, document that restriction and make
task validation reject it. Do not infer historical score contamination.

## Done when

A broken implementation cannot receive a passing score by changing workspace
checker files. Freeze/hash or isolate the trusted oracle and cover the mutation.
