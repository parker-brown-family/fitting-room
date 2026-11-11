# Offline adversarial experiments

The repository's original 128-test suite passed, with one skip. Separate probes
then reproduced gaps in secret masking, undo, trace verification and evaluation.
Production code is unchanged. No paid model request was made.

Tested source: `de2210815b723567cb280aca0e28112ba69ddc31`. This is a research branch,
not a claim that all the findings need the same fix or have the same severity.

## Reproduce

Run from the repository root:

```bash
H0_JUDGE=off python3 -P -m unittest discover -s tests -t .
python3 reports/adversarial/probe.py
python3 reports/adversarial/probe.py --only undo-metadata-spoof --out /tmp/undo-probe.json
```

The probe runner returns 1 when it falsifies a tested claim, 2 for blocked or
broken experiments, and 0 when all requested claims survive. A returned 1 is
preserved evidence, not a passing regression suite. Controls check that the
sandbox runs, shallow secrets are hidden, ordinary undo works and trace edits
are detected. Probe workspaces and synthetic canaries are removed after use.

Read [results.json](results.json) for current evidence and
[results-initial.json](results-initial.json) for the earlier pass. The initial
secret probe treated a denied read as an experimental failure; the corrected
probe records a denied read as successful masking. It now also exercises the
complete Toolbox path, including policy and output redaction.

## Ranked findings

| Rank | Finding | Evidence and scope |
|---|---|---|
| 1 | Secret masking misses deep, skipped and newly created repository files | A synthetic canary in `node_modules/pkg/.env` reaches the complete bash tool. The sensitive flag stays false when the command locates the file without spelling its name. A subsequent network policy check allows access. No network command or real credential read occurs. |
| 2 | A task can pass by rewriting its checker | The untouched implementation returns 0 instead of 42. Replacing its workspace checker with `SystemExit(0)` changes `run_checks` from fail to pass. This demonstrates a permitted oracle design, not cheating in a historical trial. |
| 3 | Undo misses changed bytes with preserved size, inode and timestamp | Overwrite seven bytes with seven different bytes and restore the old nanosecond timestamp. The cached hash is reused; the change list is empty and undo returns null. Ordinary edits restore correctly. |
| 4 | Trace verification does not verify anchored completeness | Keep the original anchor but remove the summary, keep only metadata, or empty the trace. `verify()` returns true. Interior deletion, edits and partial JSON fail their controls. No hash forgery is claimed. |
| 5 | Reported total cost omits recorded spend on unscored trials | A synthetic successful trial costs $0.01 and an unscored trial costs $10. The reported total is $0.01. An audit of 18 local archives found no unscored rows, so no historical omitted spend was established. |
| 6 | Nested Git protections have bypasses already documented by the project | New Git directories survive below skipped directories or beyond the depth limit. Existing nested Git configuration is mutable. This confirms the security model's “Not yet covered” section; it is not a new discovery or a demonstrated namespace escape. |
| 7 | Deleted empty directories are outside current undo behavior | Undo returns null and does not recreate the directory. Treat this as a restoration-scope question; the implementation explicitly describes file restoration. |

The first three findings should be addressed before collecting more paid
evidence. Fitting effectiveness remains a separate empirical question.

## Root causes

Secret masks are a bounded, one-time scan in `Sandbox._scan_secret_files` in
[sandbox.py](../../h0/sandbox.py). The whole project remains writable/readable in
the mount namespace. Secret-name heuristics and output redaction are additional
layers, but the canary demonstrates that neither is an authoritative boundary.

`run_checks` in [evals.py](../../h0/evals.py) trusts commands that may execute
files in the submitted workspace. It does not freeze checker content. Oracle
validation before a trial does not establish oracle integrity after a trial.

`TreeUndo._blob` in [undo.py](../../h0/undo.py) caches by size, modification time
and inode. A timestamp is a useful performance hint, but a writer can preserve
it. `Trace.verify` in [trace.py](../../h0/trace.py) checks links in the supplied
prefix without comparing its final hash/count with `anchors.log`.

