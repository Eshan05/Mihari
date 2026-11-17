# Regional traffic mix and false rollbacks

If the canary gets more requests from a region having an outage, its aggregate
slow-request rate can be higher even when both versions behave identically
within each region. This simulation compares a pooled guardrail with one that
compares within regions first.

```bash
python -m mihari.regional_guardrail
python -m mihari.regional_guardrail --timeline
```

## Protocol

Each simulated window sends 100 of 1,000 requests to canary. Region A receives
250 of the 1,000. In balanced routing, it supplies 25% of each arm; in skewed
routing, it supplies 70% of canary and 20% of control. The pooled rule compares
the observed slow-request rates across all traffic. The adjusted rule weights
the within-region gaps by the fixed offered mix: 25% A and 75% B.

A request is slow above 100 ms. The rules share a cutoff: two consecutive
eligible windows more than 15 percentage points above control. Each window is
evaluated two ticks after send, using only outcomes that arrived by then.
Pooled requires four observations per arm overall. Adjusted requires four
per arm in both regions; when even one region falls short, it holds and
breaks the streak. It does not discard that region or interpret the hold as
evidence that the release is safe.

Both rules see the same requests in 2,000 pairs per case (seeds 990000 through
991999). Faults last from send tick 4 through 7. The shared outage adds 30
points to both versions' slow-request rate in A. The global regression adds
18 points to the canary in both regions, and the regional regression adds
22 points to the canary in A alone. The missing-telemetry case is the same
skewed outage with A's control outcomes unavailable at the deadlines.

## Outcomes

| Scenario | Outcome | Pooled | Adjusted |
| --- | --- | ---: | ---: |
| Balanced regional outage | False rollback | 0 / 2,000 | 0 / 2,000 |
| Skewed regional outage | False rollback | 1,327 / 2,000 | 0 / 2,000 |
| Skewed outage, A control late | False rollback | 1,976 / 2,000 | 0 / 2,000 |
| Global canary regression | Detection | 1,816 / 2,000 | 1,456 / 2,000 |
| Region-A canary regression | Detection | 1,450 / 2,000 | 7 / 2,000 |

The adjusted comparator did not roll back in the skewed outage. In the
missing-data variant, it held at all 8,000 affected looks, four per rollout.
The adjusted comparator estimates harm for the overall traffic mix, not for
the worst region. The 22-point canary slowdown confined to A contributes just
5.5 points to that target gap before sampling noise.

| Fault | Rule | Missed | Detected at or after recovery | Mean delay (ticks) | Mean affected-region sends |
| --- | --- | ---: | ---: | ---: | ---: |
| Global | Pooled | 184 | 509 | 3.93 | 400.0 |
| Global | Adjusted | 544 | 604 | 5.09 | 399.9 |
| Region A | Pooled | 550 | 597 | 5.09 | 280.0 |
| Region A | Adjusted | 1,993 | 6 | 8.98 | 280.0 |

A detection uses a triggering send window inside the fault interval;
its delayed decision can arrive after recovery. Misses are charged nine
ticks. Exposure counts canary sends in affected regions through the decision,
not observed slow responses. With faults this short, almost all affected
requests were already sent by the time either rule could roll back.

The same illustrative cutoff does not give equal false-rollback risk. This
experiment shows a tradeoff between avoiding false alarms, preserving local
sensitivity, and holding when evidence is missing. It does not validate a
production guardrail or claim that a version caused an observational gap.
