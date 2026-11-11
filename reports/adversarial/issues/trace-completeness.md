## Problem

`Trace.verify` checks the hash chain in the supplied prefix but ignores the
closed session's final anchor and event count. It accepts deleted suffixes.

## Proof it's real

With the original `anchors.log` intact, the trace probe removes the summary,
keeps only the first event, and empties the file. All return `ok=true`. Edited
records, interior deletions and a partial final JSON line fail the controls.
This is a completeness gap, not a broken SHA-256 claim or hash forgery.

## Reproduce

Run `python3 reports/adversarial/probe.py --only trace-anchored-completeness --out /tmp/trace-probe.json`.

## Verify / Invalidate

Attempt to invalidate first: if a closed anchored trace with a missing suffix
is rejected, the finding is stale. If prefix verification is intentional, make
its result explicitly distinguish prefix validity from anchored completeness;
an absent anchor must remain unverified rather than complete.

## Done when

A reader can distinguish a valid open/crashed prefix, a complete anchored trace,
and a closed trace whose events were removed. Preserve useful crash recovery.

## Context

Research evidence and executable probes: https://github.com/parker-brown-family/fitting-room/tree/ecdc6fa/reports/adversarial

Original tested production revision: de2210815b723567cb280aca0e28112ba69ddc31.
