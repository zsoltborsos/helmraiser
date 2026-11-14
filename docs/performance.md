# Expected performance

Measurements from one real working day on a laptop, to set expectations for unattended runs.
They describe one machine and one mix of tasks, not a benchmark.

## Setup

| | |
|---|---|
| CPU | Intel Core i5-1135G7, 4 cores / 8 threads |
| RAM | 31 GiB, 62 GiB swap |
| OS | Linux 7.2, Docker 29.9 |
| Isolation | `process` (srt over bubblewrap); project services in Docker |
| Concurrency | `max_parallel_runs` 4; the large project `max_runs_per_project` 3, the other 1 |
| Models | Implementer and reviewer through pi on subscription models |
| Window | 2026-11-13 16:14 to 2026-11-14 12:49 UTC (20.6 h), sampled every 30 s |

The laptop was also in normal interactive use (several agent sessions, editors, a browser), so
the load figures include that background.

Two projects ran:

- **A large monorepo** (Python backend, TypeScript mobile app, Postgres and Redis as project
  services): a batch of security and data-protection fixes, 24 runs.
- **Helmraiser itself**: CLI and runner changes, 8 runs.

## Host load by concurrent runs

| Runs | Share of time | Load (1 min) median / p95 | CPU busy median / p95 | Free memory median / min | Service containers median / max |
|---|---|---|---|---|---|
| 0 | 51% | 0.9 / 6.5 | 6% / 39% | 23.5 / 19.0 GB | 0 / 0 MB |
| 1 | 32% | 1.9 / 8.8 | 15% / 46% | 23.1 / 18.9 GB | 0 / 320 MB |
| 2 | 8% | 2.2 / 9.4 | 21% / 66% | 22.8 / 17.4 GB | 176 / 529 MB |
| 3 | 8% | 4.0 / 10.8 | 33% / 72% | 22.3 / 15.5 GB | 358 / 729 MB |
| 4 | 1% | 5.6 / 16.0 | 53% / 75% | 21.5 / 19.0 GB | 428 / 670 MB |

- A run spends most of its time waiting on the model, so CPU use is low between test runs;
  the p95 figures are the moments when verify, the full test suite or a build runs.
- Memory was never the limit: free memory stayed above 15 GB and swap use did not grow.
- Four concurrent runs fit on this machine alongside interactive use. The load peaks
  coincide with test suites running at the same time; heavier suites or native builds
  would lower that ceiling.

## Per-run figures

Active minutes exclude queue and approval waits. Cost is pi's estimate of the equivalent API
price; these runs used subscriptions, so it was not billed per run (see
[Model selection](model-selection.md)).

| | Runs | Active minutes median / p90 / max | Iterations median / max | Cost median / p90 | Landed | Reworked once |
|---|---|---|---|---|---|---|
| Large monorepo | 24 | 15 / 35 / 92 | 1 / 8 | $1.42 / $5.49 | 24 of 24 | 4 |
| Helmraiser | 8 | 32 / 150 / 150 | 1 / 3 | $1.06 / $6.15 | 8 of 8 | 2 |
| All | 32 | 19 / 48 / 150 | 1 / 8 | $1.38 / $5.49 | 32 of 32 | 6 |

- Every run started in the window landed. Six needed one owner rework round after review;
  the 150-minute run includes its rework.
- Most runs passed in one iteration. Long tails come from large tasks and reworks, not from
  the host.
- Throughput was 32 landed runs in 20.6 hours, with at least one run active about half the
  time. Owner review and approval, not the machine, set the pace.

## Reproducing

A sampler appending `load1`, CPU busy, available memory, swap, service-container memory and
runs per project every 30 s, joined afterwards with the ledger's runs (active minutes,
iterations, cost, status), is enough to repeat this on other hardware.
