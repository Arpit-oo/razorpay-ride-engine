import threading

import pytest

from engine.core import NotAllowedError, Point, ValidationError
from engine.matching import PoolMatcher, PoolRider, RoutePlanner, within_detour
from engine.models import Caller, Role
from engine.pricing import pooled_fare

# Reusable callers and origin.
SYS = Caller("sys", Role.SYSTEM)
CUST = Caller("c1", Role.CUSTOMER)
O = Point(0, 0)


# Rider helper from coordinate pairs.
def rider(rid, p, d, onboard=False):
    return PoolRider(rid, Point(*p), Point(*d), onboard)


# Detour limit edge: solo 4 at 50% allows 6, rejects 7.
def test_within_detour_edge():
    assert within_detour(6, 4, 50)
    assert not within_detour(7, 4, 50)


# Shortest route nests B inside A, and every pickup precedes its drop.
def test_plan_shortest_and_ordered():
    route = RoutePlanner().plan(O, [rider("A", (1, 0), (5, 0)), rider("B", (2, 0), (4, 0))])
    assert [(s.rider_id, s.kind) for s in route.stops] == [("A", "pickup"), ("B", "pickup"),
                                                           ("B", "drop"), ("A", "drop")]
    assert route.total == 5


# Shortest overall route (4) makes A wait too long, so the planner drops A first (total 6).
def test_plan_respects_detour():
    riders = [rider("A", (0, 0), (0, 1)), rider("B", (1, 0), (0, 2))]
    assert RoutePlanner(detour_pct=1000).plan(O, riders).total == 4
    route = RoutePlanner().plan(O, riders)
    assert route.total == 6
    assert [(s.rider_id, s.kind) for s in route.stops] == [("A", "pickup"), ("A", "drop"),
                                                           ("B", "pickup"), ("B", "drop")]


# Onboard rider only needs a drop stop.
def test_plan_onboard_rider():
    route = RoutePlanner().plan(O, [rider("A", (0, 0), (3, 0), onboard=True)])
    assert [(s.rider_id, s.kind) for s in route.stops] == [("A", "drop")]


# Seats: driver's 2 seats hold 2 riders; 6 seats capped at 4.
@pytest.mark.parametrize("seats,fits", [(2, 2), (6, 4)])
def test_join_seat_limit(seats, fits):
    pool = PoolMatcher(RoutePlanner())
    pool.open_pool(SYS, "d1", seats, O)
    results = [pool.try_join(SYS, "d1", rider(f"r{i}", (1, 0), (5, 0))) for i in range(fits + 1)]
    assert results == [True] * fits + [False]


# Join refused when no route keeps the existing rider within detour; pool unchanged.
def test_join_refused_on_detour():
    pool = PoolMatcher(RoutePlanner())
    pool.open_pool(SYS, "d1", 4, O)
    assert pool.try_join(SYS, "d1", rider("A", (0, 0), (10, 0)))
    before = pool.route(SYS, "d1")
    assert not pool.try_join(SYS, "d1", rider("B", (0, 5), (0, 10)))
    assert pool.route(SYS, "d1") == before


# Removing a rider replans without them; only dispatcher may manage pools.
def test_remove_and_who():
    pool = PoolMatcher(RoutePlanner())
    pool.open_pool(SYS, "d1", 4, O)
    pool.try_join(SYS, "d1", rider("A", (1, 0), (5, 0)))
    pool.try_join(SYS, "d1", rider("B", (2, 0), (4, 0)))
    pool.remove(SYS, "d1", "B")
    assert {s.rider_id for s in pool.route(SYS, "d1").stops} == {"A"}
    with pytest.raises(NotAllowedError):
        pool.try_join(CUST, "d1", rider("C", (1, 0), (2, 0)))


# Pooled fare: 25% off, rounded down; bad discount rejected.
def test_pooled_fare():
    assert pooled_fare(10000) == 7500
    assert pooled_fare(9999) == 7499
    with pytest.raises(ValidationError):
        pooled_fare(100, 101)


# Concurrency: 10 riders race to join a 4-seat pool; exactly 4 get in.
def test_concurrent_joins_respect_seats():
    pool = PoolMatcher(RoutePlanner())
    pool.open_pool(SYS, "d1", 4, O)
    wins = []
    barrier = threading.Barrier(10)

    # Each rider waits for all, then tries to join.
    def join(i):
        barrier.wait()
        wins.append(pool.try_join(SYS, "d1", rider(f"r{i}", (1, 0), (5, 0))))

    threads = [threading.Thread(target=join, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive()
    assert wins.count(True) == 4
