# Rollout impact beyond requests exposed

A request sent to a faulty canary does not necessarily fail or become slow.
This report counts extra outcomes on the same simulated requests used in the
[fixed-share risk confirmation](risk-confirmation.md). It leaves the traffic,
cutoffs, decisions and cohort unchanged. This is additional accounting, not
independent confirmation.

```bash
python -m mihari.impact
python -m mihari.impact --timeline
```

## Count harm on the same requests

For each canary request, the simulator also checks what would have happened at
the control version's thresholds with the same draws and background load.
Count an added failure if the canary fails and that comparison does not.
Count an added slow response the same way. An observed failure or slowdown
that would also occur on control is not incremental. These counterfactual
outcomes are known inside the simulation, not on real traffic.

Count sends through the decision tick, including outcomes still in flight
or late by the deadline. Exclude future sends after a rollback. When there
is no rollback, count the full rollout. The timeline uses combined-fault
seed 950000 and shows what each policy includes. Counterfactual outcomes
never enter the policy's evidence.

## Compare costs without pricing an incident

One added failure costs one unit. An added slow response not already counted
as an added failure costs either 0.25 or 1 unit. The model charges 0, 100 or
1,000 units for a rollback on a no-version-fault workload or on pre-fault
evidence. It reports those stops separately so they are never credited as
fault detections.

Both hypothetical mixes use 90% healthy runs, 5% shared degradation, and 1%
combined faults. Error-heavy puts 3% on error-only and 1% on latency-only;
latency-heavy reverses those two weights. These proportions and cost weights
are assumptions, not observed frequencies or monetary estimates.

## Added outcomes

All columns are means per simulated rollout. Overlap is included in both raw
outcome columns, but counts once in the cost model.

| Fault | Rule | Affected sends | Added failures | Added slows | Both added |
| --- | --- | ---: | ---: | ---: | ---: |
| Error only | Error-only | 120.9 | 14.520 | 0.000 | 0.000 |
| Error only | Joint | 137.3 | 16.497 | 0.000 | 0.000 |
| Latency only | Error-only | 159.0 | 0.000 | 30.189 | 0.000 |
| Latency only | Joint | 109.2 | 0.000 | 20.682 | 0.000 |
| Combined | Error-only | 120.9 | 14.520 | 22.927 | 7.190 |
| Combined | Joint | 104.7 | 12.536 | 19.860 | 6.207 |

Neither no-version-fault case adds harm relative to control. The healthy
case has 198 error-only and 164 joint rollbacks out of 12,000 each. In the
shared degradation case those counts are 610 and 575. For each fault,
13 error-only and 12 joint runs stopped on pre-fault evidence. Those stops
can reduce exposure without detecting the fault.

## Sensitivity results

Differences below are joint minus error-only. Negative means a lower
modeled cost for joint. The command also prints the absolute scores.

| Hypothetical mix | Slow-only weight | Stop weight 0 | Stop weight 100 | Stop weight 1,000 | Break-even stop weight |
| --- | ---: | ---: | ---: | ---: | ---: |
| Error-heavy | 0.25 | +0.0105 | -0.2608 | -2.7020 | 3.86 |
| Error-heavy | 1 | -0.0765 | -0.3477 | -2.7890 | None |
| Latency-heavy | 0.25 | -0.0766 | -0.3478 | -2.7891 | None |
| Latency-heavy | 1 | -0.3061 | -0.5774 | -3.0186 | None |

In the error-heavy mix with a 0.25 slow-only weight, error-only scores
lower when an unnecessary stop is worth less than about 3.86 added
failures. Joint scores lower above that point. Under the other three
sets of assumptions, joint has the lower score at every nonnegative stop
weight. This is a sensitivity result on the frozen simulation, not a
recommendation for production.
