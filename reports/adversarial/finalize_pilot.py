#!/usr/bin/env python3
"""Describe a stopped local pilot without turning missing outcomes into failures.

Run only after the pilot process has exited. This does not score partial work,
resume inference, rewrite trial records or repair an incomplete trace.
"""
import argparse
import hashlib
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--reason', required=True, choices=['completed', 'execution-time-limit'])
    args = parser.parse_args()
    prereg = json.loads((args.directory/'preregistration.json').read_text())
    trialfile = args.directory/'trials.jsonl'
    rows = [json.loads(x) for x in trialfile.read_text().splitlines()] if trialfile.exists() else []
    completed = {(r['task'],r['arm']):r for r in rows}
    if len(completed) != len(rows):
        raise ValueError('duplicate completed trial identities')
    traces = list(args.directory.glob('[0-9][0-9]-*.jsonl'))
    cases=[]
    for task in prereg['tasks']:
        for arm in prereg['arms']:
            key=(task['id'],arm)
            row=completed.get(key)
            trace=next((p for p in traces if p.name.endswith('-'+task['id']+'-'+arm+'.jsonl')),None)
            status = ('passed' if row['passed'] is True else 'failed' if row['passed'] is False else 'unscored') if row else ('interrupted-unscored' if trace else 'not-started')
            cases.append({'task':task['id'],'arm':arm,'status':status,
                          'passed': row.get('passed') if row else None,
                          'wall_s':row.get('wall_s') if row else None,
                          'trace':trace.name if trace else None})
    if args.reason == 'completed' and len(rows) != len(cases):
        raise ValueError('cannot call an incomplete pilot completed')
    result={'schema':'h0-pilot-disposition-v1','reason':args.reason,
            'registered_cases':len(cases),'completed_records':len(rows),'cases':cases,
            'preregistration_sha256':hashlib.sha256((args.directory/'preregistration.json').read_bytes()).hexdigest(),
            'paid_model_calls':0,
            'limits':'Partial work is unscored; absent cases are not started. Shared local-server load/cache was uncontrolled. This pilot cannot establish a general success or latency advantage.'}
    (args.directory/'disposition.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'reason':args.reason,'completed':len(rows),'registered':len(cases),
        'statuses':{s:sum(c['status']==s for c in cases) for s in sorted({c['status'] for c in cases})}},indent=2))


if __name__ == '__main__':
    main()
