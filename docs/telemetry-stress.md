# What late canary telemetry changes

The [fixed-share study](fixed-exposure.md) checked every window after a two-tick grace period, regardless of whether its telemetry had already arrived. This follow-up changes only the availability times in each paired replay. It also tests faults the two rules might miss: a sustained 3 percentage-point increase in canary errors and an 8-point increase in just one window. Both start in window 4. Healthy traffic and a shared outage are controls.

```bash
python -m mihari.stress --trials 500
python -m mihari.live --telemetry-stress
```

## How the replay works

- Each pair uses the same pre-generated outcomes, 90/10 assignment, and sends up to the point at which a policy would roll back. The prompt replay receives outcomes on the send tick. In the delayed replay, control is still prompt; each canary delay is drawn independently of its outcome: 0, 1, 2, or 3 ticks with probabilities 40%, 30%, 20%, and 10%.
- Both profiles use the same scheduler. It processes windows in order, as soon as all outcomes arrive or two ticks after send. A closed window is never revised. Missing outcomes do not enter rates or discordant pairs. A window with fewer than four observed outcomes on either version cannot trigger rollback. Sends on a tick happen before any decisions on that tick.
- For each policy, calibration chooses one cutoff targeting at most 5% false rollbacks under each combination of null scenario and telemetry profile. The cutoff is frozen and shared across both profiles. Calibration uses even-seeded pairs, evaluation uses odd-seeded pairs. Both use the same final observation tick. A calibration target is not a guarantee or evidence of equal risk across policies.

## Held-out results

| Fault | Rule | Prompt detections | Delayed detections |
|---|---|---:|---:|
| Weak sustained | Two-window threshold | 25/500 | 30/500 |
| Weak sustained | Sequential likelihood ratio | 84/500 | 76/500 |
| One window | Two-window threshold | 3/500 | 2/500 |
| One window | Sequential likelihood ratio | 40/500 | 36/500 |

Both rules missed most faults in this setup. In the one-window case, every detection with delayed telemetry came at or after recovery. Healthy and shared-outage false-rollback rates also differed between rules; these are not equal-risk comparisons. The command reports the rates, intervals, paired counts and canary exposure for each scenario.

## How to read the output

The report separates additional waiting (window close lag) from information lost at the deadline (canary outcomes excluded). Those two diagnostics use the full trace, even if a particular policy would have stopped earlier. The telemetry intervention changes both of them; a detection difference is not a pure waiting-time effect.

Rates include Wilson intervals. A detection is operationally a rollback based on a window at or after fault onset, not proof the fault caused it. In particular, the one-window case reports detections that occurred at or after recovery separately. Misses include stops on pre-fault windows. Mean delay charges misses the full nine-tick observation horizon. Exposure counts only canary sends in affected windows. In the one-window fault, even a prompt diagnosis cannot prevent requests already sent on that tick. A pre-fault false stop can also reduce exposure without detecting the fault. The paired counts show how often only one profile detected a fault.

## What the demo shows

The demo uses two real loopback HTTP listeners and the same scripted status trajectory in all three runs. It models when the monitor receives status outcomes; the two ticks are not real-time network delays. The prompt and delayed examples roll back at ticks 5 and 7, after 8 and 16 affected canary requests respectively. The timeout example shows a window with only three of four canary outcomes: it holds at the deadline, then may act on later windows. The demo uses illustrative cutoffs, not the study's fitted cutoffs.
