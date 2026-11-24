# Regional triggers adjusted for sample precision

The [raw-gap study](regional-risk.md) could not match false-rollback risk
across balanced and skewed traffic. The joint rule used the same cutoff
whether region A supplied 25 canary requests per window or 70. This
follow-up tests whether weighting the observed gaps by their precision
makes an equal-risk comparison possible.

```bash
python -m mihari.regional_precision
python -m mihari.regional_precision --timeline
```

## What changed

Each regional canary-minus-control gap is divided by its estimated null
standard deviation. The null variance is
`p * (1 - p) * (1/n_canary + 1/n_control)`, where the sample sizes count
on-time observations. The pooled slow fraction `p` adds half a slow and
half a fast observation to keep the variance positive at either extreme.
The overall rule still targets a 25/75 weighted gap. Its variance sums the
regional variances with *squared* weights, assuming independent request
draws conditional on the simulated window's conditions.

The workloads, routing, deadlines, sample minimum, and two-window
persistence are unchanged. Either arm in either region falling below four
on-time observations resets every branch. The joint rule can stop on the
overall score, A, or B, but two adjacent crossings must be on the same
branch. These are scores for calibration, not p-values.

## How risk was compared

- Fit one strict cutoff per whole rule across all looks and branches.
  Use 2,000 paired calibration runs per no-fault case (1050000-1051999).
  Keep each rule at or below 5% empirical false rollback in each case.
  The overall rule gets the lowest eligible cutoff; the joint cutoff
  minimizes its largest risk gap from that reference. The previous
  tie-breakers favor a higher maximum null rate, then a stricter cutoff.
- Freeze the cutoffs (1.6303453360887723 overall, 1.8834884641332144 joint).
  Evaluate on 8,000 new pairs per no-fault case (1060000-1067999) and
  3,000 new pairs per fault (1080000-1082999).
- Call risk comparable only if every simultaneous 95% paired interval for
  joint minus overall fits strictly inside +/-1.5 percentage points.
  Replay the old raw rules at their frozen cutoffs (68419/1062600 and
  465/3268) on those same requests as descriptive controls.

## Held-out false rollbacks

| No-fault case | Precision overall | Precision joint | Joint minus overall (simultaneous 95% CI) | Raw overall | Raw joint |
| --- | ---: | ---: | ---: | ---: | ---: |
| Balanced outage | 269 / 8,000 | 320 / 8,000 | +0.64 pp (+0.03 to +1.41) | 90 / 8,000 | 387 / 8,000 |
| Skewed outage | 336 / 8,000 | 346 / 8,000 | +0.12 pp (-0.45 to +0.74) | 347 / 8,000 | 35 / 8,000 |
| Skewed outage, A control late | 202 / 8,000 | 203 / 8,000 | +0.01 pp (-0.45 to +0.48) | 172 / 8,000 | 6 / 8,000 |

**All three precision intervals fit the margin.** The balanced case is
closest to the boundary; its interval excludes zero, so comparable does
not mean identical. The raw controls still show the earlier reversal on
the same traffic. The missing-control case forced 31,560 holds in each
precision rule; any rollback in that case used other windows.

## Held-out faults

| Fault | Rule | Detected / 3,000 | Missed | Detected at/after recovery | Mean delay (ticks) | Mean affected sends |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Global | Precision overall | 2,952 | 48 | 265 | 3.17 | 389.6 |
| Global | Precision joint | 2,954 | 46 | 117 | 3.07 | 388.3 |
| Global | Raw overall | 2,954 | 46 | 236 | 3.16 | 390.3 |
| Global | Raw joint | 2,849 | 151 | 663 | 3.61 | 398.9 |
| Region A | Precision overall | 1,041 | 1,959 | 540 | 7.17 | 275.1 |
| Region A | Precision joint | 2,933 | 67 | 219 | 3.20 | 273.5 |
| Region A | Raw overall | 971 | 2,029 | 502 | 7.29 | 275.4 |
| Region A | Raw joint | 2,862 | 138 | 582 | 3.56 | 279.6 |

The precision joint rule caught 1,896 regional faults the precision
overall rule missed; four went the other way. Global detection was almost
unchanged. The local detection gain translated to only 1.6 fewer affected
canary sends on average: the fault lasted four send windows, and the
two-window persistence plus telemetry delay left little time to cut exposure.

Detection is based on the triggering send window; rollback itself can
arrive at or after recovery. Misses (including unrelated stops) cost nine
ticks in the delay metric. Exposure counts affected-region canary sends
through the decision, whether or not their outcomes arrived on time. It is
not a count of extra slow responses. These results establish comparability
for the three modeled nulls and the declared margin, not a 5% held-out
risk guarantee, production risk, or anytime validity.
