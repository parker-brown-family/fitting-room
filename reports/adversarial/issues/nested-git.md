## Problem

The security model already documents gaps in bounded nested-Git scanning and
mutable configuration in pre-existing nested repositories. These gaps now have
an executable reproducer; this is not a new discovery.

## Proof it's real

`sandbox-nested-git` removes a newly planted shallow `.git` in its control, but
leaves one in `node_modules/pkg` and another seven directories deep. A config
inside an existing vendored repository is writable. All payloads are inert;
the experiment executes no host hook or network request.

## Reproduce

Run `python3 reports/adversarial/probe.py --only sandbox-nested-git --out /tmp/git-probe.json`.

## Verify / Invalidate

Try to invalidate first: if planted entries no longer survive and existing
configuration is protected, close as stale. If preserving these paths is an
explicit trust decision, constrain the security claim and record that decision
rather than claiming total protection. Read the existing security model first.

## Done when

Resolve or explicitly scope the documented persistence boundaries, and retain
the executable checks so later changes cannot erase the evidence.
