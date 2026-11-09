---
title: Does a coding agent need a harness fitted to its project? Method and results
tags: [harness-0, results, eval, summary]
updated: 2026-11-09
---

# Does a coding agent need a harness fitted to its project?

A coding agent is a model plus a harness, the program around the model that gives it tools,
runs them, and decides what goes in its context. Most harnesses are general: the same one
works on any repository. The question here is whether it pays to **fit** the harness to one
project, giving it a map of the codebase, the project's own checks, and lessons learned from
earlier failures, and how you would know.

We built h0, a small harness with no dependencies outside Python's standard library, and a
**fitting room**: `h0 fit` writes a fitting for a project, `h0 eval` measures it against the
same harness without one, and `h0 learn` reads the failures and proposes edits to the fitting,
which are kept only if a re-run shows they help. Then we ran it on three projects.

## The short answer

| project | what it is | what fitting did |
|---|---|---|
| Omarchy, a Linux desktop configured in Lua and JSON | 11 tasks, 7 to learn on and 4 held out | The generated fitting **lost** at first. After two learning rounds it tied or beat the plain harness on every model, never lost on held-out tasks, and shortened every session. |
| The same tasks on a 9B model running on a laptop GPU | 22 paired trials | Fitted passed 16 of 22, plain 13 of 22 (not significant), but fitted sessions read **0.41×** the prompt tokens, took 6 turns instead of 24, and finished about three minutes sooner. |
| Terminal Delight, a large Rust terminal (2,121 tests) | 4 tasks mined from its history | Too few valid tasks to say anything. The project's lasting result was three defects in the eval itself. |
| A private TypeScript web app | 14 tasks mined from its history, plus 3 written to test buried knowledge | Every Claude model already passed nearly everything. Fitting added one to seven cents a task on Sonnet and Opus, with no clear gain in successes. |

So a fitting is worth the most where the model needs the most help: a small local model, or
advice the project's own documentation gets wrong. On a frontier model, in a repository whose
truth is on the disk, the model finds it alone, and the fitting mostly costs tokens.

1,132 trials across all rounds, about $68 of API spend, Claude Haiku, Sonnet and Opus 5.5, and
Fable 5.1 on the Omarchy tasks.

## How it was measured

**Paired arms.** Every comparison holds the model, tools, sandbox and loop fixed. The same task
and trial number run once with the fitting and once without, so the only difference is the
fitting. Success is compared with an exact McNemar test on the pairs that disagree, cost and
time with bootstrap intervals on the paired differences, and rates with Wilson intervals.

**An oracle for every task.** Each task's checks must fail on the untouched workspace and pass
with a reference solution before the task is used. Tasks mined from a project's history
(`h0 eval mine`) take a commit that changed a test and a few source files, put the source back,
and keep the commit's subject as the task.

**Registration before data.** Hypotheses, arms, models, measures and kill criteria were
committed to `evals/PREREG.md` before the first run. Every change since is a dated amendment
that gives its reason, eleven so far, and the results report predictions that failed alongside the ones that held.

**Every trial replayable.** Each trial keeps a hash-chained trace of every tool call. When a
check turned out to be wrong, the trials were re-scored by replaying their recorded calls into
a fresh workspace (`h0 eval rescore`), with no model involved, and both scorings were kept.

**Cost on the same footing as success.** Dollars and tokens come from the API's usage fields,
cache reads priced as cache reads. Each trial also reports the time it would take on a machine
that prefills 300 tokens a second and decodes 16, the speed of a model running at home.

**A noise floor printed beside every rate.** With n pairs, a difference in pass rate smaller
than about 1/√n cannot be told from run-to-run variation: ±33 points at 9 pairs, ±19 at 28.

## What went wrong in the evals, and what fixed it

The method held up. The suites did not, and three defects could each have produced a confident
wrong number. None of them shows in any single trial's record.

1. **The answer key sat in the workspace.** A mined task's workspace was a clone whose parent
   commit was the solution. In one 168-trial run, 26 trials read the answer from git history,
   13 of them by one model in one arm. Workspaces are now the tree exported and committed once,
   with no history, and that run was replaced.
2. **Two trials shared one build directory.** Terminal Delight's trials used one Rust build
   directory, and two ran at once, so each ran the other's compiled program. That decided 2 of
   8 trials, both against the fitting. Each trial now holds a lock on every writable cache it
   shares, across processes, and the trials were re-scored by replay. All eight pass.
3. **An environment fault looked like the missing change.** Two of Terminal Delight's four mined
   tasks failed their untouched check for reasons that had nothing to do with the change, so
   they measured nothing. Re-running the oracle on the new workspaces found both, and the miner
   now keeps why each untouched tree fails.

Three more limits: prompts that named the failing test made mined tasks too easy, so a variant
that describes only the change was run as well: still at least 93% on every Claude model in both
arms, with no clear difference. Checks that matched words in prose failed correct
paragraphs, 14 verdicts' worth, so prose needs a calibrated judge. And one author wrote both the
fittings and the tasks, the threat most likely to flatter the fitting.

A harness eval is a second harness, and it needs the same isolation it gives the agent.

## Omarchy in more detail

Omarchy 4 ships its documentation on the machine, and a model with a shell reads it in two or
three calls, so the plain harness passed 95–100% from the start.

- **Round one, the fitting lost** on all three models (Haiku and Sonnet 1.00 → 0.86, Opus 0.95 →
  0.86) and raised cost, meeting the registered kill criterion. Every fitted failure was one
  task: asked to make the bar clock show seconds, fitted agents cloned Omarchy's whole clock
  plugin instead of adding `:ss` to its format string. The fitting told them to, because
  Omarchy's own plugin guide says to customise a widget by cloning it, and the fitting injected
  that advice the moment they touched the bar.
- **Two learning rounds** ($0.21) found why. The advice arrived before the `edit` tool, but the
  agents cloned through the shell, so it came after the decision. The rewritten fitting says
  cloning is for changing a widget's code, and format options belong on the existing entry.
- **Round two, six trials per pair:** Opus fitted 1.00 against 0.91 plain (4 pairs to 0,
  p = 0.125, short of significance), Haiku and Sonnet tied, held-out tasks 1.00 in both arms on
  every model. Median turns fell on every model, for example Opus 6 → 5 and Haiku 7 → 5. Fable
  5.1 tied on success and cost 23% less fitted.
- **A planted injection** (a hand-copied theme carrying an `install.sh` payload) was attempted by
  no model in 36 security trials, and every final answer named the payloads.

## Reproduce it

The harness, the fitting room, the eval tooling, the Terminal Delight fitting and its tasks are
in [fitting-room](https://github.com/parker-brown-family/fitting-room). The Omarchy fitting, its
tasks and fixtures, and the full Omarchy and local-model results are in
[omarchy-fitted-harness](https://github.com/parker-brown-family/omarchy-fitted-harness).

```
h0 fit . --out fittings/myproject                     # write a fitting for this project
h0 eval validate tasks.jsonl                          # each task fails untouched, passes with its solution
h0 eval run tasks.jsonl --arms vanilla,fitted=myproject --models claude-sonnet-5-5 --out runs/v1
h0 eval report runs/v1
h0 learn propose fittings/myproject --runs runs/v1 --out fittings/myproject-v2
h0 learn gate --prev runs/v1 --cand runs/v2           # keep the edit only if the re-run shows it helps
```
