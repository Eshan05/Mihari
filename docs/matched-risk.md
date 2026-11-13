# Do the guardrail rules have comparable false-rollback risk?

The [joint guardrail study](joint-guardrail.md) gave both rules a 5% calibration target. That does not mean they had equal false-rollback rates. This follow-up keeps their score functions, telemetry, fixed 90/10 traffic split, and workloads unchanged. It tries to match the rates during calibration, then checks whether the match holds on fresh traffic.

```bash
python -m mihari.matched_risk
```

The command is finite, can take several minutes, and writes no files. Existing HTTP demos still use illustrative cutoffs and do not verify these results.

## Calibration

Use 2,000 fresh rollout traces per null scenario: healthy traffic and shared degradation. Compute each rule's largest score in each rollout. Scores must strictly exceed the cutoff. Choose the lowest nonnegative error-only cutoff that caps empirical false rollbacks at 5% in both nulls. For the joint rule, search all distinct feasible cutoffs for the smallest worst absolute difference from the error-only reference across the two nulls. Ties favor higher calibration risk in the worst null, then the stricter cutoff. This prioritizes matching the reference without loosening the calibration cap; it does not force an exact match.

## Held-out check

Freeze the cutoffs. Evaluate 3,000 paired rollouts per scenario, with both rules seeing the same outcomes. Calibration uses seeds starting at 10,000 in steps of two; holdout uses the intervening odd seeds. The study does not use holdout outcomes to set cutoffs or change the run size. Reusing seeds across scenarios provides common random numbers; the uncertainty adjustment does not assume the two null scenarios are independent.

The report uses a practical margin of **1.5 percentage points either way** for each null. It calls the rates matched only when both 95% simultaneous intervals for *joint minus error-only* lie strictly inside that band. Overlap between the individual rates' confidence intervals is not enough. Matching does not imply that either held-out rate is below 5%, and an inconclusive match is not itself proof of different risks.

In each null, let `q` be the chance the two rules disagree, and `p` the chance that the joint rule is the only one to roll back given disagreement. The risk difference is `q * (2*p - 1)`. The study bounds both terms with [exact binomial intervals](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats._result_classes.BinomTestResult.proportion_ci.html). It allocates the error probability across both terms in both null scenarios, then takes the smallest and largest product allowed by the intervals. This is conservative. If no disagreements occur in the sample, the direction remains unconstrained rather than reporting zero uncertainty.

## Fault results

The report always shows paired detections, misses, time, and affected-window canary sends for error-only, latency-only and combined faults. As before, a rollback on a window at or after fault onset counts as detection, not proof of attribution. Misses include pre-fault stops and receive the full observation horizon in the mean-delay summary. Early false rollbacks can reduce exposure without detecting the fault. If matching is not established, read these numbers as tradeoffs, not an equal-risk ranking. Even a match here says nothing about production risk.
