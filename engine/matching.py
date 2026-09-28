from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set, Tuple

from engine.core import NotFoundError, Point, ValidationError, is_int, manhattan
from engine.models import MAX_STARS, MIN_STARS, Caller, DriverStats, DriverStatus, Role, require
from engine.registry import DriverView

# Every sub-score is on a 0..100 scale.
FULL = 100
# Score given when a driver has no history yet for that metric.
NEUTRAL = 50


# One candidate driver and how far they are from the pickup.
@dataclass(frozen=True)
class Candidate:
    view: DriverView
    distance: int


# Basic matching: available drivers within a radius, nearest first.
class NearestDriverFinder:
    # Radius is the largest allowed pickup distance, in grid cells.
    def __init__(self, max_radius: int) -> None:
        if not is_int(max_radius) or max_radius < 0:
            raise ValidationError()
        self.max_radius = max_radius

    # Keep available, non-excluded drivers within radius; sort by distance then id.
    def find(self, views: Iterable[DriverView], pickup: Point, exclude: Set[str] = frozenset()) -> List[Candidate]:
        found = []
        for view in views:
            if view.status is not DriverStatus.AVAILABLE or view.driver_id in exclude:
                continue
            distance = manhattan(view.location, pickup)
            if distance <= self.max_radius:
                found.append(Candidate(view, distance))
        return sorted(found, key=lambda c: (c.distance, c.view.driver_id))


# How much each metric counts; percentages that must add up to 100.
@dataclass(frozen=True)
class RankingWeights:
    performance: int = 40
    rating: int = 30
    proximity: int = 30

    # All non-negative ints summing to 100.
    def __post_init__(self) -> None:
        parts = (self.performance, self.rating, self.proximity)
        if any(not is_int(p) or p < 0 for p in parts):
            raise ValidationError()
        if sum(parts) != 100:
            raise ValidationError()


# A driver with their final score, best first.
@dataclass(frozen=True)
class RankedDriver:
    driver_id: str
    score: int
    distance: int


# Past performance 0..100: average of acceptance rate and completion rate.
def performance_score(stats: DriverStats) -> int:
    offers = stats.accepted + stats.rejected
    acceptance = stats.accepted * FULL // offers if offers else NEUTRAL
    completion = min(stats.completed, stats.accepted) * FULL // stats.accepted if stats.accepted else NEUTRAL
    return (acceptance + completion) // 2


# Rating 0..100: 1 star -> 0, 5 stars -> 100, neutral when unrated.
def rating_score(stats: DriverStats) -> int:
    if stats.rating_count == 0:
        return NEUTRAL
    span = MAX_STARS - MIN_STARS
    return (stats.rating_sum - MIN_STARS * stats.rating_count) * FULL // (span * stats.rating_count)


