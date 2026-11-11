## Problem

`TreeUndo._blob` treats unchanged size, mtime and inode as unchanged contents.
A same-size overwrite followed by timestamp restoration defeats change detection.

## Proof it's real

After checkpointing `before!`, overwrite it with `after!!` on the same inode and
restore `st_mtime_ns`. `changes()` reports no files; `undo()` returns null and
leaves the changed bytes. The ordinary edit control restores the original.

## Reproduce

Run `python3 reports/adversarial/probe.py --only undo-metadata-spoof --out /tmp/undo-probe.json`.

## Verify / Invalidate

Try to invalidate first: if both control and metadata-preserved cases detect
and restore the original contents on current source, close as invalid with the
results. Check actual file bytes; a nonempty change list alone is insufficient.

## Done when

Metadata preservation cannot hide a changed small file from either the diff or
undo, with a regression covering the same-inode case.

## Context

Research evidence and executable probes: https://github.com/parker-brown-family/fitting-room/tree/ecdc6fa/reports/adversarial

Original tested production revision: de2210815b723567cb280aca0e28112ba69ddc31.
