## Problem

`cmd_eval.summarize` computes `total_usd` only from scored trials. Known spend on
an unscored provider/setup failure can disappear from the reported total.

## Proof it's real

Synthetic rows with a $0.01 successful trial and a $10 unscored trial report
`total_usd=0.01` despite $10.01 of recorded cost. The 18 archived runs audited
here contain no unscored rows: no historical omitted spend was observed.

## Reproduce

Run `python3 reports/adversarial/probe.py --only eval-failure-costs --out /tmp/cost-probe.json`.

## Verify / Invalidate

Try to invalidate first: if report totals include the recorded $10 unscored
cost, close as stale. If the existing column deliberately means scored-only
cost, label it that way and expose all-attempt spend separately. Unknown spend
must remain unknown, and duplicate snapshot archives must not be summed.

## Done when

Reports preserve scored-only efficiency, known total expenditure and unknown
cost counts as distinct readings. Cover unscored attempts and retries.
