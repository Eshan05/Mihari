# Should an unhealthy control hold the rollout early?

The joint rollback rule asks whether the canary is worse than control. It can
correctly avoid a rollback during a shared outage, even when neither version
is healthy enough to take more traffic. The fixed-step rollout already
checks control health at each stage checkpoint. This study asks whether
checking it earlier helps enough to justify interrupting more rollouts.

## Comparison

Both policies use the same 10%, 25%, and 50% stages, two-tick telemetry
deadline, joint rollback guardrail and promotion checks. The only change in
the second policy is an early, terminal **hold** on control's absolute
health. If the joint guardrail calls for rollback on the same tick, it wins.

For each metric, divide the request-weighted bad fraction by its error
budget: 4% for failures (96% success target) or 7.5% for slow responses
(92.5% within 100 ms). Hold if the **same** metric burns above 2x in both the
last two and last four closed windows of the current stage. That means
above 8% failures or 15% slow responses, also the caps used in the existing
checkpoint. The early gate requires complete control data for all four
windows; it neither treats missing observations as good nor removes the
existing telemetry check. The paired-window idea comes from
[Google's SLO alerting guidance](https://sre.google/workbook/alerting-on-slos/);
these simulated windows and targets are not production defaults.

## Results

1,000 paired seeds per scenario, starting at 1,160,000. Each pair sees the
same traffic through its first differing decision. Counts below are per
1,000 runs; time includes the decision tick.

| Scenario | Mean ticks to outcome, baseline → gated | Mean canary sends during shared degradation | Promotions | Rollbacks |
| --- | ---: | ---: | ---: | ---: |
| Healthy | 24.78 → 24.78 | — | 736 → 736 | 0 → 0 |
| Sustained shared outage | 10.00 → 6.00 | 800.0 → 600.0 | 0 → 0 | 0 → 0 |
| Shared outage, then recovery | 14.43 → 6.00 | 400.0 → 400.0 | 219 → 0 | 0 → 0 |
| Shared outage starts in last window of the 10% stage | 15.37 → 13.22 | 1,174.0 → 905.5 | 0 → 0 | 0 → 0 |
| Canary-only regression | 4.42 → 4.42 | — | 0 → 0 | 1,000 → 1,000 |

- **Sustained outage:** The gate held four ticks sooner and sent 25% fewer
  requests to the canary during the outage, without attributing the outage
  to that version.
- **Recovery:** The outage lasted four send windows. By the time the fourth
  window closed, new traffic had recovered. The gate stopped 219 rollouts
  that the baseline eventually promoted and saved **no** canary sends
  during the outage.
- **Late onset:** Both policies advanced 537 times past 10%. One late bad
  window was usually insufficient to trip the two-window/four-window gate.
  The benefit came from stopping sooner at 25%, not from preventing the ramp.

A hold here ends the simulated run; there is no retry or recovery policy.
Canary sends count exposure, not prevented errors. Traffic sent to control
may still suffer from a shared outage. The comparison does not model a
combined shared outage and canary regression across an entire trial cohort,
production time windows, or budget consumption over a real SLO period.

## Reproduce

```bash
python -m mihari.slo_gate --timeline all
python -m mihari.slo_gate --trials 1000
```

The timeline shows both decisions on the same scripted inputs. For sustained
degradation it shows the early hold at tick 5, the checkpoint hold at tick 9,
and the control burn rates used by the gate.
