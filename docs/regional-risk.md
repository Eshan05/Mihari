# Regional triggers under one false-rollback budget

The [regional guardrail study](regional-guardrail.md) showed that comparing
like-for-like traffic avoids some false alarms, but averaging the regional
gaps can hide a slowdown confined to one region. This study compares the
adjusted overall rule with a rule that can also act on a regional gap.

```bash
python -m mihari.regional_risk
python -m mihari.regional_risk --timeline
```

## Protocol

Both rules see the same requests, routing, two-tick deadlines, and regional
sample minimum. The adjusted overall gap weights regions A and B at 25% and
75%. The joint rule checks the overall gap, A, and B. Each branch's score is
the smaller of its gaps from two adjacent eligible windows. A high gap in A
followed by one in B is not persistent evidence. If either arm in either
region misses the sample minimum, both rules hold and reset all branches.

Calibration uses 2,000 seed pairs per no-version-fault scenario (1010000
through 1011999). The rollout score is the highest branch score at any
look. Fit the lowest overall-only cutoff that keeps empirical risk at most
5% in each scenario. Choose a joint cutoff under the same scenario caps
that minimizes the largest gap from the overall-only rates. Ties favor the
larger worst-null rate, then the stricter cutoff. The joint rule has one
cutoff for the full set of branches and looks, not a separate budget for
each region.

Freeze both cutoffs before evaluation. Holdout uses 8,000 pairs in each null
(seeds 1020000 through 1027999) and 3,000 in each fault (1040000 through
1042999). Call risk comparable only if all three simultaneous 95% paired
intervals for joint minus overall-only fit strictly inside +/-1.5 percentage
points. An inconclusive match does not establish that rates differ; a match
does not guarantee that either risk stays below 5%.

## False-rollback risk

The fitted cutoffs were about 6.439 percentage points for overall-only and
14.229 for joint, with exact thresholds retained by the command. The 5%
calibration cap bound the overall rule on the skewed outage and the joint
rule on the balanced outage.

| Held-out null | Overall-only | Joint | Joint minus overall (simultaneous 95% CI) |
| --- | ---: | ---: | ---: |
| Balanced outage | 98 / 8,000 | 364 / 8,000 | +3.33 pp (+2.45 to +4.27) |
| Skewed outage | 333 / 8,000 | 36 / 8,000 | -3.71 pp (-4.56 to -2.89) |
| Skewed outage, A control late | 170 / 8,000 | 6 / 8,000 | -2.05 pp (-2.53 to -1.53) |

The fitted rules **did not match** within 1.5 points. The joint rule rolled
back more often in the balanced outage, but less often in the other two
cases. In the balanced outage, A accounts for all 364 joint rollbacks:
the canary has only 25 of its 100 requests in A before telemetry loss.
For the missing-control case, both rules held at every affected deadline
they reached; rollbacks in that scenario used other windows.

## Fault tradeoffs

| Fault | Rule | Detected / 3,000 | Missed | Detected at or after recovery | Mean delay (ticks) | Mean affected sends |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Global | Overall-only | 2,958 | 42 | 242 | 3.14 | 391.3 |
| Global | Joint | 2,856 | 144 | 689 | 3.60 | 399.1 |
| Region A | Overall-only | 993 | 2,007 | 502 | 7.24 | 276.2 |
| Region A | Joint | 2,867 | 133 | 560 | 3.54 | 280.0 |

The joint rule caught 1,904 regional faults the overall-only rule missed;
30 went the other way. For the global fault, 133 were caught only by the
overall rule and 31 only by joint. These are tradeoffs at the fitted
operating points, not an equal-risk ranking.

The triggering send window must be inside the fault interval. A decision
can arrive after recovery. Misses, including unrelated rollbacks, are
charged nine ticks. Exposure counts affected-region canary sends through
the decision, not observed slow responses. Faster local detection did
not reduce that count here: the fault lasted four send windows and most
requests were already sent before rollback. Twenty-eight overall-only
runs per fault rolled back on pre-fault evidence, which can also lower
that rule's exposure without detecting the regression.

The conclusions are limited to the frozen workloads and one cutoff per
policy. There is no new operational or anytime-valid risk guarantee.