`summarize` in [cmd_eval.py](../../h0/cmd_eval.py) computes cost from scored rows.
Unknown cost should remain unknown; known cost on a failed attempt still belongs
in actual expenditure. Snapshot archives and retries require separate accounting.

## Archived evidence

```bash
python3 reports/adversarial/audit_runs.py /home/parker/Work/harness-0/runs
```

[archive-audit.json](archive-audit.json) records file hashes, numeric accounting
and task-level comparisons for 18 immediate-child run archives. It reads no
traces, replays no commands and calls no models. Archives may overlap and must
not be added together as independent trials or global spend.

The local 9B comparison contains 22 pairs across 11 tasks. Its equal-task-weight
success difference is +13.6 percentage points. Resampling whole tasks gives a
95% interval of approximately −18.2 to +45.5 points; the task sign-flip
sensitivity gives p=0.59375. This does not establish a success advantage.
These methods assume exchangeability/sampling conditions that the archive
does not establish. An all-zero bootstrap interval at ceiling is an empirical
degeneracy, not proof that the true effect is exactly zero.

## Next scientific experiment

Freeze a new suite before inspecting new model outcomes. Select tasks from a
dated workload sample, including unsuccessful work, rather than choosing tasks
because a fitting already carries their answers. Record task-family membership
and split whole families between fitting development and held-out evaluation.

Use five arms: none, map, rules, lessons and all three. Hold the sandbox,
permissions, tool schemas, model revision, inference settings and budget fixed.
Declare any extra finish checker as a separate experimental factor. Freeze and
hash evaluator inputs outside the agent's writable tree; validate broken and
reference solutions and test evaluator tampering before collecting outcomes.

Report task-weighted success, unscored counts, all-attempt known spend, missing
cost readings, wall time and tool/model latency. Randomize arm order within
tasks; record load/cache state. Repeated trials are observations within a task,
not additional sampled tasks. Use intervals at that sampling level and publish
all registered runs. Do not repeatedly extend a favorable sample until it passes
a threshold. An adequate task count needs a power analysis using a pilot's
discordance and a chosen minimum worthwhile effect; none is asserted here.

## Local pilot

`local_ablation.py` supplies a bounded exploratory implementation of those five
content arms on two authored Python tasks. It reuses h0's existing provider and
loop, permits only `127.0.0.1:11434`, disables proxies/redirects and requires an
already installed model. It does not download a model or start a service.

```bash
python3 reports/adversarial/local_ablation.py --out /tmp/h0-local-pilot-new
```

The output directory must be new. Preregistration, source hash and broken/reference
oracle checks are saved before inference. Every completed trial is appended
immediately, including errors. The trusted checker is a command owned by the
experiment, rather than a file in the writable fixture.

Two authored tasks, one trial per arm and uncontrolled server/cache conditions
cannot estimate general benefit. This pilot is a working apparatus check. The
initial `local-pilot` directory preserves an aborted setup with a trigger-field
mistake; its incomplete trace is not a completed trial. `local-pilot-v2` uses
the corrected `note` field and checks that it injects before any model request.

The corrected run stopped at its 30-minute execution ceiling. Seven trials
completed: three passed and four failed. One was interrupted and is unscored;
two were never started. [The disposition](local-pilot-v2/disposition.json)
preserves those distinctions. The local server was shared, and another loaded
model was observed during the run. The 10.2–601.8 second completed-trial range
cannot establish a fitting-speed advantage. Among the failures, two retained
false/zero/empty-string values but mishandled an explicitly present null; another
fixed interval merging while still mutating the caller's input.

The initial aborted source has been recovered byte-for-byte and checked against
its original saved hash in `local-pilot/source.py`. `finalize_pilot.py` records
the stopped pilot without rescoring partial work or replacing missing values
with failure. No local pilot process remains; the shared Ollama service is left
alone.
