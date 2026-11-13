# Should slow requests trigger rollback?

An error-only rule can miss a canary that returns HTTP 200 but takes much longer than control. This study adds a second trigger and measures the tradeoff under one overall false-rollback target. It leaves the [original benchmark](../results/report.md), [fixed-share study](fixed-exposure.md), and [telemetry stress study](telemetry-stress.md) unchanged.

```bash
python -m mihari.guardrail --trials 500
python -m mihari.live --joint-guardrail
```

## Rules and calibration

Each window measures the fraction of requests that failed and the fraction that took more than 100 ms. Both fractions use all observed requests, regardless of HTTP status. For each metric, take the smaller canary-minus-control gap from two consecutive eligible windows. The error-only rule uses the error score. The joint rule uses the larger of the two scores, and reports which metric or metrics crossed at rollback. An error gap in one window and a latency gap in the next do not make a pair of crossings.

Both rules see the same 90/10 traffic and observed outcomes. Each window closes two ticks after send. An outcome that has not arrived is excluded from both metrics. A window with fewer than four observed requests per version holds and breaks the run of consecutive windows. No result is a promotion decision.

Calibration takes the empirical 95th percentile of the largest score in each rollout, then uses the higher cutoff from healthy and shared-degradation calibration. For the joint rule this is a cutoff on the maximum over **both metrics and all looks**, not 5% for each metric. It uses one numerical gap cutoff for both metrics, measured in percentage points; it does not account for one signal being noisier than the other. Odd-seeded holdout trials are disjoint from even-seeded calibration. The realized rates are empirical, not a production statistical guarantee or proof of equal risk.

## Traffic and scoring

The shared-degradation null increases both metrics for both versions. The canary faults add 12 percentage points to failures, 19 points to slow requests, or both from window 4 onward. The simulation sometimes shares the random draw between the two outcomes, producing correlated errors and latency without making either a proxy for the other. Some outcomes arrive after the deadline independently of their outcome.

The report includes false rollback rates with Wilson intervals for both nulls, and detections, misses, trigger counts, delay, and affected-window sends for each fault. A detection is rollback on a window at or after fault onset, not proof of causal attribution. Early false rollbacks can reduce measured exposure without detecting the injected fault. A missed detection is charged the full nine-tick observation horizon in mean-delay summaries.

## Held-out results

| 500 trials per scenario | Error-only rule | Joint rule |
|---|---:|---:|
| Healthy false rollback | 1.2% | 1.0% |
| Shared-degradation false rollback | 4.4% | 3.6% |
| Error-only fault detected | 78.8% | 55.8% |
| Latency-only fault detected | 0.8% | 92.6% |
| Combined fault detected | 78.8% | 95.6% |

The joint rule picked up slow-only and combined faults that the error-only rule missed, but its larger gap cutoff also missed more error-only faults. The few rollbacks by the error-only rule in the latency-only case were not evidence that it detected latency: the generated error outcomes are identical to the healthy traces. The held-out false-rollback rates differ, so the table is a tradeoff under a common target, not a comparison at equal risk.

## Local HTTP demo

Two loopback listeners serve control and canary on different ports. The client uses a monotonic clock around each HTTP request; the server deliberately delays some requests. The latency-only canary returns HTTP 200 throughout. The error-only run has no intentional delay, the shared degradation affects both versions, and the timeout run holds when only three of four canary outcomes arrived by the decision deadline. The delay injection is real; the telemetry deadline itself uses replay ticks. The demo uses an illustrative gap cutoff, not the calibrated benchmark cutoffs.
