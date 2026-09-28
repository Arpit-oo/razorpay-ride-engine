import threading

import pytest

from engine.core import FixedClock, NotAllowedError, Point, ValidationError
from engine.models import Caller, DriverStats, DriverStatus, Role
from engine.pricing import (
    DemandForecaster,
    DemandTracker,
    EtaPredictor,
    FareCalculator,
    Quoter,
    SurgeCalculator,
    SurgeWeights,
    TimeOfDayFactor,
    ZoneMap,
    clamp_bps,
    ratio_bps,
)
from engine.registry import DriverView

# Reusable callers.
SYS = Caller("sys", Role.SYSTEM)
CUST = Caller("c1", Role.CUSTOMER)
OTHER_CUST = Caller("c2", Role.CUSTOMER)
DRV = Caller("d1", Role.DRIVER)
ZONE = (0, 0)


# Wire a full pricing stack on a given clock.
def stack(clock):
    tracker = DemandTracker(clock)
    forecaster = DemandForecaster(tracker)
    tod = TimeOfDayFactor()
    surge = SurgeCalculator(tracker, forecaster, tod, clock)
    eta = EtaPredictor(tod, clock)
    quoter = Quoter(FareCalculator(), surge, eta, ZoneMap(10), clock)
    return tracker, surge, eta, quoter


# --- clamp / ratio ---

# Cap: 30000 stays, 30001 clamps; floor: 10000 stays, 9999 clamps.
def test_clamp_edges():
    assert clamp_bps(30000) == 30000
    assert clamp_bps(30001) == 30000
    assert clamp_bps(10000) == 10000
    assert clamp_bps(9999) == 10000


# Demand/supply ratio incl. zero supply and cap edge.
@pytest.mark.parametrize("demand,supply,expected", [
    (0, 0, 10000), (1, 0, 30000), (1, 1, 10000), (3, 2, 15000),
    (1, 2, 10000), (3, 1, 30000), (31, 10, 30000),
])
def test_ratio_bps(demand, supply, expected):
    assert ratio_bps(demand, supply) == expected


# --- Fare ---

# Min-fare edge: exactly at minimum, one cell past it, and below it.
def test_fare_min_edges():
    fares = FareCalculator(base=3000, per_cell=1000, minimum=5000)
    assert fares.base_fare(1) == 5000
    assert fares.base_fare(2) == 5000
    assert fares.base_fare(3) == 6000


# Default rates and surge applied in whole paise.
def test_fare_with_surge():
    fares = FareCalculator()
    assert fares.base_fare(0) == 5000
    assert fares.fare(10, 15000) == 22500
    assert fares.fare(10, 10000) == 15000


# Surge below 1.0x and negative inputs rejected.
def test_fare_validation():
    fares = FareCalculator()
    with pytest.raises(ValidationError):
        fares.fare(1, 9999)
    with pytest.raises(ValidationError):
        fares.base_fare(-1)
    with pytest.raises(ValidationError):
        FareCalculator(base=-1)


# --- Time of day ---

# Peak boundaries: start inclusive, end exclusive, next day repeats.
@pytest.mark.parametrize("second,expected", [
    (28799, 10000), (28800, 15000), (35999, 15000), (36000, 10000),
    (61199, 10000), (61200, 15000), (71999, 15000), (72000, 10000),
    (86400 + 28800, 15000),
])
def test_time_of_day_edges(second, expected):
    assert TimeOfDayFactor().bps_at(second) == expected


# Bad peak ranges rejected.
@pytest.mark.parametrize("peaks", [((600, 480),), ((-1, 10),), ((0, 1441),)])
def test_time_of_day_validation(peaks):
    with pytest.raises(ValidationError):
        TimeOfDayFactor(peaks)


# --- Zones ---

# Zone boundaries incl. negatives; supply counts only available drivers in zone.
def test_zone_map():
    zones = ZoneMap(10)
    assert zones.zone_of(Point(9, 9)) == (0, 0)
    assert zones.zone_of(Point(10, 0)) == (1, 0)
    assert zones.zone_of(Point(-1, 0)) == (-1, 0)
    views = [DriverView("a", Point(1, 1), DriverStatus.AVAILABLE, 1, DriverStats()),
             DriverView("b", Point(2, 2), DriverStatus.BUSY, 1, DriverStats()),
             DriverView("c", Point(10, 1), DriverStatus.AVAILABLE, 1, DriverStats())]
    assert zones.supply(views, (0, 0)) == 1


# --- Demand tracker / forecast ---

# Window boundary: 299 still window 0, 300 starts window 1.
def test_tracker_window_edge():
    clock = FixedClock(0)
    tracker = DemandTracker(clock)
    clock.advance(299)
    tracker.record_request(SYS, ZONE)
    clock.advance(1)
    tracker.record_request(SYS, ZONE)
    assert tracker.count(ZONE, 0) == 1
    assert tracker.count(ZONE, 1) == 1


