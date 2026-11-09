# harness-0 (h0)

A standalone coding-agent harness. It runs a model directly, either through the Anthropic Messages API or any OpenAI-compatible server such as ollama, in place of Claude Code or Codex.

The project asks one question: should the harness be *fitted* to the project it works on? A fitting is a bundle of data:
- a project map
- named checks
- triggers
- sandbox policy
- lessons
- the project's eval suite

The subcommands are `h0 fit` (build a fitting), `h0 eval` (fitted vs bare harness on the same model) and `h0 learn` (propose changes, kept only if an eval says so).

## Hard rules (project-specific)
- **Python 3.11+ standard library only. Never add a dependency.** `pyproject.toml` has `dependencies = []`. "The empty dependency list is a security property, not an accident."
- **Security code** is `h0/sandbox.py`, `h0/policy.py`, `h0/redact.py` and `h0/tools.py`. Every change there needs a regression test in `tests/` (usually `tests/test_security.py`) that *tries to break it*.
- **Unknown is not zero.** A measurement that was not taken is `None`, never `0`. This applies to stats, eval scores, counts and report fields.
- **`evals/PREREG.md` is a pre-registration.** Never rewrite or reorder it; only append amendments. Amendment timestamps are their git commit times.
- **API key** lives in the GNOME keyring (`service=harness-0 account=anthropic-api`). Never print it, write it to a file, or pass it to a child process.
- **Commit messages** are three paragraphs (state, problem, approach), with no AI attribution.
- **`docs/` is an Obsidian vault.** Keep notes as vault Markdown with links between notes. File names may contain spaces (`docs/00 Home.md`).

## Layout
```
bin/h0                   entry script (h0 doctor | fit | eval | learn | --resume <id> ...)
h0/loop.py               agent loop (nudges once on an empty reply)
h0/tools.py              read/edit/write/grep/glob/bash      [security]
h0/sandbox.py            bubblewrap: no network, HOME hidden, /run masked,
                         git hooks read-only, PID namespace  [security]
h0/policy.py             rules, "rule of two" (never sensitive data + network
                         in one session)                     [security]
h0/redact.py             secret redaction of tool output     [security]
h0/secrets.py            keyring access
h0/trace.py              hash-chained JSONL trace; transcript saved beside it, mode 0600
h0/providers/            anthropic.py, openai_compat.py, fake.py (tests use fake)
h0/fitting.py, survey.py, mine.py, learn.py, evals.py, stats.py, context.py, config.py
h0/cmd_*.py              subcommands (eval, fit, host, learn, omarchy)
h0/omarchy.py            Omarchy theme auditor / eval host
fittings/<name>/         fitting.toml, map.md, lessons.jsonl
                         (+ survey.json, sources.json, fit-report.json from `h0 fit`)
evals/PREREG.md          pre-registration (append-only amendments)
evals/<suite>/           tasks.jsonl, heldout.jsonl, mine.log, fixtures, solutions
evals/omarchy/make_tasks.py, build_fixture.sh   build the omarchy suite/fixture
evals/gradient.py
runs/<run>/              eval outputs: trials.jsonl, summary.json, report.md, traces/, run.log
tests/                   test_security.py, test_loop.py, test_omarchy.py, test_selfreview.py
docs/plans/harness-0/00-status.md   plan and gate state
docs/rationale/          harness-principles, security-model, eval-method
docs/results/            experiment write-ups (omarchy.md, ...)
docs/journey/            dated logs
```

## Build / run / test
- No build step.
- Tests (offline, no key needed; bubblewrap is installed; tests create temp repos):
  `python3 -P -m unittest discover -s tests -t .`
  Keep `-P` and `-t .`. Do not use pytest; it is not a dependency.
- Single module: `python3 -P -m unittest tests.test_security -v`
- Smoke: `bin/h0 doctor`

## Traps
- `h0 eval`, `h0 learn` and `h0 fit` call real models: networked, slow and costly. Don't run them from a session. Tests use `providers/fake.py`.
- `runs/` holds recorded results. `trials.jsonl` is evidence, and corrections go through a documented rescore (see `trials.before-rescore.jsonl`). Never hand-edit.
- `heldout.jsonl` is the held-out split. Don't tune fittings against it.
- `fittings/*/survey.json`, `sources.json` and `fit-report.json` are `h0 fit` outputs. `fittings/*/CHANGELOG.md` records `learn` rounds.
- `evals/omarchy/tasks.jsonl` changes together with `make_tasks.py`. Edit the generator and regenerate.
- Sandbox kills by PID namespace, not `pkill -f` patterns.
- Results docs must report missing measurements as missing, not zero.
