# State-space allocation for canary experiments

Can a state-space model estimate the average latency effect with less canary
traffic? This experiment separates the routing decision from the estimator.
It does not replace [Mihari's adaptive stage policy](adaptive-steps.md):
that policy decides when to expand; these rules allocate a fixed-horizon
experiment for precision.

```bash
python -m mihari.shao_allocation
python -m mihari.shao_allocation --timeline
```

## Protocol

There are 32 periods of 60 requests and 1,500 paired runs per regime
(seeds 1120000-1121499). Each policy sees the same latent means, potential
outcomes, and random request order. It observes only assigned outcomes.
The target is the unweighted average canary-minus-control mean over all
32 periods. Centered latency and effect estimates are in milliseconds.

| Model parameter | Control | Canary |
| --- | ---: | ---: |
| AR(1) coefficient | 0.90 | 0.65 |
| Innovation variance | 100 | 4 |
| Within-period observation SD | 30 | 35 |
| Initial mean | 0 | 18 |
| Initial variance | 144 | 64 |

In the matched regime both arms use this model. In the shift regime a
sustained +25 ms canary level change starts at period 17, but the assumed
model is unchanged. The measurement noise, initial state draw, and
randomized request order are reused when applying the shift.

All rules start at 15 of 60 canary requests for four periods. After that,
the fixed policy stays at 15; both adaptive policies minimize their own
variance objective over counts 6 to 30, with ties going to the lower count.
Both estimate arm variance by pooling within-period residual sums of
squares and degrees of freedom from completed periods. The current mean
gap is not part of the allocation rule.

[Shao, Wei, and Wang](https://proceedings.mlr.press/v300/shao26a.html)
give the plain period average (11), filter and smoother (12)-(21), and
allocation objectives (24)-(28) in the
[paper](https://openreview.net/pdf/8d3ef382bd8102e47e956922528327d2d21d9fcf.pdf).
Classical Neyman minimizes the sum of arm observation variances divided by
counts. The state-space rule adds `(1-beta)^2/q` to each arm's observation
precision before inverting. This is the paper's steady-state criterion
adapted to bounded integer counts, not a rollback or bandit rule.

At the end, apply both plain averaging and Kalman smoothing to each
policy's observed period means. The smoother knows the frozen priors and
transition parameters. It estimates observation variance from its own
outcomes, divided by the realized count for each arm and period. The
backward pass is retrospective and never influences earlier assignments.

## Estimation error

Lower RMSE is better. Bias is estimate minus latent truth.

| Regime | Routing | Plain bias (ms) | Plain RMSE (ms) | Smooth bias (ms) | Smooth RMSE (ms) |
| --- | --- | ---: | ---: | ---: | ---: |
| Matched | Fixed 25% | +0.038 | 1.787 | +0.010 | 1.177 |
| Matched | Neyman | -0.020 | 1.540 | -0.006 | 1.248 |
| Matched | State-space | +0.036 | 1.777 | +0.009 | 1.173 |
| Level shift | Fixed 25% | +0.038 | 1.787 | -9.107 | 9.184 |
| Level shift | Neyman | -0.020 | 1.540 | -7.199 | 7.308 |
| Level shift | State-space | +0.036 | 1.777 | -9.105 | 9.183 |

The paired difference in smoother MSE (state-space minus Neyman) was
**-0.181 ms^2** in the matched regime (simultaneous 95% interval -0.264 to
-0.098) and **+30.918 ms^2** after the shift (+30.283 to +31.552). These
are paired t intervals across simulated seeds, adjusted for two contrasts.

Most of the gain under the matched model came from smoothing, not adaptive
routing: the state-space and fixed-share smoothers were nearly tied
descriptively. After the unmodeled shift, all three smoothers substantially
underestimated the mean gap. Plain averaging kept the same estimation
error in both regimes: the additive shift changed the true gap and the
observed canary means equally, without changing within-period variance.

## Exposure

| Routing | Mean canary sends per run | Matched exposure (request-ms) | Shift exposure (request-ms) |
| --- | ---: | ---: | ---: |
| Fixed 25% | 480.0 | 4,614.8 | 8,778.6 |
| Neyman | 899.9 | 8,541.2 | 16,869.0 |
| State-space | 481.5 | 4,616.6 | 8,785.2 |

Exposure sums the canary count times the positive latent mean gap in each
period. It is modeled latency exposure, not measured customer harm. The
Neyman rule hit the cap in 41,845 of 42,000 post-burn periods per regime;
state-space hit neither bound, and no adaptive fallback occurred. The
comparison shares bounds, not equal exposure.

In the pinned shift trace, state-space assigns 14 canary requests both
before and after the shift, even though the observed gap jumps. Its final
smoothed estimate is -1.54 ms versus +8.83 ms in simulator truth. The
trace shows why a variance rule is not a substitute for incident detection.
No interval coverage, promotion behavior, or production safety is tested.
