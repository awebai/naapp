#!/usr/bin/env python3
"""The outcome of a CI run, for its single required check.

Reads CI_NEEDS, the results of the jobs the check waits for (GitHub's
toJSON(needs)), and CI_PACKAGES, the packages the run selected (a JSON list).
Exits non-zero, naming each problem, unless the jobs that always run
succeeded, every job of a selected package succeeded, and no job failed or was
cancelled. A job this file does not know, or a known job the check does not
wait for, is a problem too, so a new job cannot pass unnoticed.
"""
from __future__ import annotations

import json
import os

# The jobs every run has.
ALWAYS = ('plan', 'scripts')

# Each package job, by its job id, with the package it tests.
JOBS = {
    'naapp-lib': 'naapp-lib',
    'folio': 'naapp/folio',
    'folio-e2e': 'naapp/folio',
    'folio-image': 'naapp/folio',
    'library': 'naapp/library',
    'library-e2e': 'naapp/library',
    'library-image': 'naapp/library',
    'logs': 'naapp/logs',
    'secrets': 'naapp/secrets',
    'secrets-e2e': 'naapp/secrets',
    'secrets-runner': 'naapp/secrets/runner',
    'secrets-values': 'naapp/secrets/values',
}


def problems(needs: dict[str, dict[str, str]], packages: list[str]) -> list[str]:
    found = []
    expected = {*ALWAYS, *JOBS}
    for job in sorted(set(needs) - expected):
        found.append(f'{job}: not a job this check knows')
    for job in sorted(expected - set(needs)):
        found.append(f'{job}: the check does not wait for it')
    for job, outcome in sorted(needs.items()):
        result = outcome['result']
        if result in ('failure', 'cancelled'):
            found.append(f'{job}: {result}')
        elif result != 'success' and (job in ALWAYS or JOBS.get(job) in packages):
            found.append(f'{job}: {result}, but it had to run')
    return found


def main() -> None:
    found = problems(json.loads(os.environ['CI_NEEDS']), json.loads(os.environ['CI_PACKAGES']))
    for problem in found:
        print(problem)
    if found:
        raise SystemExit(1)
    print('every selected job succeeded')


if __name__ == '__main__':
    main()
