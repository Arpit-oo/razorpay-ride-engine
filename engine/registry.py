from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Dict, List, TypeVar

from engine.core import InvalidTransitionError, NotFoundError, Point, ValidationError
from engine.models import Caller, Driver, DriverStats, DriverStatus, Ride, Role, require

T = TypeVar("T")


# Read-only copy of one driver's state, safe to use outside any lock.
@dataclass(frozen=True)
class DriverView:
    driver_id: str
    location: Point
    status: DriverStatus
    seats: int
    stats: DriverStats


# One driver plus its own lock and history.
class _DriverEntry:
    # Bundle driver, stats and a dedicated lock.
    def __init__(self, driver: Driver) -> None:
        self.driver = driver
        self.stats = DriverStats()
        self.lock = threading.Lock()


# Holds all drivers; each driver has its own lock, the index lock only guards the dict.
class DriverRegistry:
    # Start empty.
    def __init__(self) -> None:
        self._entries: Dict[str, _DriverEntry] = {}
        self._index_lock = threading.Lock()

    # Driver signs themself up; duplicate ids rejected.
    def register(self, caller: Caller, driver: Driver) -> None:
        if not isinstance(driver, Driver):
            raise ValidationError()
        require(caller, Role.DRIVER, driver.id)
        with self._index_lock:
            if driver.id in self._entries:
                raise ValidationError()
            self._entries[driver.id] = _DriverEntry(driver)

    # Find one entry; index lock held only for the lookup.
    def _entry(self, driver_id: str) -> _DriverEntry:
        with self._index_lock:
            entry = self._entries.get(driver_id)
        if entry is None:
            raise NotFoundError()
        return entry

    # Driver reports new location under that driver's lock.
    def update_location(self, caller: Caller, driver_id: str, location: Point) -> None:
        entry = self._entry(driver_id)
        with entry.lock:
            entry.driver.move(caller, location)

    # Driver goes online.
    def go_online(self, caller: Caller, driver_id: str) -> None:
        entry = self._entry(driver_id)
        with entry.lock:
            entry.driver.go_online(caller)

    # Driver goes offline (only when available).
    def go_offline(self, caller: Caller, driver_id: str) -> None:
        entry = self._entry(driver_id)
        with entry.lock:
            entry.driver.go_offline(caller)

    # Atomically reserve an available driver; False if someone else got there first.
    def try_claim(self, caller: Caller, driver_id: str) -> bool:
        require(caller, Role.SYSTEM)
        entry = self._entry(driver_id)
        with entry.lock:
            try:
                entry.driver.reserve(caller)
            except InvalidTransitionError:
                return False
            return True

    # Reserved driver starts the ride.
    def mark_busy(self, caller: Caller, driver_id: str) -> None:
        entry = self._entry(driver_id)
        with entry.lock:
            entry.driver.mark_busy(caller)

    # Reserved or busy driver becomes available again.
    def release(self, caller: Caller, driver_id: str) -> None:
        entry = self._entry(driver_id)
        with entry.lock:
            entry.driver.release(caller)

    # Apply one stats update under the driver's lock; dispatcher only.
    def _update_stats(self, caller: Caller, driver_id: str, update: Callable[[DriverStats], None]) -> None:
        require(caller, Role.SYSTEM)
        entry = self._entry(driver_id)
        with entry.lock:
            update(entry.stats)

    # Record an accepted offer.
    def record_accept(self, caller: Caller, driver_id: str) -> None:
        self._update_stats(caller, driver_id, DriverStats.record_accept)

    # Record a rejected offer.
    def record_reject(self, caller: Caller, driver_id: str) -> None:
        self._update_stats(caller, driver_id, DriverStats.record_reject)

    # Record a completed ride.
    def record_complete(self, caller: Caller, driver_id: str) -> None:
        self._update_stats(caller, driver_id, DriverStats.record_complete)

    # Record a customer's star rating.
    def record_rating(self, caller: Caller, driver_id: str, stars: int) -> None:
        self._update_stats(caller, driver_id, lambda s: s.add_rating(stars))

    # Record one trip's predicted vs actual seconds.
    def record_trip(self, caller: Caller, driver_id: str, predicted: int, actual: int) -> None:
        self._update_stats(caller, driver_id, lambda s: s.record_trip(predicted, actual))

    # Copy one driver's state; dispatcher only.
    def view(self, caller: Caller, driver_id: str) -> DriverView:
        require(caller, Role.SYSTEM)
        entry = self._entry(driver_id)
        with entry.lock:
            d = entry.driver
            return DriverView(d.id, d.location, d.status, d.seats, entry.stats.copy())

    # Copy every driver's state, locking one driver at a time (never two at once).
    def snapshot(self, caller: Caller) -> List[DriverView]:
        require(caller, Role.SYSTEM)
        with self._index_lock:
            entries = list(self._entries.values())
        views = []
        for entry in entries:
            with entry.lock:
                d = entry.driver
                views.append(DriverView(d.id, d.location, d.status, d.seats, entry.stats.copy()))
        return views


# Holds all rides; each ride has its own lock.
class RideRegistry:
    # Start empty.
    def __init__(self) -> None:
        self._rides: Dict[str, Ride] = {}
        self._locks: Dict[str, threading.Lock] = {}
        self._index_lock = threading.Lock()

    # Dispatcher stores a new ride; duplicate ids rejected.
    def add(self, caller: Caller, ride: Ride) -> None:
        require(caller, Role.SYSTEM)
        if not isinstance(ride, Ride):
            raise ValidationError()
        with self._index_lock:
            if ride.id in self._rides:
                raise ValidationError()
            self._rides[ride.id] = ride
            self._locks[ride.id] = threading.Lock()

    # Run an action on one ride while holding only that ride's lock; keep actions fast.
    def apply(self, ride_id: str, action: Callable[[Ride], T]) -> T:
        with self._index_lock:
            ride = self._rides.get(ride_id)
            lock = self._locks.get(ride_id)
        if ride is None or lock is None:
            raise NotFoundError()
        with lock:
            return action(ride)
