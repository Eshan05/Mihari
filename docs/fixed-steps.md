# Fixed-step canary rollout

Not rolling back a canary is not enough to promote it. This simulator separates stopping on harm from advancing on evidence that both errors and latency are within tolerance. It is the baseline for a later comparison with adaptive traffic, not deployment software.

```bash
python -m mihari.fixed_steps --timeline all
python -m mihari.fixed_steps --trials 200
```

## Decision rules

1. Start at **10%** canary traffic. On the same schedule, advance to **25%**, then **50%**, then end with `PROMOTE`. At each share, send eight windows of 1,000 requests. Evidence is read once per window at its two-tick deadline. During the final two ticks of each stage, route new requests to control only; they are not evidence for that stage. The next stage starts on the following tick.
2. Reuse the joint guardrail at every eligible window. If the same canary-minus-control metric gap exceeds 15 percentage points in two consecutive windows, roll back. Fewer than four observations in either version breaks the sequence, and the guardrail resets at each stage. A guardrail rollback takes priority over a checkpoint on the same tick. This fixed cutoff is not calibrated to a 5% false-rollback rate at these traffic shares.
3. There is one advancement check per stage, after its last window closes. A stage with incomplete on-time telemetry holds. If observed stage control errors exceed 8% or its slow share exceeds 15%, hold rather than attributing shared degradation to the canary. Otherwise, advance only if the upper canary-minus-control bounds are strictly below **+3 error points** and **+5 slow-request points**. Slow means over 100 ms. An inconclusive checkpoint is a terminal hold, not extra looks until a pass.

The harm bound subtracts a one-sided exact lower bound for control from a one-sided exact upper bound for canary. The upper-bound confidence allowance divides 5% across 12 terms: two arms, two metrics, three scheduled checkpoints. Its nominal interpretation assumes fixed samples of independent Bernoulli outcomes. The simulation varies load across windows and reports outcomes empirically; those bounds are not a production guarantee. The control-health limits are illustrative.

## What the study measures

The six scenarios are healthy traffic, a strong canary fault, a latency fault that starts only at 25%, shared degradation, one late telemetry outcome, and a sustained near-margin fault. The last adds four error points: above the promotion tolerance but below the rollback cutoff. In the seeded study, per-arm counts come from a joint error/slow distribution under shared window load. The scripted timeline uses deterministic counts with the same decision engine.

The report separates terminal holds, rollbacks, and promotions. A decision to advance or promote is unsafe if the simulated harm at that stage is at or above either tolerance. This uses scenario truth, not whether the sample happened to look bad. It does not count faults that have not yet appeared, and no post-promotion traffic is simulated. Exposure counts requests sent to the canary, separately tracking sends in faulty windows. All sends on a tick precede the action on that tick. Time includes waiting and drain ticks.

## Seeded results

| Scenario (200 each) | Promoted | Rolled back | Held | Mean faulty-window canary sends |
|---|---:|---:|---:|---:|
| Healthy | 152 | 0 | 48 | 0 |
| Early fault | 0 | 200 | 0 | 440 |
| Fault at 25% | 0 | 153 | 47 | 770 |
| Shared degradation | 0 | 0 | 200 | 0 |
| Missing telemetry | 0 | 0 | 200 | 0 |
| Near-margin harm | 0 | 0 | 200 | 800 |

In the 25% fault case, 47 rollouts held before reaching 25%; the 153 that reached it rolled back. No unsafe advances or promotions were observed. The cost of the evidence requirement is visible in the healthy case: 48 rollouts finished inconclusive. These simulation counts do not rule out false rollbacks or unsafe promotions in deployment.
