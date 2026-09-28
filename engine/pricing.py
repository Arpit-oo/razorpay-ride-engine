
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Tuple

from engine.core import Clock, IdGenerator, Point, ValidationError, is_int, manhattan
from engine.models import Caller, DriverStats, DriverStatus, Role, require
from engine.registry import DriverView

# 1.0x surge in basis points.
BPS_ONE = 10000
# Surge ceiling: 3.0x.
BPS_MAX = 30000
# Seconds in a day and in a minute.
DAY_SECONDS = 86400
MINUTE = 60

Zone = Tuple[int, int]


# Raise unless value is an int at or above the given minimum.
def _require_int(value: object, minimum: int) -> None:
    if not is_int(value) or value < minimum:
        raise ValidationError()


# Keep a surge value inside 1.0x .. 3.0x.
def clamp_bps(bps: int) -> int:
    return max(BPS_ONE, min(BPS_MAX, bps))


# Integer division rounded up.
def _ceil_div(a: int, b: int) -> int:
    return -(-a // b)


# Splits the grid into square zones of fixed size.
class ZoneMap:
    # Zone edge length in cells, at least 1.
    def __init__(self, size: int) -> None:
        _require_int(size, 1)
        self.size = size

    # Zone containing a point (floor division keeps negatives consistent).
    def zone_of(self, point: Point) -> Zone:
        return (point.x // self.size, point.y // self.size)

    # Number of available drivers in a zone.
    def supply(self, views: Iterable[DriverView], zone: Zone) -> int:
        return sum(1 for v in views if v.status is DriverStatus.AVAILABLE and self.zone_of(v.location) == zone)


# Fare before surge: base plus per-cell rate, never below the minimum.
class FareCalculator:
    # All amounts in paise.
    def __init__(self, base: int = 3000, per_cell: int = 1200, minimum: int = 5000) -> None:
        for amount in (base, per_cell, minimum):
            _require_int(amount, 0)
        self.base = base
        self.per_cell = per_cell
        self.minimum = minimum

    # Unsurged fare for a trip distance.
    def base_fare(self, distance: int) -> int:
        _require_int(distance, 0)
        return max(self.minimum, self.base + self.per_cell * distance)

    # Surged fare, rounded down to whole paise.
    def fare(self, distance: int, surge_bps: int) -> int:
        _require_int(surge_bps, BPS_ONE)
        return self.base_fare(distance) * surge_bps // BPS_ONE


# Multiplier that applies inside given minute-of-day ranges (start inclusive, end exclusive).
class TimeOfDayFactor:
    # Default peaks 08:00-10:00 and 17:00-20:00 at 1.5x.
    def __init__(self, peaks: Tuple[Tuple[int, int], ...] = ((480, 600), (1020, 1200)),
                 peak_bps: int = 15000) -> None:
        for start, end in peaks:
            if not (is_int(start) and is_int(end) and 0 <= start < end <= 1440):
                raise ValidationError()
        _require_int(peak_bps, BPS_ONE)
        self.peaks = peaks
        self.peak_bps = peak_bps

    # Multiplier for a clock time; clock second 0 is local midnight.
    def bps_at(self, now: int) -> int:
        minute = (now % DAY_SECONDS) // MINUTE
        for start, end in self.peaks:
            if start <= minute < end:
                return self.peak_bps
        return BPS_ONE


# Counts ride requests per zone in fixed clock windows; one lock per zone.
class DemandTracker:
    # Window length in seconds and how many past windows to keep.
    def __init__(self, clock: Clock, window_seconds: int = 300, keep_windows: int = 3) -> None:
        _require_int(window_seconds, 1)
        _require_int(keep_windows, 1)
        self._clock = clock
        self.window_seconds = window_seconds
        self.keep_windows = keep_windows
        self._counts: Dict[Zone, Dict[int, int]] = {}
        self._locks: Dict[Zone, threading.Lock] = {}
        self._index_lock = threading.Lock()

    # Index of the window the clock is in now.
    def current_window(self) -> int:
        return self._clock.now() // self.window_seconds

    # Get or create a zone's lock and counter map; index lock held only for the dict.
    def _zone(self, zone: Zone) -> Tuple[threading.Lock, Dict[int, int]]:
        with self._index_lock:
            if zone not in self._locks:
                self._locks[zone] = threading.Lock()
                self._counts[zone] = {}
            return self._locks[zone], self._counts[zone]

    # Dispatcher records one request; windows older than kept history are dropped.
    def record_request(self, caller: Caller, zone: Zone) -> None:
        require(caller, Role.SYSTEM)
        window = self.current_window()
        lock, counts = self._zone(zone)
        with lock:
            counts[window] = counts.get(window, 0) + 1
            for old in [w for w in counts if w < window - self.keep_windows]:
                del counts[old]

    # Requests seen in a zone during a given window.
    def count(self, zone: Zone, window: int) -> int:
        lock, counts = self._zone(zone)
        with lock:
            return counts.get(window, 0)


# Predicts next-window demand as the average of recent finished windows.
class DemandForecaster:
    # Uses the tracker's kept history.
    def __init__(self, tracker: DemandTracker) -> None:
        self._tracker = tracker

    # Floor average of the last N finished windows (current window excluded).
    def forecast(self, zone: Zone) -> int:
        n = self._tracker.keep_windows
        now = self._tracker.current_window()
        total = sum(self._tracker.count(zone, now - i) for i in range(1, n + 1))
        return total // n


# Surge weights in percent; must sum to 100.
@dataclass(frozen=True)
class SurgeWeights:
    demand: int = 50
    time_of_day: int = 20
    forecast: int = 30

    # Non-negative ints summing to 100.
    def __post_init__(self) -> None:
        parts = (self.demand, self.time_of_day, self.forecast)
        if any(not is_int(p) or p < 0 for p in parts) or sum(parts) != 100:
            raise ValidationError()


# Demand-to-supply ratio as bps, clamped; no drivers with demand means max surge.
def ratio_bps(demand: int, supply: int) -> int:
    if supply == 0:
        return BPS_MAX if demand > 0 else BPS_ONE
    return clamp_bps(demand * BPS_ONE // supply)


# Combines live demand/supply, time of day and forecast into one surge value.
class SurgeCalculator:
    # Collaborators are injected so each piece is tested alone.
    def __init__(self, tracker: DemandTracker, forecaster: DemandForecaster,
                 time_of_day: TimeOfDayFactor, clock: Clock, weights: SurgeWeights = SurgeWeights()) -> None:
        self._tracker = tracker
        self._forecaster = forecaster
        self._time_of_day = time_of_day
        self._clock = clock
        self.weights = weights

    # Weighted surge for a zone given current available drivers there.
    def surge_bps(self, zone: Zone, supply: int) -> int:
        _require_int(supply, 0)
        w = self.weights
        live = ratio_bps(self._tracker.count(zone, self._tracker.current_window()), supply)
        future = ratio_bps(self._forecaster.forecast(zone), supply)
        tod = self._time_of_day.bps_at(self._clock.now())
        return clamp_bps((w.demand * live + w.time_of_day * tod + w.forecast * future) // 100)


# Slowest and fastest driver factor allowed: 0.5x .. 2.0x.
DRIVER_FACTOR_MIN = 5000
DRIVER_FACTOR_MAX = 20000


# Driver speed factor from history: actual / predicted trip time, neutral 1.0x with no trips.
def driver_factor_bps(stats: DriverStats) -> int:
    if stats.trip_predicted == 0:
        return BPS_ONE
    raw = stats.trip_actual * BPS_ONE // stats.trip_predicted
    return max(DRIVER_FACTOR_MIN, min(DRIVER_FACTOR_MAX, raw))


# Travel time: cells x seconds per cell x traffic x driver factor, rounded up.
class EtaPredictor:
    # Default 1 minute per cell; traffic slows trips at peak.
    def __init__(self, traffic: TimeOfDayFactor, clock: Clock, seconds_per_cell: int = 60) -> None:
        _require_int(seconds_per_cell, 1)
        self._traffic = traffic
        self._clock = clock
        self.seconds_per_cell = seconds_per_cell

    # Seconds to cover a distance now; driver factor 10000 = average driver.
    def eta_seconds(self, distance: int, driver_factor_bps: int = BPS_ONE) -> int:
        _require_int(distance, 0)
        _require_int(driver_factor_bps, 1)
        traffic = self._traffic.bps_at(self._clock.now())
        raw = distance * self.seconds_per_cell * traffic * driver_factor_bps
        return _ceil_div(raw, BPS_ONE * BPS_ONE)


# Price and ETA promised to one customer until it expires.
@dataclass(frozen=True, repr=False)
class Quote:
    customer_id: str
    pickup: Point
    dropoff: Point
    fare_paise: int
    surge_bps: int
    eta_seconds: int
    expires_at: int
    quote_id: str = ""

    # Print numbers only; customer id stays hidden.
    def __repr__(self) -> str:
        return f"Quote(fare_paise={self.fare_paise}, surge_bps={self.surge_bps})"


# Builds and checks quotes.
class Quoter:
    # Quote valid for ttl seconds (default 120).
    def __init__(self, fares: FareCalculator, surge: SurgeCalculator, eta: EtaPredictor,
                 zones: ZoneMap, clock: Clock, ttl_seconds: int = 120,
                 ids: Optional[IdGenerator] = None) -> None:
        _require_int(ttl_seconds, 1)
        self._fares = fares
        self._surge = surge
        self._eta = eta
        self._zones = zones
        self._clock = clock
        self.ttl_seconds = ttl_seconds
        self._ids = ids or IdGenerator("quote")

    # Customer asks for a price; surge is taken from the pickup zone.
    def quote(self, caller: Caller, pickup: Point, dropoff: Point, supply: int) -> Quote:
        require(caller, Role.CUSTOMER)
        distance = manhattan(pickup, dropoff)
        surge = self._surge.surge_bps(self._zones.zone_of(pickup), supply)
        return Quote(
            customer_id=caller.user_id,
            pickup=pickup,
            dropoff=dropoff,
            fare_paise=self._fares.fare(distance, surge),
            surge_bps=surge,
            eta_seconds=self._eta.eta_seconds(distance),
            expires_at=self._clock.now() + self.ttl_seconds,
            quote_id=self._ids.next_id(),
        )

    # Quote usable only by its own customer and only before it expires.
    def is_valid(self, caller: Caller, quote: Quote) -> bool:
        require(caller, Role.CUSTOMER, quote.customer_id)
        return self._clock.now() < quote.expires_at


# Default discount a pooled rider gets off their solo fare, in percent.
POOL_DISCOUNT_PCT = 25


# Pooled rider pays their own solo fare minus the discount, rounded down to paise.
def pooled_fare(solo_fare_paise: int, discount_pct: int = POOL_DISCOUNT_PCT) -> int:
    _require_int(solo_fare_paise, 0)
    if not is_int(discount_pct) or not 0 <= discount_pct <= 100:
        raise ValidationError()
    return solo_fare_paise * (100 - discount_pct) // 100
