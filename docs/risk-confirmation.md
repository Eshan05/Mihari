# Confirming comparable false-rollback risk

The [first held-out comparison](matched-risk.md) did not establish that the
error-only and joint error/latency rules roll back healthy versions at
comparable rates. The risk differences were small, but the intervals were too
wide to fit the predeclared margin. This second study keeps both rules and their
cutoffs fixed and runs a fresh confirmation.

```bash
python -m mihari.risk_confirmation
```

This is a finite CPU-only simulation. It takes several minutes, writes no
files, and does not contact services.

## Protocol

- Reproduce the original 2,000-trial calibration per no-regression scenario and
  check both full-precision cutoffs and calibration counts. Do not use the
  previous holdout in the new estimate.
- Run 12,000 fresh pairs per no-regression scenario (seeds 900000 through
  911999), and 3,000 pairs per fault scenario (950000 through 952999). Each pair
  evaluates both rules on the same 90/10 traffic and delayed telemetry.
- Form the same simultaneous 95% intervals for joint minus error-only
  false-rollback risk. Both intervals must be strictly inside +/-1.5 percentage
  points. Comparability does not imply that either risk is under 5%.
- Count detection only when the triggering window is at or after fault onset.
  Pre-fault rollbacks are misses. Charge every miss the full nine-tick horizon
  in mean delay, and count canary sends in affected windows through the
  decision, not just the observed responses.

## False-rollback risk

| Scenario | Error-only rollbacks | Joint rollbacks | Joint minus error-only (simultaneous 95% CI) |
| --- | ---: | ---: | ---: |
| Healthy | 198 / 12,000 | 164 / 12,000 | -0.28 pp (-0.73 to +0.08) |
| Shared degradation | 610 / 12,000 | 575 / 12,000 | -0.29 pp (-0.94 to +0.31) |

Both intervals fit inside the +/-1.5-point margin, so risk was comparable in
these two modeled scenarios. This does not establish that either rule
meets a 5% held-out cap. Under shared degradation, the observed rates were
5.1% for error-only and 4.8% for joint.

## Faults at comparable modeled risk

![Faults detected by the error-only and joint guardrails at comparable modeled false-rollback risk](charts/fault-detection.svg)

| Fault | Rule | Detected / 3,000 | Missed (pre-fault stops) | Mean delay (ticks) | Mean affected canary sends |
| --- | --- | ---: | ---: | ---: | ---: |
| Error only | Error-only | 2,485 | 515 (13) | 5.57 | 120.9 |
| Error only | Joint | 1,746 | 1,254 (12) | 6.91 | 137.3 |
| Latency only | Error-only | 28 | 2,972 (13) | 8.97 | 159.0 |
| Latency only | Joint | 2,791 | 209 (12) | 4.73 | 109.2 |
| Combined | Error-only | 2,485 | 515 (13) | 5.57 | 120.9 |
| Combined | Joint | 2,875 | 125 (12) | 4.42 | 104.7 |

The joint rule caught 2,763 latency-only faults that error-only missed; the
reverse never happened in that scenario. For error-only faults, the reverse
was common: 748 were caught only by the error-only rule and nine only by the
joint rule. On combined faults, 450 were caught only by the joint rule and 60
only by error-only.

The joint rule reduced exposure on the latency and combined faults but
increased it on error-only faults. That is a tradeoff, not a universal winner.
These results cover the frozen 90/10 simulator, modeled workloads and fault
sizes. They do not compare staged rollouts or establish production risk.
