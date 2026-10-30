# Synthetic CAS concurrency canary

This repository contains a small, standalone concurrency experiment with inert synthetic state only.

The experiment asks two independent GitHub-hosted jobs to build sibling successor commits from one immutable seed and each make exactly one ordinary, non-forced push to the protected branch cas-synthetic-001. A separate read-only job reconciles the branch tip and both writer outcomes.

Safety properties:

- No external services, secrets, deployments, dispatchers, or operational data.
- Workflow token defaults to contents:read. Only writer jobs receive contents:write; the reconciler remains read-only.
- The token is the GitHub Actions repository token and is limited to this repository.
- main and cas-synthetic-001 are protected against force-push and deletion, with no ruleset bypass actors.
- The workflow has only workflow_dispatch; no push, pull_request, schedule, or workflow_run trigger.
- The workflow uses no third-party actions, dependencies, artifacts, caches, or external package downloads.
- State is inert and permanently records authority=NONE, execution_authorized=false, dispatch_status=NONE, outbox_write=false, and continuation_decision=null.

The test is a synthetic test of GitHub's normal branch update behavior, not an explicit compare-and-swap API primitive. One accepted ref update and one rejected non-fast-forward sibling are required. If the API job records do not prove independent runners and overlapping push intervals, the run is not a concurrency PASS.

