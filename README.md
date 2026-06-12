# swarm-runners

GitHub Actions worker for the contractor-app v2 swarm protocol.

Runs three cron-scheduled jobs (DDG search, Google search, web fetch) on GitHub's
hosted runners. Each invocation claims jobs from the central queue, executes them
from GitHub's IP pool (a distinct egress from the VPS fleet and Pi residential),
and submits results back.

Public repo = unlimited free minutes on Actions.

## Required secrets

- `AGENT_KEY` — the v2 protocol agent key
