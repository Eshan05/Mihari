# Recorded-trace replay

Does the latency guardrail distinguish a canary-only slowdown from whatever both arms experience in a recorded workload? This replay uses six [RCAEval Online Boutique testbed recordings](https://huggingface.co/datasets/phamquiluan/RCAEval/tree/8b6c06a7a15bcecdbf70b611a13b789c5925dc17) ([paper](https://arxiv.org/abs/2412.17015)). The request timings are recorded. The control and canary assignments, the added delay, and the rollback decisions are simulated.

## Run

```bash
python -m pip install -e ".[trace]"
python -m mihari.trace_replay --data-dir /path/outside/repo --fetch --timeline delay_1
```

The first run fetches six `traces.parquet` files (60,959,525 bytes total) and their `inject_time.txt` markers. Drop `--fetch` for an offline rerun. The replay checks byte sizes and SHA-256 hashes against the pinned dataset revision. It does not contact the services in the traces.

## Protocol

- Keep unique parentless `frontendservice` / `frontend` spans that started in the half-open interval from five minutes before to five minutes after the supplied marker. The input audit checks the microsecond start clock against its millisecond counterpart and reports counts before and after filtering.
- In each 30-second send window, assign the lowest stable identity hashes to the pseudo-canary (10%, rounded down). Use the same assignment in A/A and in the paired run that adds 150 ms to each pseudo-canary duration starting at or after the marker.
- Evaluate each send window once, 60 seconds after it closes. A result is visible only if its span has completed by then, including the added delay. A window needs at least four visible completions per arm. Roll back after two consecutive eligible windows where the share exceeding 100 ms is more than 15 percentage points higher in the pseudo-canary. An ineligible window breaks the streak.
- Count pseudo-canary starts through the rollback decision, including starts at the decision tick and requests still in flight. Without a rollback, count all starts in the selected cohort. A modeled detection requires the window that triggers rollback to start at or after the marker.

## Results

All six A/A replays finished without rollback. All six modeled slowdowns rolled back at +120 s, with no ineligible windows before either outcome. The second post-marker send window closes at +60 s and is evaluated at +120 s, the earliest possible decision from two post-marker windows under these settings.

| Checkout testbed case | Selected requests | A/A post-marker sends | Modeled post-marker sends through rollback |
| --- | ---: | ---: | ---: |
| delay_1 | 8,089 | 398 | 158 |
| delay_2 | 7,756 | 377 | 147 |
| delay_3 | 7,913 | 390 | 161 |
| loss_1 | 7,614 | 367 | 148 |
| loss_2 | 7,845 | 386 | 156 |
| loss_3 | 7,886 | 379 | 154 |

The source's checkout delay and loss injections were part of the recorded workload, not faults assigned to either replay arm. Span duration is not necessarily what the client experienced, and recorded completion is not a measured telemetry ingestion time. The replay makes no HTTP status inference. These six related testbed cases and one modeled effect size do not estimate production false-rollback risk or real deployment outcomes.