# Proximity 0..100: at pickup -> 100, at max radius -> 0.
def proximity_score(distance: int, max_radius: int) -> int:
    if max_radius == 0:
        return FULL
    return max(0, FULL - distance * FULL // max_radius)


# Recommendation: ranks candidates using performance, rating and proximity together.
class DriverRanker:
    # Weights plus the radius used to scale proximity.
    def __init__(self, weights: RankingWeights, max_radius: int) -> None:
        if not isinstance(weights, RankingWeights):
            raise ValidationError()
        if not is_int(max_radius) or max_radius < 0:
            raise ValidationError()
        self.weights = weights
        self.max_radius = max_radius

    # Weighted total of the three sub-scores (0..10000).
    def score(self, candidate: Candidate) -> int:
        w = self.weights
        stats = candidate.view.stats
        return (w.performance * performance_score(stats)
                + w.rating * rating_score(stats)
                + w.proximity * proximity_score(candidate.distance, self.max_radius))

    # Highest score first; ties broken by nearer, then by driver id.
    def rank(self, candidates: Iterable[Candidate]) -> List[RankedDriver]:
        scored: List[Tuple[int, int, str]] = [
            (self.score(c), c.distance, c.view.driver_id) for c in candidates
        ]
        scored.sort(key=lambda s: (-s[0], s[1], s[2]))
        return [RankedDriver(driver_id, score, distance) for score, distance, driver_id in scored]


# Most riders one pooled vehicle may carry, whatever seats the driver reports.
MAX_POOL_SEATS = 4
# Default max extra in-vehicle distance per rider, as percent of their solo trip.
DEFAULT_DETOUR_PCT = 50


# A rider in a pool; onboard riders already sit in the car and only need dropping.
@dataclass(frozen=True, repr=False)
class PoolRider:
    rider_id: str
    pickup: Point
    dropoff: Point
    onboard: bool = False

    # Print id only; locations stay hidden.
    def __repr__(self) -> str:
        return f"PoolRider(id={self.rider_id})"


# One stop on a route: kind is "pickup" or "drop".
@dataclass(frozen=True)
class Stop:
    rider_id: str
    kind: str
    point: Point


# Chosen stop order and its total driving distance.
@dataclass(frozen=True)
class Route:
    stops: Tuple[Stop, ...]
    total: int


# True if a rider's in-vehicle distance is within the allowed detour over their solo distance.
def within_detour(actual: int, solo: int, detour_pct: int) -> bool:
    return actual * 100 <= solo * (100 + detour_pct)


# Finds the shortest stop order where every pickup precedes its drop and no rider exceeds the detour limit.
class RoutePlanner:
    # Detour limit in percent.
    def __init__(self, detour_pct: int = DEFAULT_DETOUR_PCT) -> None:
        if not is_int(detour_pct) or detour_pct < 0:
            raise ValidationError()
        self.detour_pct = detour_pct

    # Best valid route from start, or None if none satisfies every rider; ties broken by stop order.
    # enforce_detour=False is used when replanning riders already accepted, so a route always exists.
    def plan(self, start: Point, riders: List[PoolRider], enforce_detour: bool = True) -> Optional[Route]:
        pending = []
        for r in riders:
            if not r.onboard:
                pending.append(Stop(r.rider_id, "pickup", r.pickup))
            pending.append(Stop(r.rider_id, "drop", r.dropoff))
        best: Optional[Tuple[int, Tuple[Tuple[str, str], ...], Tuple[Stop, ...]]] = None
        for order in self._orders(pending, [], {r.rider_id for r in riders if r.onboard}):
            if enforce_detour and not self._detours_ok(start, order, riders):
                continue
            key = (self._length(start, order), tuple((s.rider_id, s.kind) for s in order), order)
            if best is None or key[:2] < best[:2]:
                best = key
        return None if best is None else Route(best[2], best[0])

    # Every stop order with each pickup before its drop (depth-first).
    def _orders(self, remaining: List[Stop], path: List[Stop], picked: Set[str]):
        if not remaining:
            yield tuple(path)
            return
        for i, stop in enumerate(remaining):
            if stop.kind == "drop" and stop.rider_id not in picked:
                continue
            added = {stop.rider_id} if stop.kind == "pickup" else set()
            yield from self._orders(remaining[:i] + remaining[i + 1:], path + [stop], picked | added)

    # Driving distance from start through all stops.
    @staticmethod
    def _length(start: Point, order: Tuple[Stop, ...]) -> int:
        total, here = 0, start
        for stop in order:
            total += manhattan(here, stop.point)
            here = stop.point
        return total

    # Route distance from start until a given rider's stop of a given kind.
    @staticmethod
    def distance_until(start: Point, stops: Tuple[Stop, ...], rider_id: str, kind: str) -> int:
        travelled, here = 0, start
        for stop in stops:
            travelled += manhattan(here, stop.point)
            here = stop.point
            if stop.rider_id == rider_id and stop.kind == kind:
                return travelled
        raise NotFoundError()

    # Each rider's wait + ride (route distance from start to their drop) stays within the limit
    # of a direct trip for them alone (start -> pickup -> drop, or start -> drop if onboard).
    def _detours_ok(self, start: Point, order: Tuple[Stop, ...], riders: List[PoolRider]) -> bool:
        for rider in riders:
            direct = (manhattan(start, rider.dropoff) if rider.onboard
                      else manhattan(start, rider.pickup) + manhattan(rider.pickup, rider.dropoff))
            travelled = self.distance_until(start, order, rider.rider_id, "drop")
            if not within_detour(travelled, direct, self.detour_pct):
                return False
        return True


# One driver's shared ride: its riders, seat limit, current route and own lock.
class _Pool:
    # Seats capped at MAX_POOL_SEATS.
    def __init__(self, seats: int, start: Point) -> None:
        self.seats = min(seats, MAX_POOL_SEATS)
        self.start = start
        self.riders: Dict[str, PoolRider] = {}
        self.route = Route((), 0)
        self.closed = False
        self.lock = threading.Lock()


# Adds and removes pooled riders per driver; one lock per pool.
class PoolMatcher:
    # Planner decides routes and detour limit.
    def __init__(self, planner: RoutePlanner) -> None:
        self._planner = planner
        self._pools: Dict[str, _Pool] = {}
        self._index_lock = threading.Lock()

    # Dispatcher opens a pool for a driver at their location.
    def open_pool(self, caller: Caller, driver_id: str, seats: int, start: Point) -> None:
        require(caller, Role.SYSTEM)
        if not is_int(seats) or seats < 1:
            raise ValidationError()
        with self._index_lock:
            if driver_id in self._pools:
                raise ValidationError()
            self._pools[driver_id] = _Pool(seats, start)

    # Find a pool; index lock held only for lookup.
    def _pool(self, driver_id: str) -> _Pool:
        with self._index_lock:
            pool = self._pools.get(driver_id)
        if pool is None:
            raise NotFoundError()
        return pool

    # True if the driver has an open pool.
    def has_pool(self, caller: Caller, driver_id: str) -> bool:
        require(caller, Role.SYSTEM)
        with self._index_lock:
            return driver_id in self._pools

    # Drivers with an open pool, sorted by id.
    def open_driver_ids(self, caller: Caller) -> List[str]:
        require(caller, Role.SYSTEM)
        with self._index_lock:
            return sorted(self._pools)

    # Add a rider if a seat is free and a route exists that keeps everyone within the detour limit.
    # start is the driver's current location; omitted means the pool's last known start.
    def try_join(self, caller: Caller, driver_id: str, rider: PoolRider, start: Optional[Point] = None) -> bool:
        require(caller, Role.SYSTEM)
        pool = self._pool(driver_id)
        with pool.lock:
            if pool.closed or len(pool.riders) >= pool.seats or rider.rider_id in pool.riders:
                return False
            origin = start or pool.start
            route = self._planner.plan(origin, list(pool.riders.values()) + [rider])
            if route is None:
                return False
            pool.riders[rider.rider_id] = rider
            pool.start = origin
            pool.route = route
            return True

    # Rider got in the car: now only their drop is left; replan from the driver's location.
    def mark_onboard(self, caller: Caller, driver_id: str, rider_id: str, start: Point) -> None:
        require(caller, Role.SYSTEM)
        pool = self._pool(driver_id)
        with pool.lock:
            rider = pool.riders.get(rider_id)
            if rider is None:
                raise NotFoundError()
            pool.riders[rider_id] = PoolRider(rider_id, rider.pickup, rider.dropoff, onboard=True)
            pool.start = start
            pool.route = self._planner.plan(start, list(pool.riders.values()), enforce_detour=False)

    # Rider leaves (done, cancelled, rejected); replan; close the pool when empty. True if closed now.
    # strict=True raises NotFound when the pool or rider is missing instead of ignoring it.
    def leave(self, caller: Caller, driver_id: str, rider_id: str, strict: bool = False) -> bool:
        require(caller, Role.SYSTEM)
        with self._index_lock:
            pool = self._pools.get(driver_id)
        if pool is None:
            if strict:
                raise NotFoundError()
            return False
        with pool.lock:
            if pool.riders.pop(rider_id, None) is None:
                if strict:
                    raise NotFoundError()
                return False
            remaining = list(pool.riders.values())
            if remaining:
                pool.route = self._planner.plan(pool.start, remaining, enforce_detour=False)
                return False
            pool.route = Route((), 0)
            pool.closed = True
        with self._index_lock:
            if self._pools.get(driver_id) is pool:
                del self._pools[driver_id]
        return True

    # Strict remove: the rider must be in the pool.
    def remove(self, caller: Caller, driver_id: str, rider_id: str) -> None:
        self.leave(caller, driver_id, rider_id, strict=True)

    # Route distance from the pool's start to a rider's pickup or drop.
    def distance_to(self, caller: Caller, driver_id: str, rider_id: str, kind: str) -> int:
        require(caller, Role.SYSTEM)
        pool = self._pool(driver_id)
        with pool.lock:
            return RoutePlanner.distance_until(pool.start, pool.route.stops, rider_id, kind)

    # Copy of the current route.
    def route(self, caller: Caller, driver_id: str) -> Route:
        require(caller, Role.SYSTEM)
        pool = self._pool(driver_id)
        with pool.lock:
            return pool.route
