import pytest

from engine.core import InvalidTransitionError, NotAllowedError, Point, ValidationError
from engine.models import Caller, Driver, DriverStats, DriverStatus, Role

# Reusable callers: the driver, another driver, and the dispatcher.
DRV = Caller("d1", Role.DRIVER)
OTHER_DRV = Caller("d2", Role.DRIVER)
SYS = Caller("sys", Role.SYSTEM)


# Fresh offline driver d1.
def new_driver() -> Driver:
    return Driver("d1", "Ravi", Point(0, 0), 3)


# Full happy cycle: online -> reserved -> busy -> available -> offline.
def test_driver_happy_cycle():
    d = new_driver()
    d.go_online(DRV)
    d.reserve(SYS)
    d.mark_busy(SYS)
    d.release(SYS)
    d.go_offline(DRV)
    assert d.status is DriverStatus.OFFLINE


# Release also works straight from reserved (offer rejected).
def test_release_from_reserved():
    d = new_driver()
    d.go_online(DRV)
    d.reserve(SYS)
    d.release(SYS)
    assert d.status is DriverStatus.AVAILABLE


# Cannot go offline while reserved or busy.
def test_offline_blocked_mid_ride():
    d = new_driver()
    d.go_online(DRV)
    d.reserve(SYS)
    with pytest.raises(InvalidTransitionError):
        d.go_offline(DRV)
    d.mark_busy(SYS)
    with pytest.raises(InvalidTransitionError):
        d.go_offline(DRV)
    assert d.status is DriverStatus.BUSY


# Illegal moves: reserve offline, busy from available, release offline, online twice.
def test_illegal_driver_moves():
    d = new_driver()
    with pytest.raises(InvalidTransitionError):
        d.reserve(SYS)
    with pytest.raises(InvalidTransitionError):
        d.release(SYS)
    d.go_online(DRV)
    with pytest.raises(InvalidTransitionError):
        d.go_online(DRV)
    with pytest.raises(InvalidTransitionError):
        d.mark_busy(SYS)


# Online/offline only by the driver; reserve/busy/release only by dispatcher.
def test_driver_status_who():
    d = new_driver()
    with pytest.raises(NotAllowedError):
        d.go_online(OTHER_DRV)
    with pytest.raises(NotAllowedError):
        d.go_online(SYS)
    d.go_online(DRV)
    with pytest.raises(NotAllowedError):
        d.reserve(DRV)
    d.reserve(SYS)
    with pytest.raises(NotAllowedError):
        d.mark_busy(DRV)
    with pytest.raises(NotAllowedError):
        d.release(DRV)
    assert d.status is DriverStatus.RESERVED


# Rating edges: 1 and 5 allowed, 0 and 6 rejected, non-int rejected.
def test_rating_edges():
    s = DriverStats()
    s.add_rating(1)
    s.add_rating(5)
    assert (s.rating_sum, s.rating_count) == (6, 2)
    for bad in (0, 6, True, 3.0):
        with pytest.raises(ValidationError):
            s.add_rating(bad)
    assert (s.rating_sum, s.rating_count) == (6, 2)


# Copy is independent of the original.
def test_stats_copy_independent():
    s = DriverStats()
    c = s.copy()
    s.record_accept()
    assert c.accepted == 0 and s.accepted == 1
