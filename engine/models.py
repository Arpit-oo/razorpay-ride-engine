
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Set

from engine.core import InvalidTransitionError, NotAllowedError, Point, ValidationError, is_int


# Kinds of callers the engine knows about; SYSTEM is the dispatcher itself.
class Role(Enum):
    CUSTOMER = "customer"
    DRIVER = "driver"
    SYSTEM = "system"


# Identity of whoever is invoking an operation.
@dataclass(frozen=True)
class Caller:
    user_id: str
    role: Role

    # Require a non-empty id and a known role.
    def __post_init__(self) -> None:
        if not isinstance(self.user_id, str) or not self.user_id:
            raise ValidationError()
        if not isinstance(self.role, Role):
            raise ValidationError()


# Raise unless caller has the given role and, if given, the given user id.
def require(caller: Caller, role: Role, user_id: Optional[str] = None) -> None:
    if caller.role is not role:
        raise NotAllowedError()
    if user_id is not None and caller.user_id != user_id:
        raise NotAllowedError()


# A rider who books rides.
class Customer:
    # Store id and name; name is private data.
    def __init__(self, customer_id: str, name: str) -> None:
        if not isinstance(customer_id, str) or not customer_id:
            raise ValidationError()
        if not isinstance(name, str) or not name:
            raise ValidationError()
        self.id = customer_id
        self._name = name

    # Only the customer themself may read their name.
    def name(self, caller: Caller) -> str:
        require(caller, Role.CUSTOMER, self.id)
        return self._name

    # Print id only; name stays hidden.
    def __repr__(self) -> str:
        return f"Customer(id={self.id})"


# Availability of a driver.
class DriverStatus(Enum):
    AVAILABLE = "available"
    RESERVED = "reserved"
    BUSY = "busy"
    OFFLINE = "offline"


# A driver with a vehicle of fixed seat count.
class Driver:
    # Start OFFLINE at a location with at least one seat.
    def __init__(self, driver_id: str, name: str, location: Point, seats: int) -> None:
        if not isinstance(driver_id, str) or not driver_id:
            raise ValidationError()
        if not isinstance(name, str) or not name:
            raise ValidationError()
        if not isinstance(location, Point):
            raise ValidationError()
        if not is_int(seats) or seats < 1:
            raise ValidationError()
        self.id = driver_id
        self._name = name
        self.location = location
        self.seats = seats
        self.status = DriverStatus.OFFLINE

    # Only the driver themself may report a new location.
    def move(self, caller: Caller, location: Point) -> None:
        require(caller, Role.DRIVER, self.id)
        if not isinstance(location, Point):
            raise ValidationError()
        self.location = location

    # Only the driver themself may read their name.
    def name(self, caller: Caller) -> str:
        require(caller, Role.DRIVER, self.id)
        return self._name

    # Change status only if currently in one of the expected states.
    def _change_status(self, expected: Set[DriverStatus], new_status: DriverStatus) -> None:
        if self.status not in expected:
            raise InvalidTransitionError()
        self.status = new_status

    # Driver starts taking rides.
    def go_online(self, caller: Caller) -> None:
        require(caller, Role.DRIVER, self.id)
        self._change_status({DriverStatus.OFFLINE}, DriverStatus.AVAILABLE)

    # Driver stops taking rides; not allowed while reserved or on a ride.
    def go_offline(self, caller: Caller) -> None:
        require(caller, Role.DRIVER, self.id)
        self._change_status({DriverStatus.AVAILABLE}, DriverStatus.OFFLINE)

    # Dispatcher holds this driver for an offer.
    def reserve(self, caller: Caller) -> None:
        require(caller, Role.SYSTEM)
        self._change_status({DriverStatus.AVAILABLE}, DriverStatus.RESERVED)

    # Dispatcher marks reserved driver as on a ride.
    def mark_busy(self, caller: Caller) -> None:
        require(caller, Role.SYSTEM)
        self._change_status({DriverStatus.RESERVED}, DriverStatus.BUSY)

    # Dispatcher frees a reserved or busy driver.
    def release(self, caller: Caller) -> None:
        require(caller, Role.SYSTEM)
        self._change_status({DriverStatus.RESERVED, DriverStatus.BUSY}, DriverStatus.AVAILABLE)

    # Print id and status only; name and location stay hidden.
    def __repr__(self) -> str:
        return f"Driver(id={self.id}, status={self.status.value})"


# Lowest and highest star rating a customer may give.
MIN_STARS = 1
MAX_STARS = 5


# Raise unless stars is an int from MIN_STARS to MAX_STARS.
def validate_stars(stars: int) -> None:
    if not is_int(stars) or not MIN_STARS <= stars <= MAX_STARS:
        raise ValidationError()


# Running counters of a driver's history used for ranking.
@dataclass
class DriverStats:
    accepted: int = 0
    rejected: int = 0
    completed: int = 0
    rating_sum: int = 0
    rating_count: int = 0
    trip_predicted: int = 0
    trip_actual: int = 0

    # Add one trip's predicted and actual seconds, used for the driver speed factor.
    def record_trip(self, predicted: int, actual: int) -> None:
        for value in (predicted, actual):
            if not is_int(value) or value < 0:
                raise ValidationError()
        self.trip_predicted += predicted
        self.trip_actual += actual

    # Count one accepted offer.
    def record_accept(self) -> None:
        self.accepted += 1

    # Count one rejected offer.
    def record_reject(self) -> None:
        self.rejected += 1

    # Count one completed ride.
    def record_complete(self) -> None:
        self.completed += 1

    # Add one star rating in the allowed range.
    def add_rating(self, stars: int) -> None:
        validate_stars(stars)
        self.rating_sum += stars
        self.rating_count += 1

    # Independent copy for reading outside a lock.
    def copy(self) -> "DriverStats":
        return DriverStats(self.accepted, self.rejected, self.completed, self.rating_sum, self.rating_count,
                           self.trip_predicted, self.trip_actual)


