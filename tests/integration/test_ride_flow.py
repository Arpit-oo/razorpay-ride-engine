import threading

import pytest

from engine.core import ExpiredError, InvalidTransitionError, NotAllowedError, Point, ValidationError
from engine.models import DriverStatus, RideStatus
from engine.rides import PaymentStatus
from tests.integration.world import CUST, D1, D2, OTHER_CUST, World


# Happy path: nearest equal-history driver gets it, trip completes, payment taken, rating once.
def test_happy_path():
    w = World()
    ride = w.book()
    assert w.status(ride) is RideStatus.OFFERED
    w.service.accept(D1, ride)
    assert w.driver_status("d1") is DriverStatus.BUSY
    w.service.start(D1, ride)
    w.clock.advance(300)
    assert w.service.complete(D1, ride) is PaymentStatus.PAID
    assert w.gateway.charged == {f"pay-{ride}": 9000}
    assert w.driver_status("d1") is DriverStatus.AVAILABLE
    w.service.rate(CUST, ride, 5)
    with pytest.raises(InvalidTransitionError):
        w.service.rate(CUST, ride, 5)


# Reject chain: d1 rejects -> d2 offered -> d2 rejects -> no driver; customer may book again.
def test_reject_chain_to_no_driver():
    w = World()
    ride = w.book()
    w.service.reject(D1, ride)
    w.service.reject(D2, ride)
    assert w.status(ride) is RideStatus.NO_DRIVER_FOUND
    assert w.driver_status("d1") is DriverStatus.AVAILABLE
    assert w.status(w.book()) is RideStatus.OFFERED


# Offer timeout edge: accept at 29s works, at 30s the offer moves to the next driver.
@pytest.mark.parametrize("wait,expired", [(29, False), (30, True)])
def test_offer_timeout_edge(wait, expired):
    w = World()
    ride = w.book()
    w.clock.advance(wait)
    if expired:
        with pytest.raises(ExpiredError):
            w.service.accept(D1, ride)
        w.service.accept(D2, ride)
    else:
        w.service.accept(D1, ride)
    assert w.status(ride) is RideStatus.ACCEPTED


# Customer status check withdraws a stale offer even if the driver never answers.
def test_stale_offer_expired_on_status_check():
    w = World()
    ride = w.book()
    w.clock.advance(30)
    assert w.status(ride) is RideStatus.OFFERED
    assert w.driver_status("d1") is DriverStatus.AVAILABLE
    assert w.driver_status("d2") is DriverStatus.RESERVED


# Cancel after accept frees the driver; cancel after completion is refused.
def test_cancel_frees_driver():
    w = World()
    ride = w.book()
    w.service.accept(D1, ride)
    w.service.cancel(CUST, ride)
    assert w.driver_status("d1") is DriverStatus.AVAILABLE
    ride2 = w.book()
    w.service.accept(D1, ride2)
    w.service.start(D1, ride2)
    w.service.complete(D1, ride2)
    with pytest.raises(InvalidTransitionError):
        w.service.cancel(CUST, ride2)


# Wrong callers rejected; one active ride per customer; expired quote refused.
def test_who_and_guards():
    w = World()
    ride = w.book()
    with pytest.raises(NotAllowedError):
        w.service.accept(D2, ride)
    with pytest.raises(NotAllowedError):
        w.service.cancel(OTHER_CUST, ride)
    with pytest.raises(NotAllowedError):
        w.book()
    quote = w.quoter.quote(OTHER_CUST, Point(0, 0), Point(5, 0), supply=2)
    w.clock.advance(120)
    with pytest.raises(ExpiredError):
        w.service.request_ride(OTHER_CUST, quote)
    with pytest.raises(ValidationError):
        w.service.rate(CUST, ride, 6)


# Payment retry edge: success on attempt 3 is PAID; failing all 3 is PAYMENT_FAILED, retry later works.
@pytest.mark.parametrize("script,expected", [
    ([False, False, True], PaymentStatus.PAID),
    ([False, False, False], PaymentStatus.PAYMENT_FAILED),
])
def test_payment_retry_edge(script, expected):
    w = World(script)
    ride = w.book()
    w.service.accept(D1, ride)
    w.service.start(D1, ride)
    assert w.service.complete(D1, ride) is expected
    assert w.driver_status("d1") is DriverStatus.AVAILABLE
    if expected is PaymentStatus.PAYMENT_FAILED:
        assert w.service.retry_payment(CUST, ride) is PaymentStatus.PAID
    assert w.service.payment_status(CUST, ride) is PaymentStatus.PAID


# Charge succeeds but reply is lost: retry with same key does not charge twice.
def test_payment_lost_reply_no_double_charge():
    w = World(["lost", RuntimeError("card 4111 declined")])
    ride = w.book()
    w.service.accept(D1, ride)
    w.service.start(D1, ride)
    assert w.service.complete(D1, ride) is PaymentStatus.PAID
    assert len(w.gateway.charged) == 1


# A slow driver's history makes their pickup ETA longer.
def test_pickup_eta_uses_driver_history():
    w = World()
    ride = w.book()
    w.service.accept(D1, ride)
    w.service.start(D1, ride)
    w.clock.advance(600)
    w.service.complete(D1, ride)
    ride2 = w.book()
    w.service.accept(D1, ride2)
    assert w.service.pickup_eta(CUST, ride2) == 120


# Concurrency: accept racing cancel always ends cancelled with the driver free.
def test_accept_cancel_race():
    for _ in range(30):
        w = World()
        ride = w.book()
        barrier = threading.Barrier(2)

        # Driver tries to accept; losing the race is fine.
        def accept():
            barrier.wait()
            try:
                w.service.accept(D1, ride)
            except InvalidTransitionError:
                pass

        # Customer cancels at the same moment.
        def cancel():
            barrier.wait()
            w.service.cancel(CUST, ride)

        threads = [threading.Thread(target=accept), threading.Thread(target=cancel)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
            assert not t.is_alive()
        assert w.status(ride) is RideStatus.CANCELLED
        assert w.driver_status("d1") is DriverStatus.AVAILABLE


# Spec rule, checked directly (not via the transition table): cancel mid-trip is allowed and frees the driver.
def test_cancel_during_trip():
    w = World()
    ride = w.book()
    w.service.accept(D1, ride)
    w.service.start(D1, ride)
    w.service.cancel(CUST, ride)
    assert w.status(ride) is RideStatus.CANCELLED
    assert w.driver_status("d1") is DriverStatus.AVAILABLE


# A quote books one ride only: reusing it after cancelling is refused; a fresh quote works.
def test_quote_single_use():
    w = World()
    quote = w.quoter.quote(CUST, Point(0, 0), Point(5, 0), supply=2)
    ride = w.service.request_ride(CUST, quote)
    w.service.cancel(CUST, ride)
    with pytest.raises(NotAllowedError):
        w.service.request_ride(CUST, quote)
    fresh = w.quoter.quote(CUST, Point(0, 0), Point(5, 0), supply=2)
    assert w.status(w.service.request_ride(CUST, fresh)) is RideStatus.OFFERED
