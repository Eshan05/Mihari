# Does changing the middle canary share help?

This experiment changes one decision in the [fixed-step rollout](fixed-steps.md). Both policies start at 10%, run the same checkpoints, and must pass a middle-stage checkpoint to reach 50%. The fixed comparator always chooses 25% in the middle. The adaptive schedule can choose 15%, 25%, or 35%.

```bash
python -m mihari.adaptive_steps --timeline all
python -m mihari.adaptive_steps --trials 200
```

After a passing 10% checkpoint, take the larger of the error upper bound divided by its margin and the slow-request upper bound divided by its margin. Use 35% at or below 0.70, 15% at or above 0.90, and 25% otherwise. No share is chosen if the checkpoint holds or the guardrail rolls back. Those cutoffs were set before this run; they are not optimized. Stage lengths, telemetry deadlines, control-only drain, rollback and promotion gates stay the same. Rereading the same checkpoint bounds to choose a share adds no new looks.

## What is paired

Each of the six scenarios has 200 seed pairs starting at 40,000. A seed and send tick give both policies the same offered demand, window load, randomized request order, and request-level outcome draws. Requests earlier in that order go to the canary, so shares are nested. An outcome matches when the request goes to the same version at the same simulated fault level. Changing the share can activate the fault that appears only at 25% or higher, so realized outcomes need not match. The published fixed-step numbers used an aggregate sampler; the fixed comparator in this report uses these new paired inputs, not that transcript.

## Reading the report

The benchmark shows actual middle-share choices, entries into 50%, evaluated final checkpoints, terminal holds, promotions and rollbacks. It reports false rollbacks separately under healthy traffic and shared degradation, and unsafe decisions against the simulated truth at their own stage. Paired time and exposure differences include both trajectories even when one stops early.

For the larger-share fault, the report distinguishes activating at 25%, 35%, or 50% from never activating. A run that stops on the 15% step has not detected the fault. Fewer requests sent to a faulty canary can reflect an earlier hold rather than better detection. The scripted timelines illustrate share selection, not performance. The seeded results are descriptive; this study does not establish equal risk or production safety.

## Seeded result

| Outcome (200 pairs per scenario) | Fixed | Adaptive |
|---|---:|---:|
| Healthy promotions | 154 | 155 |
| Larger-share fault activations | 155 | 155 |
| Rollbacks after activation | 155 | 155 |
| Mean faulty-window canary sends, larger-share fault | 780.0 | 1,061.8 |

The adaptive rule offered little advantage on healthy traffic and exposed more requests in the larger-share fault. It first activated that fault at 35% in 64 runs and deferred first activation to 50% in 31. The fixed rule first tested it at 25% in every run that reached it. A smaller middle step was not necessarily safer: it could postpone testing the hazard until more traffic was at stake. Both policies behaved identically in the other four scenarios, which stopped at the first stage. No unsafe advances or promotions were observed in these seeded runs.
