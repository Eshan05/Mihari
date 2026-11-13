# Fixed-share canary rollbacks

How much traffic reaches a faulty canary before a rollback rule has enough evidence to act? This study compares a two-window failure-rate threshold with a sequential likelihood ratio. It extends the [original equal-traffic benchmark](../results/report.md) without replacing it.

## Traffic and decisions

- Each rollout sends 12 windows of requests with an exact 90% control and 10% canary split. It simulates a varying baseline, three windows of elevated failures in both versions during the shared outage, and a separate scenario with a sustained 19-percentage-point canary failure increase.
- Each window is examined once, two ticks after it was sent. Outcomes that have not arrived are excluded rather than counted as successes. Nothing is backfilled into an earlier decision. The regression starts at tick 4, and its canary telemetry arrives at the end of the two-tick grace period. Traffic continues during that wait. The study doesn't isolate the effect of delay from the effect of this observation schedule.
- The threshold rule rolls back if the canary-minus-control failure-rate gap exceeds a cutoff in two consecutive windows with at least four observed outcomes per version. The sequential rule uses one preassigned control request per canary request, counts discordant pairs with both outcomes available, and accumulates the Bernoulli log likelihood ratio (alternative 0.75 versus null 0.5 for a discordance favoring control). This is an upper-boundary application of [Wald's sequential likelihood ratio](https://doi.org/10.1214/aoms/1177731118); it does not stop early in favor of the null or claim Wald's nominal error rates.
- Both policies see the same underlying rollouts. Each cutoff is the higher of the two empirical 95th percentiles of the maximum score per rollout under healthy and shared-outage traffic, floored at zero. Crossing is strict, so ties can make a rule more conservative. Calibration uses even seeds; evaluation uses disjoint odd seeds.

## Reading the study

Run `python -m mihari.study --trials 1000` to reproduce the results. No files are written.

| Held-out metric (1,000 rollouts per scenario) | Two-window threshold | Sequential likelihood ratio |
|---|---:|---:|
| Healthy false rollback (95% CI) | 0.3% (0.1%-0.9%) | 4.2% (3.1%-5.6%) |
| Shared-outage false rollback (95% CI) | 3.5% (2.5%-4.8%) | 5.0% (3.8%-6.5%) |
| Detected regressions (95% CI) | 99.1% (98.3%-99.5%) | 99.2% (98.4%-99.6%) |
| Not detected (including rollbacks on pre-onset windows) | 9 (0) | 8 (6) |
| Median detection delay, detections only | 3 ticks | 4 ticks |
| Mean delay, charging misses 9 ticks | 3.92 ticks | 3.85 ticks |
| Mean post-onset canary requests before stop or end | 96.9 | 94.7 |

False rollbacks are counted per rollout, not per window. The report includes Wilson 95% intervals for false rollback and detection rates. A rollback on a pre-regression window doesn't count as detection, even if the decision arrives after fault onset. The median delay covers detections only; the mean delay assigns undetected rollouts the full observation horizon (nine ticks after onset). Exposure counts canary requests sent from onset until rollback or the end of traffic, including sends before a decision on the same tick. Earlier false rollbacks can reduce measured exposure without detecting a regression. Compare these figures alongside the achieved false-rollback rates, not as a ranking at equal risk.

Calibration and evaluation are synthetic. The intervals describe simulation uncertainty in this setup, not performance on real traffic. The matched statistic uses fewer control observations than the rate-gap rule and assumes a meaningful pairing. The experiment does not establish how either policy handles mismatched cohorts, outcome-dependent missing telemetry, or different exposure levels.

## Live demo

```bash
python -m mihari.live --scenario all --rule threshold
python -m mihari.live --scenario all --rule sequential
```

Two HTTP listeners bind separate ephemeral ports on `127.0.0.1`. Failure counts come from actual HTTP statuses; faults, latency values, and telemetry arrival times are scripted. The demo uses fixed illustrative cutoffs, not the calibrated benchmark cutoffs. Healthy traffic and the shared outage finish without rollback. For the canary regression, the first affected outcomes are counted at tick 6. Both rules roll back at tick 7 after 16 post-onset canary requests, and the next request goes only to control. The listeners close when the run exits.
