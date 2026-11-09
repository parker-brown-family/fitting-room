---
title: Eval Method
tags: [harness-0, eval, rationale]
updated: 2026-11-08
---

# Eval Method

The question is narrow: with the model held fixed, does a fitting make h0 better at one
project's work? So every comparison is paired. The same task, the same model and the
same trial number run once in each arm, and the arms differ only in the fitting.

## Where the method comes from

| Practice | Precedent | How h0 uses it |
|---|---|---|
| Hold the model fixed, vary the harness | LangChain's Terminal-Bench 2.0 work; the Empirical Harness Design study (176 matched settings) | arms share model, tools, sandbox and loop |
| Tasks from the project's own history | SWE-bench; SWE-smith's task synthesis | `h0 eval mine`: commits that changed tests and source become tasks |
| A reference solution for every task | Terminal-Bench's oracle requirement | `h0 eval validate`: fails untouched, passes with the solution |
| Consistency, not one lucky run | tau-bench's pass^k | pass^2 and pass^3 in every table |
| Cost on the same footing as accuracy | HAL; *AI Agents That Matter* | dollars and tokens per trial, paired differences with intervals |
| Specification before data | directiveforge's pre-registered report card | PREREG, committed before the first run |
| Frozen fixtures, typed traces, honest gaps | tcballard/build-omarchy-themes evaluation protocol | `host_metadata` first and `host_summary` last; HOME is the fixture when it is a home |

## The protocol

1. **Write tasks, then prove them.** Every task carries checks that run in the sandbox
   after the agent stops. `h0 eval validate` runs them on the untouched workspace (they
   must fail) and after the reference solution (they must pass). Mined tasks are kept
   only if both hold.
2. **Register before running.** Hypotheses, arms, models, measures, analysis and threats
   go in PREREG and into git first. Anything changed later is a dated
   amendment with its reason.
3. **Run paired.** `h0 eval run` creates a disposable workspace per trial (a shared git
   clone, or a copy of a fixture), runs the agent headless, then the checks. A rerun
   resumes where it stopped.
4. **Read the failures.** Before a number is believed, the failed trials are opened.
5. **Report with intervals and the unscored count.** Exact McNemar on discordant pairs,
   bootstrap intervals for paired dollar and latency differences, Wilson intervals on
   rates.

## Four things reading the failures caught

**A check that failed the right answer.** The first keybinding check stripped Lua
comments by splitting on `--`, which also cut `"uwsm-app -- nautilus"`, the idiomatic
answer. The oracle step caught it before any run.

**A check that rejected real API.** The learning loop's reflector reported that agents
"switched to `o.exec_on_start`". The check accepted only `o.launch_on_start`; Omarchy's
`helpers.lua` defines both. The check was corrected (amendment 1), and every autostart
trial was re-scored by **trace replay**: `h0 eval rescore` rebuilds each trial's final
workspace by replaying its recorded tool calls into a fresh one, then runs the corrected
check. Five trials moved from fail to pass and none the other way; replaying 42 trials of
two other tasks changed nothing.

**A replay that was not faithful.** The first replay turned passes into failures because
agents addressed their workspace by absolute path, and each replay workspace has a new
name. The replayer now rewrites the original root to the new one.

**A home that was not home.** Omarchy tasks treat the workspace as `~`, but inside the
sandbox `~` was the masked real home, so a `cd ~` edited a throwaway copy. Tom Ballard's
host contract sets `HOME` to the fixture when it is a home directory; h0 does the same
for every arm (amendment 4).

## Simulated local latency

Every trial reports `sim_local_s`: new prompt tokens at a prefill speed plus output
tokens at a decode speed (300 and 16 tokens a second by default, a "16 tok/s"
machine). Tokens served from a cache count as already prefilled, which is what a local
server with prefix caching does. Measured on this machine, `qwen3.5:9b` on an RTX 3080
Laptop GPU prefills 2,150 tokens a second at harness scale and decodes 51, so the
default profile describes a slower machine than this one. [local](https://github.com/parker-brown-family/omarchy-fitted-harness/blob/main/docs/results/local.md) reports both.

## Threats this method does not remove

- Small suites. Seven, four and fourteen tasks detect only large effects; a null result
  is "not detected".
- One author wrote the tasks and the fitting briefs. The generated fittings were searched
  for task answers before any run.
- Structural checks can reject unusual but valid answers. The first night found two such
  checks; there may be more.
- A shared, loaded machine. Wall time in these runs includes other agents' builds;
  token counts and dollars do not.