# Old windows pruned: window 0 kept at window 3, dropped at window 4.
def test_tracker_prune_edge():
    clock = FixedClock(0)
    tracker = DemandTracker(clock)
    tracker.record_request(SYS, ZONE)
    clock.advance(900)
    tracker.record_request(SYS, ZONE)
    assert tracker.count(ZONE, 0) == 1
    clock.advance(300)
    tracker.record_request(SYS, ZONE)
    assert tracker.count(ZONE, 0) == 0


# Only dispatcher records demand.
def test_tracker_who():
    with pytest.raises(NotAllowedError):
        DemandTracker(FixedClock(0)).record_request(CUST, ZONE)


# Forecast = floor average of last 3 finished windows, current excluded.
def test_forecast_average():
    clock = FixedClock(300)
    tracker = DemandTracker(clock)
    for _ in range(3):
        tracker.record_request(SYS, ZONE)
    clock.advance(600)
    tracker.record_request(SYS, ZONE)
    clock.advance(300)
    tracker.record_request(SYS, ZONE)
    tracker.record_request(SYS, ZONE)
    assert DemandForecaster(tracker).forecast(ZONE) == 1


# Concurrency: parallel requests to one zone are all counted.
def test_tracker_concurrent_counts():
    tracker = DemandTracker(FixedClock(0))
    threads = [threading.Thread(target=lambda: [tracker.record_request(SYS, ZONE) for _ in range(500)])
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
        assert not t.is_alive()
    assert tracker.count(ZONE, 0) == 4000


# --- Surge ---

# No demand, off-peak -> exactly 1.0x.
def test_surge_calm():
    _, surge, _, _ = stack(FixedClock(0))
    assert surge.surge_bps(ZONE, 5) == 10000


# Live demand 2 vs supply 1 off-peak -> (50*20000 + 20*10000 + 30*10000)/100.
def test_surge_live_demand():
    tracker, surge, _, _ = stack(FixedClock(0))
    tracker.record_request(SYS, ZONE)
    tracker.record_request(SYS, ZONE)
    assert surge.surge_bps(ZONE, 1) == 15000


# All three factors maxed -> 27000 with default weights, never above cap.
def test_surge_all_high():
    clock = FixedClock(28800 - 600)
    tracker, surge, _, _ = stack(clock)
    for _ in range(3):
        for _ in range(9):
            tracker.record_request(SYS, ZONE)
        clock.advance(300)
    tracker.record_request(SYS, ZONE)
    assert surge.surge_bps(ZONE, 0) == 27000


# Surge weights must sum to 100.
def test_surge_weights_edges():
    for bad in ((50, 20, 29), (50, 20, 31), (-1, 51, 50)):
        with pytest.raises(ValidationError):
            SurgeWeights(*bad)


# --- ETA ---

# Off-peak 1 min/cell, peak x1.5, driver factor, round-up edge, zero distance.
def test_eta():
    clock = FixedClock(0)
    _, _, eta, _ = stack(clock)
    assert eta.eta_seconds(0) == 0
    assert eta.eta_seconds(5) == 300
    assert eta.eta_seconds(1, 12000) == 72
    assert eta.eta_seconds(1, 10001) == 61
    clock.advance(28800)
    assert eta.eta_seconds(5) == 450


# Negative distance and zero driver factor rejected.
def test_eta_validation():
    _, _, eta, _ = stack(FixedClock(0))
    with pytest.raises(ValidationError):
        eta.eta_seconds(-1)
    with pytest.raises(ValidationError):
        eta.eta_seconds(1, 0)


# --- Quote ---

# Quote carries fare, surge, ETA and expiry; valid at 119s, expired at 120s.
def test_quote_and_expiry_edge():
    clock = FixedClock(0)
    _, _, _, quoter = stack(clock)
    q = quoter.quote(CUST, Point(0, 0), Point(3, 4), supply=1)
    assert (q.fare_paise, q.surge_bps, q.eta_seconds, q.expires_at) == (11400, 10000, 420, 120)
    clock.advance(119)
    assert quoter.is_valid(CUST, q) is True
    clock.advance(1)
    assert quoter.is_valid(CUST, q) is False


# Only customers get quotes; only the owner can use one.
def test_quote_who():
    _, _, _, quoter = stack(FixedClock(0))
    with pytest.raises(NotAllowedError):
        quoter.quote(DRV, Point(0, 0), Point(1, 1), 1)
    q = quoter.quote(CUST, Point(0, 0), Point(1, 1), 1)
    with pytest.raises(NotAllowedError):
        quoter.is_valid(OTHER_CUST, q)


# Printed quote hides the customer id.
def test_quote_repr_hides_customer():
    _, _, _, quoter = stack(FixedClock(0))
    q = quoter.quote(Caller("secret-cust", Role.CUSTOMER), Point(0, 0), Point(1, 1), 1)
    assert "secret-cust" not in repr(q)
