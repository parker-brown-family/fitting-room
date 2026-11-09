---
title: Terminal Delight
tags: [harness-0, results, terminal-delight]
updated: 2026-11-09
runs: [runs/td-v1]
---

# Terminal Delight

Terminal Delight is one large Rust crate (gpui UI over the rio-vt core) with 2,121 tests and a
46 GB build directory, built against gpui through a relative path to `~/Work/zed-upstream`. It
is the hardest of the three projects to evaluate, and most of what this page records is about
making that possible at all.

## What it took

- **A build environment that belongs to the task.** Every eval workspace needs a shared cargo
  target (outside the repository, under `~/.cache/h0`), zed-upstream bound read-only at the
  relative path the crate expects, and a 10 GB memory ceiling, because the desktop had 7 GB free
  and twenty agents running. Both arms get the same environment; the fitting adds only knowledge.
- **A cold build.** The first test build under that ceiling took about 38 minutes at the lowest
  CPU priority; later ones are incremental.
- **Validation caught the fitting.** The generated TD fitting proposed `cargo test --lib`
  checks; TD is binary-only, so they could never have passed. They were corrected to
  `--bin terminal-delight`.

## The tasks

Four commits from TD's own history that changed an integration test under `app/tests/` and at
most four source files, each oracle-checked at mining time (the test fails with the source
reverted and passes with the commit's source). One of them, the bench check, did not hold up;
see below.

| task | the commit's subject |
|---|---|
| td-b4a7caf5 | The bench check plays a page's video as a clip in a hidden window and watches it close |
| td-fe457d34 | The real-Chromium tests skip, and say why, where the browser cannot start its sandbox |
| td-a5ace5bc | A closed pipe ends a headless verb quietly instead of printing a panic |
| td-112f92cc | A surface write no longer eats a concurrent update of the same id |

## The first scoring, and why it was wrong

Sonnet 5.5, one trial per arm, registered as exploratory (amendment 7), two workers. The run
finished at 01:16 on 2026-11-09 for $0.92 and said: vanilla 4 of 4, fitted 2 of 4.

The first version of this page blamed the fitted agent on the bench-check task, which "concluded
the failures came from the sandbox, and changed nothing". Reading both fitted failures end to end
said otherwise:

- **The closed-pipe task.** The fitted agent restored the default SIGPIPE handling for the verbs
  that only print and exit, then ran `cargo test --offline --test broken_pipe` in the same
  command: 2 passed, 0 failed. Its final check, a few minutes later, saw `--help` and
  `--version` panic on a closed stdout, paths its edit covers.
- **The bench-check task.** The fitted agent changed nothing; neither did its vanilla twin. The
  vanilla check passed all 74 tests; the fitted check failed 43 of them with "No such file or
  directory" for fixtures that exist in the workspace.

Both arms built into one shared cargo target, and the two workers ran two trials at once. Cargo
names a workspace member's outputs by its path relative to the workspace, which is the same in
every eval workspace, so the two trials overwrote each other's binaries. The closed-pipe check
ran a program built by the trial running beside it, on a commit from the day before the fix
(the surface-write task, whose tree has no SIGPIPE handling at all). The bench-check binary had
been compiled with the other workspace's `CARGO_MANIFEST_DIR`, a path the sandbox hides, so its
fixture lookups failed. The agent's reading, "the failures come from the sandbox, not from the
source", was nearer the truth than this page's.

## Re-scored one trial at a time

Amendment 10 records the defect and the analysis before the outcome was known. Trials, replays
and oracle runs now hold every writable cache their task shares (`h0.evals.exclusive`, an flock
per cache, with tests), so Terminal Delight's trials take turns while unrelated tasks still run
in parallel. Every td-v1 trial was then rebuilt from its own recorded tool calls in a fresh
workspace, one at a time, and its checks re-run (`h0 eval rescore`, no model calls). All eight
pass: both fitted failures were the shared directory's, and no pass turned out to be borrowed.

| task | vanilla (both scorings) | fitted, first scoring | fitted, re-scored |
|---|---|---|---|
| td-b4a7caf5 bench check | pass | fail | pass |
| td-fe457d34 Chromium sandbox skip | pass | pass | pass |
| td-a5ace5bc closed pipe | pass | fail | **pass** |
| td-112f92cc surface write | pass | pass | pass |

**The bench-check task measures nothing.** Both arms left the tree untouched and both pass, so
the reverted tree already passes its checks. Why its oracle failed the untouched tree at mining
time is not on record: the miner kept only a failing reference run's output, and it mined one
candidate at a time, so it was not this defect. Its tests drive a real Chromium, and the machine
was running three other evaluations then. Re-run one task at a time, holding the build directory, the oracle agrees: the bench-check tree passes untouched, and the other three fail untouched (one does not compile, two fail their tests) and pass with their commit's source (`evals/terminal-delight/oracle-rerun-2026-11-09.log`). The task now lives in `evals/terminal-delight/invalid.jsonl`, with the reason.

## What is left

On the three valid tasks both arms pass everything, as on HC Video. The fitted arm cost less on
all three pairs ($0.270 against $0.370 in total) and on the fourth, invalid one, though three
pairs cannot show a difference in anything. Terminal Delight's mined tasks name their failing
test in the prompt, which puts them within reach of a frontier model without help.

The lasting result from this project is about the instrument. A harness eval is a second harness,
and it needs the same isolation it gives the agents: two trials that share a writable cache are
not independent, and nothing in either trial's own record shows it.
