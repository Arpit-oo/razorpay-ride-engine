import threading

import pytest

from engine.core import EngineError, Point
from engine.models import Caller, DriverStatus, RideStatus, Role
from engine.rides import SYSTEM, FakeGateway, PaymentService, PaymentStatus
from tests.integration.world import World

pytestmark = pytest.mark.slow


# Start all workers together and fail if any is still running after the timeout (deadlock guard).
def run_all(workers, timeout=20):
    barrier = threading.Barrier(len(workers))
    errors = []

    # Wrap a worker so it waits for the others and records unexpected errors.
    def wrap(fn):
        # Waits for the others, then runs the worker.
        def go():
            barrier.wait()
            try:
                fn()
            except EngineError:
                pass
            except Exception as exc:  # anything non-engine is a real bug
                errors.append(exc)
        return go

    threads = [threading.Thread(target=wrap(w)) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=timeout)
        assert not t.is_alive(), "possible deadlock"
    assert errors == []


# 40 customers vs 20 drivers booking, accepting, finishing at once: nobody double-booked, all drivers freed.
def test_many_solo_rides():
    w = World(drivers=[(f"d{i:02d}", i % 10) for i in range(20)])
    completed = []
    lock = threading.Lock()

    # One customer's full journey; driver acts only if the ride was offered.
    def journey(n):
        # Runs this customer's trip.
        def go():
            cust = Caller(f"c{n:02d}", Role.CUSTOMER)
            ride = w.book(cust)
            driver_id = w.driver_of(ride)
            if w.status(ride, cust) is not RideStatus.OFFERED:
                return
            drv = Caller(driver_id, Role.DRIVER)
            w.service.accept(drv, ride)
            w.service.start(drv, ride)
            w.service.complete(drv, ride)
            with lock:
                completed.append(ride)
        return go

    run_all([journey(n) for n in range(40)])
    assert len(completed) >= 20
    assert len(w.gateway.charged) == len(completed)
    for view in w.drivers.snapshot(SYSTEM):
        assert view.status is DriverStatus.AVAILABLE


# Many pooled riders racing into one open pool never exceed its seats (driver has 2 seats).
def test_pool_join_race():
    w = World(drivers=[("d1", 1)])
    first = w.book(Caller("c00", Role.CUSTOMER), (0, 0), (5, 0), pooled=True)
    w.service.accept(Caller("d1", Role.DRIVER), first)
    run_all([lambda n=n: w.book(Caller(f"c{n:02d}", Role.CUSTOMER), (1, 0), (5, 0), pooled=True)
             for n in range(1, 15)])
    riders = {s.rider_id for s in w.pools.route(SYSTEM, "d1").stops}
    assert len(riders) == 2


# Many threads paying the same ride call the gateway once and charge once.
def test_concurrent_pay_same_ride():
    gateway = FakeGateway()
    payments = PaymentService(gateway)
    results = []
    run_all([lambda: results.append(payments.pay(SYSTEM, "ride-1", 5000)) for _ in range(16)])
    assert gateway.calls == 1
    assert len(gateway.charged) == 1
    assert set(results) <= {PaymentStatus.PAID, PaymentStatus.PROCESSING}


# Drivers moving while dispatch snapshots and claims run: no errors, no deadlock.
def test_location_updates_during_dispatch():
    w = World(drivers=[(f"d{i}", i) for i in range(5)])

    # Driver keeps moving.
    def mover(i):
        # Moves this driver 200 times.
        def go():
            for step in range(200):
                w.drivers.update_location(Caller(f"d{i}", Role.DRIVER), f"d{i}", Point(step % 7, i))
        return go

    # Customer books then cancels.
    def booker(n):
        # Books a ride, then cancels it.
        def go():
            cust = Caller(f"c{n}", Role.CUSTOMER)
            ride = w.book(cust)
            w.service.cancel(cust, ride)
        return go

    run_all([mover(i) for i in range(5)] + [booker(n) for n in range(10)])
    for view in w.drivers.snapshot(SYSTEM):
        assert view.status is DriverStatus.AVAILABLE
