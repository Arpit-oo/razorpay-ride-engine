from __future__ import annotations

import threading
from enum import Enum
from typing import Dict, List, Optional, Protocol, Set

from engine.core import (
    Clock,
    ExpiredError,
    IdGenerator,
    InvalidTransitionError,
    NotAllowedError,
    Point,
    ValidationError,
    is_int,
    manhattan,
)
from engine.matching import DriverRanker, NearestDriverFinder, PoolMatcher, PoolRider
from engine.models import Caller, Ride, RideStatus, Role, require, validate_stars
from engine.pricing import (
    DemandTracker,
    EtaPredictor,
    Quote,
    Quoter,
    ZoneMap,
    driver_factor_bps,
    pooled_fare,
)
from engine.registry import DriverRegistry, RideRegistry

# The engine's own identity for dispatcher-only operations.
SYSTEM = Caller("system", Role.SYSTEM)


# Where a ride's payment stands; separate from the ride's own status.
class PaymentStatus(Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    PAID = "paid"
    PAYMENT_FAILED = "payment_failed"


# External payment provider; must treat a repeated key as the same charge.
class PaymentGateway(Protocol):
    # Charge amount once per key; True on success.
    def charge(self, idempotency_key: str, amount_paise: int) -> bool: ...


# Scripted stand-in gateway: each call takes the next outcome (True, False, "lost", or an exception).
class FakeGateway:
    # Empty script means every call succeeds.
    def __init__(self, script: Optional[List[object]] = None) -> None:
        self._script = list(script or [])
        self.calls = 0
        self.charged: Dict[str, int] = {}
        self._lock = threading.Lock()

    # Dedupe by key; "lost" charges the money but fails to reply.
    def charge(self, idempotency_key: str, amount_paise: int) -> bool:
        with self._lock:
            self.calls += 1
            if idempotency_key in self.charged:
                return True
            outcome = self._script.pop(0) if self._script else True
            if outcome == "lost":
                self.charged[idempotency_key] = amount_paise
                raise TimeoutError()
            if isinstance(outcome, Exception):
                raise outcome
            if outcome is True:
                self.charged[idempotency_key] = amount_paise
            return outcome is True


# Charges rides with bounded retries; one lock per ride, gateway called outside locks.
class PaymentService:
    # Try at most max_attempts times per pay call.
    def __init__(self, gateway: PaymentGateway, max_attempts: int = 3) -> None:
        if not is_int(max_attempts) or max_attempts < 1:
            raise ValidationError()
        self._gateway = gateway
        self.max_attempts = max_attempts
        self._status: Dict[str, PaymentStatus] = {}
        self._locks: Dict[str, threading.Lock] = {}
        self._index_lock = threading.Lock()

    # Get or create the lock for one ride's payment.
    def _lock_for(self, ride_id: str) -> threading.Lock:
        with self._index_lock:
            return self._locks.setdefault(ride_id, threading.Lock())

    # Current payment status of a ride.
    def status(self, ride_id: str) -> PaymentStatus:
        with self._lock_for(ride_id):
            return self._status.get(ride_id, PaymentStatus.PENDING)

    # Charge a ride; a second concurrent call sees PROCESSING and does not charge again.
    def pay(self, caller: Caller, ride_id: str, amount_paise: int) -> PaymentStatus:
        require(caller, Role.SYSTEM)
        if not is_int(amount_paise) or amount_paise < 0:
            raise ValidationError()
        lock = self._lock_for(ride_id)
        with lock:
            current = self._status.get(ride_id, PaymentStatus.PENDING)
            if current in (PaymentStatus.PAID, PaymentStatus.PROCESSING):
                return current
            self._status[ride_id] = PaymentStatus.PROCESSING
        paid = self._attempt(f"pay-{ride_id}", amount_paise)
        with lock:
            self._status[ride_id] = PaymentStatus.PAID if paid else PaymentStatus.PAYMENT_FAILED
            return self._status[ride_id]

    # Call the gateway up to max_attempts times; any gateway error counts as a failed try.
    def _attempt(self, key: str, amount_paise: int) -> bool:
        for _ in range(self.max_attempts):
            try:
                if self._gateway.charge(key, amount_paise) is True:
                    return True
            except Exception:  # gateway errors are not trusted or exposed
                continue
        return False


# One customer's lock and their current active ride, if any.
class _CustomerSlot:
    # Start with no active ride.
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active_ride_id: Optional[str] = None
        self.used_quotes: Dict[str, int] = {}


# Runs the booking flow; lock order is always ride lock outer, driver/customer lock inner.
class RideService:
    # All collaborators injected.
    def __init__(self, drivers: DriverRegistry, rides: RideRegistry, finder: NearestDriverFinder,
                 ranker: DriverRanker, quoter: Quoter, zones: ZoneMap, tracker: DemandTracker,
                 eta: EtaPredictor, payments: PaymentService, clock: Clock, ids: IdGenerator,
                 offer_timeout_seconds: int = 30, pools: Optional[PoolMatcher] = None) -> None:
        self._drivers = drivers
        self._rides = rides
        self._finder = finder
        self._ranker = ranker
        self._quoter = quoter
        self._zones = zones
        self._tracker = tracker
        self._eta = eta
        self._payments = payments
        self._clock = clock
        self._ids = ids
        self.offer_timeout_seconds = offer_timeout_seconds
        self._pools = pools
        self._customers: Dict[str, _CustomerSlot] = {}
        self._index_lock = threading.Lock()

    # Get or create a customer's slot.
    def _slot(self, customer_id: str) -> _CustomerSlot:
        with self._index_lock:
            return self._customers.setdefault(customer_id, _CustomerSlot())

    # Forget a customer's active ride if it is this one.
    def _clear_active(self, customer_id: str, ride_id: str) -> None:
        slot = self._slot(customer_id)
        with slot.lock:
            if slot.active_ride_id == ride_id:
                slot.active_ride_id = None

    # --- customer actions ---

    # Price a trip; supply is counted by the engine so customers cannot influence surge.
    def get_quote(self, caller: Caller, pickup: Point, dropoff: Point) -> Quote:
        require(caller, Role.CUSTOMER)
        views = self._drivers.snapshot(SYSTEM)
        supply = self._zones.supply(views, self._zones.zone_of(pickup))
        return self._quoter.quote(caller, pickup, dropoff, supply)

    # Book using a valid quote; one active ride per customer; pooled rides get the pool discount.
    def request_ride(self, caller: Caller, quote: Quote, pooled: bool = False) -> str:
        require(caller, Role.CUSTOMER)
        if pooled and self._pools is None:
            raise ValidationError()
        if not self._quoter.is_valid(caller, quote):
            raise ExpiredError()
        slot = self._slot(caller.user_id)
        with slot.lock:
            if slot.active_ride_id is not None:
                raise NotAllowedError()
            # Each quote books at most one ride; forget used quotes once they have expired anyway.
            now = self._clock.now()
            slot.used_quotes = {q: exp for q, exp in slot.used_quotes.items() if exp > now}
            if quote.quote_id in slot.used_quotes:
                raise NotAllowedError()
            fare = pooled_fare(quote.fare_paise) if pooled else quote.fare_paise
            ride = Ride(self._ids.next_id(), caller.user_id, quote.pickup, quote.dropoff,
                        fare_paise=fare, pooled=pooled)
            self._rides.add(SYSTEM, ride)
            slot.active_ride_id = ride.id
            slot.used_quotes[quote.quote_id] = quote.expires_at
        self._tracker.record_request(SYSTEM, self._zones.zone_of(quote.pickup))
        self._dispatch(ride.id)
        return ride.id

    # Customer checks status; a stale offer is withdrawn and re-dispatched here.
    def ride_status(self, caller: Caller, ride_id: str) -> RideStatus:
        expired = self._rides.apply(ride_id, lambda r: self._check_stale_locked(caller, r))
        if expired:
            self._dispatch(ride_id)
        return self._rides.apply(ride_id, lambda r: r.status)

    # Customer cancels (free) any time before completion; attached driver is freed.
    def cancel(self, caller: Caller, ride_id: str) -> None:
        self._rides.apply(ride_id, lambda r: self._cancel_locked(caller, r))

    # Customer rates the driver once after completion.
    def rate(self, caller: Caller, ride_id: str, stars: int) -> None:
        validate_stars(stars)
        self._rides.apply(ride_id, lambda r: self._rate_locked(caller, r, stars))

    # Customer sees seconds until the accepted driver arrives.
    def pickup_eta(self, caller: Caller, ride_id: str) -> int:
        return self._rides.apply(ride_id, lambda r: self._pickup_eta_locked(caller, r))

    # Customer sees payment status of their ride.
    def payment_status(self, caller: Caller, ride_id: str) -> PaymentStatus:
        self._rides.apply(ride_id, lambda r: require(caller, Role.CUSTOMER, r.customer_id))
        return self._payments.status(ride_id)

    # Customer retries a failed payment on a completed ride.
    def retry_payment(self, caller: Caller, ride_id: str) -> PaymentStatus:
        fare = self._rides.apply(ride_id, lambda r: self._completed_fare_locked(caller, r))
        return self._payments.pay(SYSTEM, ride_id, fare)

    # --- driver actions ---

    # Offered driver accepts; an offer past its timeout is withdrawn instead.
    def accept(self, caller: Caller, ride_id: str) -> None:
        expired = self._rides.apply(ride_id, lambda r: self._accept_locked(caller, r))
        if expired:
            self._dispatch(ride_id)
            raise ExpiredError()

    # Offered driver rejects; ride goes to the next-ranked driver.
    def reject(self, caller: Caller, ride_id: str) -> None:
        self._rides.apply(ride_id, lambda r: self._reject_locked(caller, r))
        self._dispatch(ride_id)

    # Assigned driver starts the trip; baseline trip time is predicted now.
    def start(self, caller: Caller, ride_id: str) -> None:
        self._rides.apply(ride_id, lambda r: self._start_locked(caller, r))

    # Assigned driver completes; driver freed, then payment taken outside all locks.
    def complete(self, caller: Caller, ride_id: str) -> PaymentStatus:
        fare = self._rides.apply(ride_id, lambda r: self._complete_locked(caller, r))
        return self._payments.pay(SYSTEM, ride_id, fare)

    # --- dispatch ---

    # Offer the ride to the best claimable driver, or mark no driver found.
    def _dispatch(self, ride_id: str) -> None:
        state = self._rides.apply(ride_id, lambda r: (r.status, r.pickup, r.dropoff, r.pooled,
                                                      set(r.rejected_driver_ids)))
        status, pickup, dropoff, pooled, excluded = state
        if status is not RideStatus.REQUESTED:
            return
        if pooled and self._dispatch_to_pool(ride_id, PoolRider(ride_id, pickup, dropoff), excluded):
            return
        views = self._drivers.snapshot(SYSTEM)
        ranked = self._ranker.rank(self._finder.find(views, pickup, excluded))
        for candidate in ranked:
            if not self._drivers.try_claim(SYSTEM, candidate.driver_id):
                continue
            self._rides.apply(ride_id, lambda r, d=candidate.driver_id: self._offer_locked(r, d))
            return
        self._rides.apply(ride_id, self._no_driver_locked)

    # Try open pools nearest first (within radius); True if the ride was offered to a pool driver.
    def _dispatch_to_pool(self, ride_id: str, rider: PoolRider, excluded: Set[str]) -> bool:
        nearby = []
        for driver_id in self._pools.open_driver_ids(SYSTEM):
            if driver_id in excluded:
                continue
            location = self._drivers.view(SYSTEM, driver_id).location
            distance = manhattan(location, rider.pickup)
            if distance <= self._finder.max_radius:
                nearby.append((distance, driver_id, location))
        for _, driver_id, location in sorted(nearby, key=lambda n: (n[0], n[1])):
            if self._pools.try_join(SYSTEM, driver_id, rider, start=location):
                self._rides.apply(ride_id, lambda r, d=driver_id: self._offer_locked(r, d))
                return True
        return False

    # Free a ride's driver: a pooled rider leaves the pool; the driver is released only once it empties.
    def _free_driver(self, ride: Ride, driver_id: str) -> None:
        if ride.pooled and self._pools.has_pool(SYSTEM, driver_id):
            if self._pools.leave(SYSTEM, driver_id, ride.id):
                self._drivers.release(SYSTEM, driver_id)
            return
        self._drivers.release(SYSTEM, driver_id)

    # --- work done while holding the ride lock ---

    # Offer to a claimed (or pool) driver, or free the driver if the ride moved on meanwhile.
    def _offer_locked(self, ride: Ride, driver_id: str) -> None:
        if ride.status is not RideStatus.REQUESTED:
            self._free_driver(ride, driver_id)
            return
        ride.offer(SYSTEM, driver_id)
        ride.offered_at = self._clock.now()

    # Mark no driver found if still waiting, and free the customer to book again.
    def _no_driver_locked(self, ride: Ride) -> None:
        if ride.status is RideStatus.REQUESTED:
            ride.mark_no_driver(SYSTEM)
            self._clear_active(ride.customer_id, ride.id)

    # True when the current offer has reached its timeout.
    def _offer_stale(self, ride: Ride) -> bool:
        return (ride.status is RideStatus.OFFERED
                and self._clock.now() >= ride.offered_at + self.offer_timeout_seconds)

    # Withdraw the offer, free the driver and count it as a reject.
    def _expire_locked(self, ride: Ride) -> None:
        driver_id = ride.expire_offer(SYSTEM)
        self._free_driver(ride, driver_id)
        self._drivers.record_reject(SYSTEM, driver_id)

    # Owner check, then expire a stale offer; True if expired.
    def _check_stale_locked(self, caller: Caller, ride: Ride) -> bool:
        require(caller, Role.CUSTOMER, ride.customer_id)
        if self._offer_stale(ride):
            self._expire_locked(ride)
            return True
        return False

    # Accept unless stale; driver becomes busy atomically with the ride. True if expired.
    def _accept_locked(self, caller: Caller, ride: Ride) -> bool:
        ride.require_assigned_driver(caller)
        if self._offer_stale(ride):
            self._expire_locked(ride)
            return True
        ride.accept(caller)
        if not (ride.pooled and self._pools.has_pool(SYSTEM, ride.driver_id)):
            self._drivers.mark_busy(SYSTEM, ride.driver_id)
            if ride.pooled:
                self._open_pool_locked(ride)
        self._drivers.record_accept(SYSTEM, ride.driver_id)
        return False

    # First pooled rider accepted: open a pool at the driver's location with this rider in it.
    def _open_pool_locked(self, ride: Ride) -> None:
        view = self._drivers.view(SYSTEM, ride.driver_id)
        self._pools.open_pool(SYSTEM, ride.driver_id, view.seats, view.location)
        self._pools.try_join(SYSTEM, ride.driver_id, PoolRider(ride.id, ride.pickup, ride.dropoff))

    # Reject, free the driver and count the reject.
    def _reject_locked(self, caller: Caller, ride: Ride) -> None:
        ride.require_assigned_driver(caller)
        driver_id = ride.driver_id
        ride.reject(caller)
        self._free_driver(ride, driver_id)
        self._drivers.record_reject(SYSTEM, driver_id)

    # Start and store baseline (average-driver) trip prediction.
    def _start_locked(self, caller: Caller, ride: Ride) -> None:
        ride.start(caller)
        ride.started_at = self._clock.now()
        ride.predicted_trip_seconds = self._eta.eta_seconds(manhattan(ride.pickup, ride.dropoff))
        if ride.pooled:
            self._pools.mark_onboard(SYSTEM, ride.driver_id, ride.id, ride.pickup)

    # Complete, free driver, update history; returns fare to charge.
    def _complete_locked(self, caller: Caller, ride: Ride) -> int:
        ride.complete(caller)
        self._free_driver(ride, ride.driver_id)
        self._drivers.record_complete(SYSTEM, ride.driver_id)
        if not ride.pooled:
            actual = self._clock.now() - ride.started_at
            self._drivers.record_trip(SYSTEM, ride.driver_id, ride.predicted_trip_seconds, actual)
        self._clear_active(ride.customer_id, ride.id)
        return ride.fare_paise

    # Cancel and free any driver still attached.
    def _cancel_locked(self, caller: Caller, ride: Ride) -> None:
        holds_driver = ride.status in (RideStatus.OFFERED, RideStatus.ACCEPTED, RideStatus.IN_PROGRESS)
        ride.cancel(caller)
        if holds_driver:
            self._free_driver(ride, ride.driver_id)
        self._clear_active(ride.customer_id, ride.id)

    # Mark rated, then add stars to driver's history.
    def _rate_locked(self, caller: Caller, ride: Ride, stars: int) -> None:
        ride.mark_rated(caller)
        self._drivers.record_rating(SYSTEM, ride.driver_id, stars)

    # Driver-to-pickup ETA using the driver's speed factor.
    def _pickup_eta_locked(self, caller: Caller, ride: Ride) -> int:
        require(caller, Role.CUSTOMER, ride.customer_id)
        if ride.status is not RideStatus.ACCEPTED:
            raise InvalidTransitionError()
        view = self._drivers.view(SYSTEM, ride.driver_id)
        distance = manhattan(view.location, ride.pickup)
        if ride.pooled and self._pools.has_pool(SYSTEM, ride.driver_id):
            distance = self._pools.distance_to(SYSTEM, ride.driver_id, ride.id, "pickup")
        return self._eta.eta_seconds(distance, driver_factor_bps(view.stats))

    # Owner check and completed check; returns fare.
    def _completed_fare_locked(self, caller: Caller, ride: Ride) -> int:
        require(caller, Role.CUSTOMER, ride.customer_id)
        if ride.status is not RideStatus.COMPLETED:
            raise InvalidTransitionError()
        return ride.fare_paise
