from engine.core import Point
from engine.models import Caller, DriverStatus, RideStatus, Role
from engine.rides import SYSTEM, PaymentStatus
from tests.integration.world import CUST, D1, D2, OTHER_CUST, World


# Book c1 pooled (accepted by d1), then c2 pooled heading the same way; returns both ride ids.
def two_pooled(w):
    r1 = w.book(CUST, (0, 0), (5, 0), pooled=True)
    w.service.accept(D1, r1)
    r2 = w.book(OTHER_CUST, (1, 0), (5, 0), pooled=True)
    return r1, r2


# Second pooled rider joins d1's pool (not free d2); both pay 25% less; d1 freed only after last drop.
def test_two_riders_share_driver():
    w = World()
    r1, r2 = two_pooled(w)
    assert w.driver_of(r2) == "d1"
    assert w.driver_status("d2") is DriverStatus.AVAILABLE
    w.service.accept(D1, r2)
    w.service.start(D1, r1)
    w.service.start(D1, r2)
    assert w.service.complete(D1, r2) is PaymentStatus.PAID
    assert w.driver_status("d1") is DriverStatus.BUSY
    assert w.service.complete(D1, r1) is PaymentStatus.PAID
    assert w.driver_status("d1") is DriverStatus.AVAILABLE
    # c2: 4 cells -> 7800 * 75% = 5850; c1: 5 cells -> 9000 * 75% = 6750.
    assert sorted(w.gateway.charged.values()) == [5850, 6750]
    assert not w.pools.has_pool(SYSTEM, "d1")


# Pickup ETA for the second rider follows the pool route (via c1's pickup), not a straight line.
def test_pooled_pickup_eta_follows_route():
    w = World()
    r1, r2 = two_pooled(w)
    w.service.accept(D1, r2)
    assert w.service.pickup_eta(OTHER_CUST, r2) == 120


# One pooled rider cancels: pool keeps the other rider and d1 stays busy.
def test_pool_cancel_keeps_others():
    w = World()
    r1, r2 = two_pooled(w)
    w.service.accept(D1, r2)
    w.service.cancel(OTHER_CUST, r2)
    assert w.driver_status("d1") is DriverStatus.BUSY
    stops = w.pools.route(SYSTEM, "d1").stops
    assert {s.rider_id for s in stops} == {r1}


# Pool driver rejects the joining rider: rider goes to d2, d1 keeps first rider and stays busy.
def test_pool_reject_moves_rider_on():
    w = World()
    r1, r2 = two_pooled(w)
    w.service.reject(D1, r2)
    assert w.driver_of(r2) == "d2"
    assert w.driver_status("d1") is DriverStatus.BUSY
    assert w.status(r1) is RideStatus.ACCEPTED


# Rider whose trip would be ruined by the detour is not pooled with d1; goes to free d2 instead.
def test_far_rider_not_pooled():
    w = World()
    r1 = w.book(CUST, (0, 0), (9, 0), pooled=True)
    w.service.accept(D1, r1)
    r2 = w.book(OTHER_CUST, (0, 5), (0, 9), pooled=True)
    assert w.driver_of(r2) == "d2"


# Engine counts supply itself: two drivers taken, third customer sees surge from demand 2 vs supply 0.
def test_quote_supply_counted_by_engine():
    w = World()
    w.book(CUST)
    w.book(OTHER_CUST)
    quote = w.service.get_quote(Caller("c3", Role.CUSTOMER), Point(0, 0), Point(5, 0))
    assert quote.surge_bps == 20000
