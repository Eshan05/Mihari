# False rollbacks from correlated requests

The [original benchmark](studies.md#original-benchmark) asks what happens
when a rule checks too often. This study checks **once**. It asks whether
a rule can still roll back a healthy release by counting repeated failures
from the same tenant as independent evidence.

## Protocol

- Randomly assign 3 of 30 tenants to the canary, uniformly without
  replacement. Each tenant sends 100 requests to its assigned version.
  The canary gets 300 requests, but only three tenants.
- The independent workload draws every request separately. The burst
  workload divides each tenant's requests into ten blocks of ten. Each
  block has a 35% chance to share a single success or failure across all
  ten attempts; otherwise its attempts are independent. The marginal
  failure probability stays at 2% for both versions under no effect, or
  rises to 4% for the canary under the modeled fault. This models correlated
  attempts, not a retry scheduler that creates new traffic after failures.
  The burst model increases the variance of each tenant's error count by
  a factor of 4.15 without changing its expected count.
- Both policies read the same error counts at the same checkpoint and roll
  back for a one-sided p-value at or below 0.05. The **request-level** test
  uses Fisher's exact test, treating individual requests as exchangeable.
  The **tenant-level** test holds the observed tenant totals fixed and
  enumerates all 4,060 allowed assignments of three canary tenants. Both
  rank evidence by the canary's error count given the overall total; they
  differ only in what is shuffled.

The tenant test controls false positives at one predeclared look under the
sharp null that the version changes no outcomes, provided the assignment
follows the stated randomization. Ties count in the upper tail, so the test
can be conservative. This is standard [cluster randomization inference](https://pmc.ncbi.nlm.nih.gov/articles/PMC5507602/),
not a new statistical method.

## Results

4,000 paired runs per case, starting at seed 1,210,000. Intervals are
pointwise 95% Wilson intervals over simulated runs.

| Workload | Outcome | Request-level | Tenant-level |
| --- | --- | ---: | ---: |
| Independent requests | False rollback | 145 / 4,000 (3.6%; 3.1–4.3%) | 143 / 4,000 (3.6%; 3.0–4.2%) |
| Retry-like bursts | False rollback | 661 / 4,000 (16.5%; 15.4–17.7%) | 159 / 4,000 (4.0%; 3.4–4.6%) |
| Independent requests | Detect 2% → 4% | 2,349 / 4,000 (58.7%; 57.2–60.2%) | 2,215 / 4,000 (55.4%; 53.8–56.9%) |
| Retry-like bursts | Detect 2% → 4% | 1,852 / 4,000 (46.3%; 44.8–47.8%) | 1,009 / 4,000 (25.2%; 23.9–26.6%) |

With bursts and identical versions, 504 pairs rolled back only under the
request-level rule; two rolled back only under the tenant rule. This happens
without repeated peeking and without increasing the underlying failure
probability. The tenant-aware rule still missed most modeled burst
regressions with only three canary tenants. The detection rates are **not**
a comparison at matched false-rollback risk: the request-level rule bought
more detections in this case while spending far more false rollbacks.

In the scripted example, the canary gets 16 errors in 300 requests and
control gets 54 in 2,700. The extra failures come from one tenant on
identical software. The request test rolls back (`p=0.00110`); the tenant
test does not (`p=0.10`), because 10% of valid assignments put that tenant
on the canary.

This study uses equal tenant volumes, complete telemetry, and known random
assignment. It does not establish a guarantee for an observational rollout,
a weak null of zero *average* effect, or repeated checks. The candidate
does not replace Mihari's staged, regional, or sequential rules.

## Reproduce

```bash
python -m mihari.clustered_requests --example
python -m mihari.clustered_requests --trials 4000
```
