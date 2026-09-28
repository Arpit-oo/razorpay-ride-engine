# Ride-Sharing Matching & Pricing Engine — Phase Plan (v2)


## Context
Greenfield (`C:\code\razorpay` has only `notes.md`, `ps.md`). Build in-memory ride-sharing engine in pure Python + pytest, no DB, no 3rd-party libs. Rules from notes.md bind every phase: SOLID small classes, lock-per-item (no global lock, no lock during slow work), injectable clock, no randomness, one-line comment above each code block, caller-authorization on every op, no sensitive data leaks, edge tests (at limit + one past), one concurrency test per shared-data phase. Stop for Arpit's OK after each phase with 4-point report.

## Decisions (confirmed)
- Geo: integer 2D grid, Manhattan distance.
- Scope includes ETA prediction + payment failure handling (from ps.md).
- Cancel: customer may cancel any time before completion, always free.
- Surge: int basis points, 10000 (1.0x) .. 30000 (3.0x).
- Reject: rejecting driver excluded for that ride; re-offer to next-ranked; none left -> `NO_DRIVER_FOUND`.
- Pool capacity: set per driver from vehicle seats.
- Rating: int 1–5, by that ride's customer, after COMPLETED, once.
- Payment: injectable fake gateway, N retries with idempotency key, then `PAYMENT_FAILED`; payment status separate from ride status.
- Money: int paise, INR.

## Modules (13 files -> 6)
```
engine/
  core.py      # Clock, FixedClock, Point, manhattan(), errors, id generator
  models.py    # Driver, Customer, Ride state machine, DriverStats, Caller
  registry.py  # DriverRegistry, RideRegistry — lock per item
  matching.py  # NearestDriverFinder, DriverRanker (all 3 metrics), RoutePlanner, PoolMatcher
  pricing.py   # FareCalculator, DemandTracker, DemandForecaster, SurgeCalculator, EtaPredictor
  rides.py     # RideService (flow + auth), PaymentGateway protocol, PaymentService
tests/  unit/  integration/  stress/
```
Classes stay small/single-job; only file count shrinks.

## Phases (12 -> 6)

**P1 — Foundation** (`core.py`, `models.py`)
Clock, Point/manhattan, safe error hierarchy, deterministic ids. Driver (location, status, seats), Customer, Caller role checks, Ride state machine as pure transitions (no locks yet). `__repr__` hides sensitive fields.
Tests: distance edges, every legal/illegal transition, wrong-role rejects, repr leak.

**P2 — Registry + matching/ranking** (`registry.py`, `matching.py` ranker part)
Per-driver / per-ride locks; atomic `try_claim(driver)` compare-and-set (AVAILABLE -> RESERVED). `NearestDriverFinder`: available drivers within max radius, sorted by distance (basic nearest matching = candidate pool). `DriverRanker`: always uses all three metrics — past performance (completion/acceptance rate), avg rating, proximity — as int weighted score; tie-break by id. Booking offers go in ranker order.
Tests: radius exact/+1, ties, new-driver default score, concurrency: many threads claim same driver -> exactly one wins; location updates no lost writes.

**P3 — Pricing + ETA** (`pricing.py`)
Fare = base + per-cell × distance, min fare. Demand/supply per zone over clock-based window; time-of-day buckets; forecast = deterministic moving average of past windows; surge clamped bps. ETA = distance × zone traffic factor(time bucket) × driver speed factor. Quote carries price + ETA + expiry.
Tests: cap 30000/30001, floor 10000, window & bucket boundaries, min-fare boundary, concurrent tracker updates.

**P4 — Ride lifecycle + payments** (`rides.py`)
request (quote, rank, offer) -> accept / reject (re-offer) / offer-timeout -> start -> complete -> pay -> rate; cancel free anytime before complete. Payment: idempotency key, bounded retries, gateway called outside all locks.
Tests: double-accept race, cancel-vs-accept race, reject chain to NO_DRIVER_FOUND, one active ride per customer, pay success on retry N / fail at N+1, concurrent pay charges once, rating once & 1–5 edges.

**P5 — Pooling** (`matching.py` pool part)
RoutePlanner: permutations with pickup-before-drop, min total distance, deterministic tie-break. PoolMatcher: join if seats left and every rider's detour ≤ limit; replan on cancel.
Tests: seats exact/+1, detour exact/+1, order constraint, concurrent joins never exceed seats, cancel mid-pool replans.

**P6 — Integration + stress + mutation**
E2E flows (solo, pool, reject chain, cancel, payment failure, ranking shift after ratings). Stress tests marked `slow` with deadlock timeouts. Manual mutation pass (flip bounds/comparisons, confirm tests fail).

## Weaknesses found (and fix baked in above)
1. **Nearest vs recommendation conflict** — v1 had two matchers, unclear which books. Fix: nearest finder only builds candidate pool within radius; ranker always scores with all three metrics (performance, rating, proximity) and decides offer order.
2. **Stale snapshot race** — driver picked as available, then taken by another ride. Fix: atomic `try_claim` under driver lock; fall to next candidate on failure.
3. **Deadlock risk in pooling** — one driver, many ride locks. v1 rule "ride then driver" insufficient. Fix (revised in P4): global lock order = ride lock(s) outer (sorted by id when several), driver lock inner; driver/customer registries never take a ride lock, so no cycle. Needed so ride + driver status change atomically (cancel-vs-accept race).
4. **Stuck OFFERED ride** — driver never responds. Fix: offer expiry via clock, checked lazily on next op -> treated as reject.
5. **Price drift** — surge changes between quote and booking. Fix: quote locks price, has expiry; expired quote rejected.
6. **Customer double booking** — two concurrent requests. Fix: one active ride per customer, enforced under customer lock.
7. **Driver goes OFFLINE mid-ride** — Fix: only allowed when no active ride.
8. **Pool permutation blow-up** — driver-set seats unbounded; 6 riders = 12 stops = huge factorial. Needs seat cap (see open Q).
9. **Pool fare split undefined** — shared ride price per rider not specified (see open Q).
10. **Payment "charged but reply lost"** — retry could double charge. Fix: idempotency key per ride; gateway dedupes.
11. **Free cancel abuse** — unlimited cancels cost nothing; unchanged per your decision, only noted.
12. **ETA "route optimization" is thin on a Manhattan grid** — shortest path is trivial; value comes only from traffic factors. Honest limitation.
13. **"Predicted demand" is just moving average** — deterministic and simple, not ML. Honest limitation.
14. **Mutation testing without libraries** — only manual; less rigorous than mutmut.
15. **Bigger phases vs "reviewable in 2-3 min" rule** — merging to 6 phases makes P2 and P4 heavier. Mitigation: I can split P4 (lifecycle / payments) if review runs long.

## Open (ask at the phase that needs it)
- P2: ranking weights (proximity / rating / completion rate).
- P3: base fare, per-cell rate, min fare, surge weights, time buckets, offer/quote expiry seconds.
- P5: max seats cap, max detour %, pooled fare split rule.

## Verification
- Each phase: `python -m pytest tests -m "not slow" -q`; report test count before -> after.
- Final: `python -m pytest tests -q` incl. stress; manual mutation pass.
