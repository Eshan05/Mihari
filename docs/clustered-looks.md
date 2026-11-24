# Repeated looks on correlated canary traffic

The [original benchmark](studies.md#original-benchmark) shows what happens
when we check too often. The [single-checkpoint tenant study](clustered-requests.md)
shows what happens when we count correlated requests as independent. This
companion benchmark crosses the two; neither earlier result changes.

## Protocol

- Randomly assign 3 of 30 equal-volume tenants to the canary and keep that
  assignment for all 20 looks. Each look adds ten requests per tenant.
  A policy that never rolls back sees 6,000 total requests, including 600
  canary requests.
- Reuse the earlier independent and retry-like burst models. In the burst
  model, each tenant's ten requests in a look have a 35% chance to share
  one outcome. The underlying error rate is 2% on both versions in the
  no-effect cases. In the fault cases, it is 4% on canary from the first
  look and remains 2% on control.
- Both tests use cumulative errors and the same evidence through each look.
  The request test assumes individual requests could have been assigned
  separately. The exact tenant test instead enumerates the 4,060 valid
  allocations of three canary tenants. At each look, both rank allocations
  by the canary error count; only the unit of reassignment changes.
- **Final only** checks look 20 at 5%. **Repeated 5%** stops at the first
  crossing of 5%. **Split budget** stops at the first crossing of 0.25%,
  spending at most 5% across all 20 planned looks. The union bound does not
  require independent looks, but it does require valid individual p-values.
  That condition holds for the tenant test under the sharp null and stated
  random assignment; the request test does not satisfy it under bursts.

## False rollbacks over the entire rollout

4,000 paired seeds per case starting at 1,260,000. Percentages are per
rollout, not per look. The CLI prints pointwise 95% Wilson intervals.

| Unit | Schedule | Independent requests | Retry-like bursts |
| --- | --- | ---: | ---: |
| Request | Final only | 140 / 4,000 (3.5%) | 715 / 4,000 (17.9%) |
| Request | Repeated 5% | 531 / 4,000 (13.3%) | 1,270 / 4,000 (31.8%) |
| Request | Split budget | 30 / 4,000 (0.8%) | 774 / 4,000 (19.4%) |
| Tenant | Final only | 136 / 4,000 (3.4%) | 173 / 4,000 (4.3%) |
| Tenant | Repeated 5% | 523 / 4,000 (13.1%) | 528 / 4,000 (13.2%) |
| Tenant | Split budget | 34 / 4,000 (0.9%) | 35 / 4,000 (0.9%) |

**With 20 looks, neither fix worked alone.** The tenant-aware test still
overreacted at 5% per look. Splitting the budget across looks did not rescue
the request-level test under bursts: its per-look p-values already
overstated the evidence. With both corrections, the observed rate was
0.9% in both cases, well below the 5% budget.

## Detection and affected sends

Detection means a rollback by look 20. Mean exposure includes every run,
counts all sends through the decision look, and charges a miss all 600
canary sends. These are requests routed to the modeled faulty version,
not incremental customer errors.

| Unit | Schedule | Independent: detected | Independent: mean sends | Bursts: detected | Bursts: mean sends |
| --- | --- | ---: | ---: | ---: | ---: |
| Request | Final only | 85.0% | 600.0 | 65.1% | 600.0 |
| Request | Repeated 5% | 91.2% | 248.4 | 76.4% | 307.4 |
| Request | Split budget | 58.1% | 449.6 | 56.1% | 407.6 |
| Tenant | Final only | 82.7% | 600.0 | 35.0% | 600.0 |
| Tenant | Repeated 5% | 90.4% | 256.2 | 55.3% | 391.8 |
| Tenant | Split budget | 44.2% | 493.1 | 11.3% | 564.7 |

The equally split tenant rule was conservative and missed most 2%-to-4%
regressions under bursts. The valid final-only tenant test detected more,
but couldn't spare a single canary request before the deadline. These
rules did not have matched observed false-rollback rates, so the table
doesn't establish a general power ranking. It also doesn't cover unequal
tenant volumes, telemetry delays, adaptive assignment, or more than one
guardrail.

## Reproduce

```bash
python -m mihari.clustered_looks --example
python -m mihari.clustered_looks --trials 4000
```

The scripted A/A example puts ten failed attempts on one canary tenant at
look 2. The request-level test rolls back even with the split budget. The
tenant test sees that one tenant would land on the canary in 10% of valid
assignments and does not roll back.
