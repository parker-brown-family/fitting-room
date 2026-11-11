## Problem

Tree undo does not recreate a deleted empty directory. Its current implementation
describes restoring files, so this issue concerns restoration scope rather than
an established security defect.

## Proof it's real

Checkpoint a tree containing an empty directory, remove it, then call undo.
Undo returns null and the directory stays absent.

## Reproduce

Run `python3 reports/adversarial/probe.py --only undo-empty-directory --out /tmp/directory-probe.json`.

## Verify / Invalidate

Attempt to invalidate first: if the directory is restored, close as stale. If
file-only restoration is the intended contract, make that explicit wherever
tree undo is described broadly and classify this as unsupported scope.

## Done when

Either empty directories are restored with regression coverage, or the user-facing
contract explicitly excludes them. Do not imply directory metadata is preserved
unless it is measured too.

## Context

Research evidence and executable probes: https://github.com/parker-brown-family/fitting-room/tree/ecdc6fa/reports/adversarial

Original tested production revision: de2210815b723567cb280aca0e28112ba69ddc31.
