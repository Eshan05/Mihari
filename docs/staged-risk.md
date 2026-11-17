# Staged rollouts: common-time vs share-triggered faults

The [earlier comparison](adaptive-steps.md) tested a slowdown that started
only at 25% canary traffic or above. A smaller middle step could delay
activation, so it was hard to tell whether it reduced exposure or postponed
the test. Here the slowdown also starts at a fixed time, independent of share.

```bash
python -m mihari.staged_risk
python -m mihari.staged_risk --timeline
```

## Protocol

Both schedules start at 10%, finish at 50% if their checkpoints pass, and
use the same guardrail, deadlines, and control-only drains. The fixed middle
share is 25%; the existing adaptive comparator chooses 15%, 25%, or 35% from
the first checkpoint. Each case uses 2,000 fresh seed pairs (970000 through
971999) with the same offered traffic, randomized routing order, and outcome
draws by tick. A higher canary share includes the lower share's requests.

Both faults add 22 percentage points to the canary's slow-request rate. The
common-time fault begins at tick 10, the first possible middle send. The
share-triggered fault begins only at a share of 25% or above. The scripted
15% example shows the distinction: both schedules roll back at tick 13 when
the fault starts on the clock. With the share trigger, the 15% path first
sends affected requests at tick 20, after advancing to 50%.

The report counts affected-window canary sends through the decision tick,
including in-flight requests and sends before the first observations.
Nonactivation is not detection. Delay is measured from first affected send
and averaged only over detections; misses and nonactivations are separate.
Exposure counts all canary sends during the fault, not just slow responses.

## False rollbacks

Neither schedule rolled back in any healthy or shared-degradation pair.
Both simultaneous 95% intervals for adaptive minus fixed were -0.25 to
+0.25 percentage points, inside the predeclared +/-1.5-point margin. The
risks were comparable in these modeled cases, not proven to be zero.
On healthy traffic, fixed promoted 1,524 and held 476; adaptive promoted
1,513 and held 487. Both always held during shared degradation.

## Faults

| Onset | Schedule | Activated | Detected | Never activated | Mean ticks to detection | Mean affected sends, all seeds |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Common time | Fixed | 1,530 | 1,530 | 470 | 3.02 | 768.8 |
| Common time | Adaptive | 1,530 | 1,530 | 470 | 3.03 | 821.1 |
| Share trigger | Fixed | 1,530 | 1,530 | 470 | 3.02 | 768.8 |
| Share trigger | Adaptive | 1,517 | 1,517 | 483 | 3.01 | 1,011.8 |

There were no pre-fault rollbacks or misses after activation. The adaptive
schedule sent an average of **52.4 more affected requests** on the common-time
fault and **243.0 more** on the share-triggered fault across all pairs.
Restricting to pairs where both activated, the differences were +68.5
(1,530 pairs) and +329.3 (1,517 pairs), respectively. With the share trigger,
281 adaptive runs first encountered harm at 50%, and 13 never activated
even though their fixed counterpart did.

Adaptive exposure was higher even when both schedules hit the fault at the
same time. The share trigger made the difference larger by deferring some
tests until the 50% stage. This result covers one adaptive choice rule, one
simulated effect size, and two modeled no-regression cases. It does not
validate a production risk budget or establish that fixed stages always win.
