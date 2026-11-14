# mihari — 見張り

**見張り (mihari)** means "lookout, watch". A canary rollout compares a new version's error rate with the old one's, over time. If you check the dashboard every hour and roll back the first time it looks significant, how often do you roll back a perfectly good release? And what do better stopping rules cost in detection speed? This is a Monte Carlo study, validated against an exact calculation where one exists.

## Run

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
python -m mihari.live --scenario all --rule threshold
python -m mihari.study --trials 1000
```

The demo routes 90% of requests to control and 10% to canary on two local HTTP ports. It runs healthy traffic, a shared outage, and a canary-only regression with delayed telemetry. Try `--rule sequential` to see the other rollback rule.

The [fixed-share study](docs/fixed-exposure.md) compares both rules on held-out traffic. It reports false rollbacks against a 5% target, missed regressions, time to rollback, and canary requests exposed while waiting. The two rules do not necessarily achieve the same false-rollback rate.

The [telemetry stress study](docs/telemetry-stress.md) replays identical traffic with prompt and late outcomes for weaker and shorter faults. Run `python -m mihari.stress` for the comparison and `python -m mihari.live --telemetry-stress` to see it over local HTTP.

The [joint guardrail study](docs/joint-guardrail.md) tests whether slow requests should trigger rollback alongside failures. Run `python -m mihari.guardrail` for the study or `python -m mihari.live --joint-guardrail` for the demo with measured HTTP latency.

The [matched-risk study](docs/matched-risk.md) checks whether the two guardrail rules actually have similar false-rollback rates before comparing fault detection. Run `python -m mihari.matched_risk` for the frozen benchmark.

The [fixed-step simulator](docs/fixed-steps.md) makes separate hold, advance, promote, and rollback decisions at 10%, 25%, and 50% canary traffic. Run `python -m mihari.fixed_steps --timeline all` to see the decisions.

## Original benchmark
Baseline error rate 2%, 500 requests per arm per look, up to 20 looks, 20,000 simulated rollouts per cell, one-sided alpha 0.05. Rules: fixed horizon (test once at the end), naive peeking (z > 1.645 at every look), a constant boundary and an O'Brien-Fleming-style boundary (both calibrated by simulation on separate runs to 5% false rollbacks), an always-valid mixture SPRT, and an SPRT against a known baseline.

### Results ([full report](results/report.md))
| rule | false rollback (no regression) | detect 3% (mean look) | detect 4% | detect 6% |
|---|---|---|---|---|
| fixed horizon | 5.1% | 99.8% (20.0) | 100% (20.0) | 100% (20.0) |
| **naive peeking** | **21.5%** | 99.9% (3.5) | 100% (1.6) | 100% (1.0) |
| constant boundary z>2.39 | 5.1% | 99.2% (6.1) | 100% (2.4) | 100% (1.2) |
| O'Brien-Fleming-style | 5.2% | 99.8% (8.9) | 100% (5.1) | 100% (3.1) |
| mixture SPRT (tau=0.02) | 0.6% | 93.1% (9.1) | 100% (3.4) | 100% (1.5) |
| SPRT vs known baseline (H1=4%) | 0.9% | 85.3% (5.2) | 100% (1.6) | 100% (1.0) |

- Naive peeking inflates false rollbacks from 5% to about 8% at 2 looks, 13% at 5, 18% at 10 and 22% at 20 looks (simulated). The two-look simulation (7.9%) matches the exact Gaussian value (8.0%, from the bivariate normal), checked in a test.
- Calibrated sequential rules keep 5% overall and still stop early on big regressions (4% canary: after 2.4 looks on average instead of 20). The always-valid test is far more conservative (0.6%) and gives up power on small regressions (93% for a 3% canary).

### Caveats
Simulation with i.i.d. requests, equal traffic to both arms, a known stable baseline and a normal approximation at 10 expected errors per look; real traffic is bursty and correlated. No cost model for user impact. The boundary constants are specific to these settings. Existing sequential-testing literature and experimentation platforms cover this ground; this is a small hands-on check, not a new method.

To regenerate the original report, run `python -m mihari.experiment`. It writes `results/report.md`; the test suite reproduces it in a temporary directory instead.
