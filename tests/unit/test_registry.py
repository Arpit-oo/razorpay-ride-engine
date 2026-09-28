import threading

import pytest

from engine.core import NotAllowedError, NotFoundError, Point, ValidationError
from engine.models import Caller, Driver, DriverStatus, Ride, RideStatus, Role
from engine.registry import DriverRegistry, RideRegistry

# Reusable callers.
SYS = Caller("sys", Role.SYSTEM)
CUST = Caller("c1", Role.CUSTOMER)


# Driver caller for a given id.
def drv(driver_id: str) -> Caller:
    return Caller(driver_id, Role.DRIVER)


# Registry with given driver ids registered and online.
def registry_with(*ids: str) -> DriverRegistry:
    reg = DriverRegistry()
    for i in ids:
        reg.register(drv(i), Driver(i, "N", Point(0, 0), 2))
        reg.go_online(drv(i), i)
    return reg


# --- DriverRegistry ---

# Only the driver can register themself; duplicates rejected.
def test_register_who_and_duplicate():
    reg = DriverRegistry()
    d = Driver("d1", "N", Point(0, 0), 2)
    for bad in (drv("d2"), SYS, CUST):
        with pytest.raises(NotAllowedError):
            reg.register(bad, d)
    reg.register(drv("d1"), d)
    with pytest.raises(ValidationError):
        reg.register(drv("d1"), Driver("d1", "N", Point(0, 0), 2))


# Unknown driver id -> NotFound.
def test_unknown_driver():
    with pytest.raises(NotFoundError):
        DriverRegistry().go_online(drv("x"), "x")


# Location update visible in snapshot; wrong driver rejected.
def test_update_location():
    reg = registry_with("d1")
    reg.update_location(drv("d1"), "d1", Point(4, 5))
    with pytest.raises(NotAllowedError):
        reg.update_location(drv("d2"), "d1", Point(9, 9))
    assert reg.snapshot(SYS)[0].location == Point(4, 5)


# Claim succeeds once; second claim fails; release makes it claimable again.
def test_try_claim_once():
    reg = registry_with("d1")
    assert reg.try_claim(SYS, "d1") is True
    assert reg.try_claim(SYS, "d1") is False
    reg.release(SYS, "d1")
    assert reg.try_claim(SYS, "d1") is True


# Claim and snapshot are dispatcher-only.
def test_claim_snapshot_who():
    reg = registry_with("d1")
    with pytest.raises(NotAllowedError):
        reg.try_claim(drv("d1"), "d1")
    with pytest.raises(NotAllowedError):
        reg.snapshot(CUST)


# Stats updates dispatcher-only and visible in snapshot copy.
def test_stats_recorded():
    reg = registry_with("d1")
    reg.record_accept(SYS, "d1")
    reg.record_complete(SYS, "d1")
    reg.record_rating(SYS, "d1", 4)
    with pytest.raises(NotAllowedError):
        reg.record_rating(CUST, "d1", 5)
    stats = reg.snapshot(SYS)[0].stats
    assert (stats.accepted, stats.completed, stats.rating_sum) == (1, 1, 4)


# Snapshot is a copy: later changes do not alter it.
def test_snapshot_is_copy():
    reg = registry_with("d1")
    view = reg.snapshot(SYS)[0]
    reg.try_claim(SYS, "d1")
    reg.record_accept(SYS, "d1")
    assert view.status is DriverStatus.AVAILABLE
    assert view.stats.accepted == 0


# Concurrency: many threads claim the same driver, exactly one wins.
def test_concurrent_claim_single_winner():
    reg = registry_with("d1")
    wins = []
    barrier = threading.Barrier(16)

    # Each worker waits for all, then tries once.
    def worker():
        barrier.wait()
        wins.append(reg.try_claim(SYS, "d1"))

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
        assert not t.is_alive()
    assert wins.count(True) == 1


# --- RideRegistry ---

# Add is dispatcher-only; duplicate and unknown ids rejected; apply runs action.
def test_ride_registry_basic():
    rides = RideRegistry()
    ride = Ride("r1", "c1", Point(0, 0), Point(1, 1))
    with pytest.raises(NotAllowedError):
        rides.add(CUST, ride)
    rides.add(SYS, ride)
    with pytest.raises(ValidationError):
        rides.add(SYS, Ride("r1", "c1", Point(0, 0), Point(1, 1)))
    with pytest.raises(NotFoundError):
        rides.apply("nope", lambda r: r.status)
    rides.apply("r1", lambda r: r.cancel(CUST))
    assert rides.apply("r1", lambda r: r.status) is RideStatus.CANCELLED
