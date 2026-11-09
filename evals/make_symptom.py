"""Write the symptom variant of a mined suite: the same tasks, with the failing tests not named.

A mined task's prompt says which test file describes the change. That hands the agent the
investigation (eval-audit: "symptom, not investigation"), and on our first mined suite every
model passed every task. The variant keeps the commit's own description of the change and says only that
some tests fail without it; the agent has to find them. Checks and workspace are unchanged.

    python3 -P evals/make_symptom.py evals/<project>/tasks.jsonl > evals/<project>/tasks-symptom.jsonl
"""

import json
import sys

SAYS = ("\n\nSome of the project's tests fail because this change is missing. Make the change. "
        "Do not edit any test file.")

for line in open(sys.argv[1]):
    if not line.strip():
        continue
    t = json.loads(line)
    t["id"] = t["id"] + "-s"
    t["prompt"] = t["prompt"].split("\n\nThe unit tests in ", 1)[0].strip() + SAYS
    t["variant"] = "symptom"
    print(json.dumps(t))
