# Anytime-valid regression adjustment for canary rollbacks

Can a request attribute known before routing make a slowdown easier to
detect? Both rules use the same sequential confidence sequence on the same
randomized rollouts. Only one rule adjusts for the attribute.

```bash
python -m mihari.lindon_adjustment
python -m mihari.lindon_adjustment --timeline
```

## Rule

Use the scalar heteroskedasticity-robust asymptotic t sequence from
[Lindon et al., Theorem 5.4 and equations (22)-(25)](https://arxiv.org/abs/2210.08589v5).
Fit OLS with an intercept and a canary indicator; add the pre-routing
attribute in the adjusted fit. Both use the treatment coefficient and an
HC1 sandwich standard error. This is the one-contrast additive version;
the simulated treatment effect is constant.

At each completed look both rules use the same two-sided 95% sequence
construction with `alpha = 0.05` and `g = 1000`. Roll back only when the
lower endpoint for canary-minus-control latency is strictly positive. A
negative effect does not trigger, and a fit that cannot produce a finite
bound holds. No boundary is fit to these runs.

## Workload

- 20 looks of 200 independent, fully observed requests. The canary gets
  each request with fixed probability 10%, independently of its potential
  latency and attributes.
- A fair binary request class `Z` determines the control latency:
  `180 + 120*Z + (12 + 24*Z)*U` ms with independent `U` uniform from -1 to 1.
  Canary adds no delay in the null, or 12 ms from the first request in the
  fault. The simulator watches for a persistent, not transient, slowdown.
- The informative attribute is `Z`; the irrelevant attribute is an
  independent fair binary value. The assignment and latencies stay the same
  between attribute cases for a given seed. This deliberately gives the
  informative case a strong predictor.
- Run 2,000 pairs per null (seeds 1090000-1091999) and fault (1100000-1101999).
  Both simultaneous 95% paired intervals must fit strictly inside
  +/-1.5 percentage points to call false-rollback risk comparable.

## False rollbacks

| Attribute | Unadjusted | Adjusted | Adjusted minus unadjusted (simultaneous 95% CI) |
| --- | ---: | ---: | ---: |
| Informative | 7 / 2,000 | 4 / 2,000 | -0.15 pp (-0.95 to +0.58) |
| Irrelevant | 7 / 2,000 | 7 / 2,000 | 0.00 pp (-0.25 to +0.25) |

Both intervals fit the predeclared margin. These are one-directional stops
over 20 looks, not estimates of two-sided rejection over an unbounded run.

## Persistent slowdown

| Attribute | Rule | Detected / 2,000 | Mean look to stop | Mean canary sends |
| --- | --- | ---: | ---: | ---: |
| Informative | Unadjusted | 1,552 | 14.19 | 280.1 |
| Informative | Adjusted | 2,000 | 2.63 | 52.6 |
| Irrelevant | Unadjusted | 1,552 | 14.19 | 280.1 |
| Irrelevant | Adjusted | 1,548 | 14.21 | 280.5 |

With the informative attribute, adjustment caught all 448 faults missed by
the unadjusted rule. With the irrelevant attribute, four runs stopped only
with adjustment and eight stopped only without it. Misses count as look 21
for the time metric. Canary sends include the decision look, or all 20 looks
if the rule never stops. That counts exposure, not measured user harm.

The fixed walkthrough (seed 1100000) stops at look 2 with adjustment and
look 16 without it. At look 2 both see the same 400 requests, but only the
adjusted lower bound is above zero.

This study uses the paper's asymptotic method under a controlled independent
assignment model. It does not establish exact finite-sample validity, or
extend the result to delayed, clustered, or drifting production traffic.