# Lifecycle states of a ride.
class RideStatus(Enum):
    REQUESTED = "requested"
    OFFERED = "offered"
    ACCEPTED = "accepted"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    NO_DRIVER_FOUND = "no_driver_found"


# Legal next states for each ride state; terminal states have none.
ALLOWED_TRANSITIONS = {
    RideStatus.REQUESTED: {RideStatus.OFFERED, RideStatus.NO_DRIVER_FOUND, RideStatus.CANCELLED},
    RideStatus.OFFERED: {RideStatus.ACCEPTED, RideStatus.REQUESTED, RideStatus.CANCELLED},
    RideStatus.ACCEPTED: {RideStatus.IN_PROGRESS, RideStatus.CANCELLED},
    RideStatus.IN_PROGRESS: {RideStatus.COMPLETED, RideStatus.CANCELLED},
    RideStatus.COMPLETED: set(),
    RideStatus.CANCELLED: set(),
    RideStatus.NO_DRIVER_FOUND: set(),
}


# One ride request and its progress; no locking here (registry adds it later).
@dataclass
class Ride:
    id: str
    customer_id: str
    pickup: Point
    dropoff: Point
    status: RideStatus = RideStatus.REQUESTED
    driver_id: Optional[str] = None
    rejected_driver_ids: Set[str] = field(default_factory=set)
    fare_paise: int = 0
    offered_at: Optional[int] = None
    started_at: Optional[int] = None
    predicted_trip_seconds: int = 0
    rated: bool = False
    pooled: bool = False

    # Require ids, real points, and distinct pickup/dropoff.
    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ValidationError()
        if not isinstance(self.customer_id, str) or not self.customer_id:
            raise ValidationError()
        if not isinstance(self.pickup, Point) or not isinstance(self.dropoff, Point):
            raise ValidationError()
        if self.pickup == self.dropoff:
            raise ValidationError()

    # Move to a new state if the transition table allows it.
    def _move_to(self, new_status: RideStatus) -> None:
        if new_status not in ALLOWED_TRANSITIONS[self.status]:
            raise InvalidTransitionError()
        self.status = new_status

    # Only the driver currently attached to this ride passes; no driver attached means nobody does.
    def require_assigned_driver(self, caller: Caller) -> None:
        if self.driver_id is None:
            raise NotAllowedError()
        require(caller, Role.DRIVER, self.driver_id)

    # Dispatcher offers the ride to a driver who has not rejected it.
    def offer(self, caller: Caller, driver_id: str) -> None:
        require(caller, Role.SYSTEM)
        if not isinstance(driver_id, str) or not driver_id:
            raise ValidationError()
        if driver_id in self.rejected_driver_ids:
            raise NotAllowedError()
        self._move_to(RideStatus.OFFERED)
        self.driver_id = driver_id

    # Offered driver accepts.
    def accept(self, caller: Caller) -> None:
        self.require_assigned_driver(caller)
        self._move_to(RideStatus.ACCEPTED)

    # Offered driver rejects; ride goes back to REQUESTED and that driver is excluded.
    def reject(self, caller: Caller) -> None:
        self.require_assigned_driver(caller)
        self._move_to(RideStatus.REQUESTED)
        self.rejected_driver_ids.add(caller.user_id)
        self.driver_id = None

    # Dispatcher withdraws an unanswered offer; counts as that driver rejecting.
    def expire_offer(self, caller: Caller) -> str:
        require(caller, Role.SYSTEM)
        driver_id = self.driver_id
        self._move_to(RideStatus.REQUESTED)
        self.rejected_driver_ids.add(driver_id)
        self.driver_id = None
        return driver_id

    # Ride's customer rates once, only after completion.
    def mark_rated(self, caller: Caller) -> None:
        require(caller, Role.CUSTOMER, self.customer_id)
        if self.status is not RideStatus.COMPLETED or self.rated:
            raise InvalidTransitionError()
        self.rated = True

    # Dispatcher marks that no driver could be found.
    def mark_no_driver(self, caller: Caller) -> None:
        require(caller, Role.SYSTEM)
        self._move_to(RideStatus.NO_DRIVER_FOUND)

    # Assigned driver starts the trip after pickup.
    def start(self, caller: Caller) -> None:
        self.require_assigned_driver(caller)
        self._move_to(RideStatus.IN_PROGRESS)

    # Assigned driver completes the trip.
    def complete(self, caller: Caller) -> None:
        self.require_assigned_driver(caller)
        self._move_to(RideStatus.COMPLETED)

    # Ride's own customer cancels (always free) any time before completion.
    def cancel(self, caller: Caller) -> None:
        require(caller, Role.CUSTOMER, self.customer_id)
        self._move_to(RideStatus.CANCELLED)

    # Print id and status only; customer, driver and locations stay hidden.
    def __repr__(self) -> str:
        return f"Ride(id={self.id}, status={self.status.value})"
