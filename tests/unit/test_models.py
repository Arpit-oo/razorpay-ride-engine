import pytest

from engine.core import InvalidTransitionError, NotAllowedError, Point, ValidationError
from engine.models import (
    ALLOWED_TRANSITIONS,
    Caller,
    Customer,
    Driver,
    DriverStatus,
    Ride,
    RideStatus,
    Role,
)

# Reusable callers: owner customer c1, stranger c2, driver d1, stranger d2, dispatcher.
CUST = Caller("c1", Role.CUSTOMER)
OTHER_CUST = Caller("c2", Role.CUSTOMER)
DRV = Caller("d1", Role.DRIVER)
OTHER_DRV = Caller("d2", Role.DRIVER)
SYS = Caller("sys", Role.SYSTEM)


# Fresh ride for customer c1.
def new_ride() -> Ride:
    return Ride("r1", "c1", Point(0, 0), Point(5, 5))


# Drive a new ride into the given state using legal steps.
def ride_in(status: RideStatus) -> Ride:
    ride = new_ride()
    steps = {
        RideStatus.REQUESTED: [],
        RideStatus.OFFERED: [lambda r: r.offer(SYS, "d1")],
        RideStatus.ACCEPTED: [lambda r: r.offer(SYS, "d1"), lambda r: r.accept(DRV)],
        RideStatus.IN_PROGRESS: [lambda r: r.offer(SYS, "d1"), lambda r: r.accept(DRV), lambda r: r.start(DRV)],
        RideStatus.COMPLETED: [lambda r: r.offer(SYS, "d1"), lambda r: r.accept(DRV), lambda r: r.start(DRV),
                               lambda r: r.complete(DRV)],
        RideStatus.CANCELLED: [lambda r: r.cancel(CUST)],
        RideStatus.NO_DRIVER_FOUND: [lambda r: r.mark_no_driver(SYS)],
    }
    for step in steps[status]:
        step(ride)
    assert ride.status is status
    return ride


# Perform the action that targets a given status, using the right caller.
def act(ride: Ride, target: RideStatus) -> None:
    actions = {
        RideStatus.OFFERED: lambda: ride.offer(SYS, "d1"),
        RideStatus.ACCEPTED: lambda: ride.accept(DRV),
        RideStatus.REQUESTED: lambda: ride.reject(DRV),
        RideStatus.IN_PROGRESS: lambda: ride.start(DRV),
        RideStatus.COMPLETED: lambda: ride.complete(DRV),
        RideStatus.CANCELLED: lambda: ride.cancel(CUST),
        RideStatus.NO_DRIVER_FOUND: lambda: ride.mark_no_driver(SYS),
    }
    actions[target]()


# --- Caller ---

# Empty id or non-Role rejected.
@pytest.mark.parametrize("uid,role", [("", Role.CUSTOMER), (None, Role.DRIVER), ("x", "driver")])
def test_caller_validation(uid, role):
    with pytest.raises(ValidationError):
        Caller(uid, role)


# --- Customer ---

# Customer reads own name; others cannot.
def test_customer_name_access():
    c = Customer("c1", "Asha")
    assert c.name(CUST) == "Asha"
    for bad in (OTHER_CUST, DRV, SYS):
        with pytest.raises(NotAllowedError):
            c.name(bad)


# Printed customer hides name.
def test_customer_repr_hides_name():
    assert "Asha" not in repr(Customer("c1", "Asha"))


# Empty fields rejected.
@pytest.mark.parametrize("cid,name", [("", "A"), ("c1", ""), (None, "A")])
def test_customer_validation(cid, name):
    with pytest.raises(ValidationError):
        Customer(cid, name)


# --- Driver ---

# New driver starts offline with given seats.
def test_driver_defaults():
    d = Driver("d1", "Ravi", Point(1, 1), 3)
    assert d.status is DriverStatus.OFFLINE
    assert d.seats == 3


# Seats edge: 1 allowed, 0 rejected; non-int rejected.
def test_driver_seats_edges():
    assert Driver("d1", "Ravi", Point(0, 0), 1).seats == 1
    for bad in (0, -1, True, 2.0):
        with pytest.raises(ValidationError):
            Driver("d1", "Ravi", Point(0, 0), bad)


# Other bad constructor inputs rejected.
@pytest.mark.parametrize("did,name,loc", [("", "R", Point(0, 0)), ("d1", "", Point(0, 0)), ("d1", "R", (0, 0))])
def test_driver_validation(did, name, loc):
    with pytest.raises(ValidationError):
        Driver(did, name, loc, 1)


# Only the driver can move themself; non-point rejected.
def test_driver_move_access():
    d = Driver("d1", "Ravi", Point(0, 0), 2)
    d.move(DRV, Point(2, 3))
    assert d.location == Point(2, 3)
    for bad in (OTHER_DRV, CUST, SYS):
        with pytest.raises(NotAllowedError):
            d.move(bad, Point(9, 9))
    with pytest.raises(ValidationError):
        d.move(DRV, (1, 1))
    assert d.location == Point(2, 3)


# Only the driver reads their own name.
def test_driver_name_access():
    d = Driver("d1", "Ravi", Point(0, 0), 2)
    assert d.name(DRV) == "Ravi"
    with pytest.raises(NotAllowedError):
        d.name(OTHER_DRV)


# Printed driver hides name and location.
def test_driver_repr_hides_sensitive():
    text = repr(Driver("d1", "Ravi", Point(77, 88), 2))
    assert "Ravi" not in text and "77" not in text and "88" not in text


# --- Ride construction ---

# Pickup equal to dropoff rejected; one cell apart allowed.
def test_ride_pickup_dropoff_edges():
    with pytest.raises(ValidationError):
        Ride("r1", "c1", Point(1, 1), Point(1, 1))
    assert Ride("r1", "c1", Point(1, 1), Point(1, 2)).status is RideStatus.REQUESTED


# Bad ids or points rejected.
@pytest.mark.parametrize("rid,cid,p,d", [("", "c1", Point(0, 0), Point(1, 1)),
                                         ("r1", "", Point(0, 0), Point(1, 1)),
                                         ("r1", "c1", (0, 0), Point(1, 1))])
def test_ride_validation(rid, cid, p, d):
    with pytest.raises(ValidationError):
        Ride(rid, cid, p, d)


# Printed ride hides customer, driver and locations.
def test_ride_repr_hides_sensitive():
    ride = Ride("r1", "cust-secret", Point(111, 222), Point(333, 444))
    ride.offer(SYS, "drv-secret")
    text = repr(ride)
    for secret in ("cust-secret", "drv-secret", "111", "222", "333", "444"):
        assert secret not in text


# --- Ride transitions: every (from, to) pair ---

# Every combination of source and target status.
ALL = list(RideStatus)
PAIRS = [(a, b) for a in ALL for b in ALL]


# Legal pairs succeed; every other pair raises and leaves state unchanged.
@pytest.mark.parametrize("src,dst", PAIRS, ids=[f"{a.value}->{b.value}" for a, b in PAIRS])
def test_every_transition(src, dst):
    ride = ride_in(src)
    if dst in ALLOWED_TRANSITIONS[src]:
        act(ride, dst)
        assert ride.status is dst
    else:
        with pytest.raises((InvalidTransitionError, NotAllowedError)):
            act(ride, dst)
        assert ride.status is src


# --- Ride: who may act ---

# Only SYSTEM offers.
def test_offer_requires_system():
    for bad in (CUST, DRV):
        ride = new_ride()
        with pytest.raises(NotAllowedError):
            ride.offer(bad, "d1")
        assert ride.status is RideStatus.REQUESTED


# Only the offered driver can accept, reject, start, complete.
@pytest.mark.parametrize("method", ["accept", "reject"])
def test_offered_driver_only(method):
    ride = ride_in(RideStatus.OFFERED)
    for bad in (OTHER_DRV, CUST, SYS):
        with pytest.raises(NotAllowedError):
            getattr(ride, method)(bad)
    assert ride.status is RideStatus.OFFERED


# Start and complete limited to assigned driver.
def test_start_complete_assigned_driver_only():
    ride = ride_in(RideStatus.ACCEPTED)
    with pytest.raises(NotAllowedError):
        ride.start(OTHER_DRV)
    ride.start(DRV)
    with pytest.raises(NotAllowedError):
        ride.complete(OTHER_DRV)
    assert ride.status is RideStatus.IN_PROGRESS


# With no driver attached, no driver may accept (guards against None matching anyone).
def test_no_driver_attached_rejects_all_drivers():
    ride = new_ride()
    for method in ("accept", "reject", "start", "complete"):
        with pytest.raises(NotAllowedError):
            getattr(ride, method)(DRV)


# Only the ride's own customer can cancel.
def test_cancel_own_customer_only():
    ride = ride_in(RideStatus.ACCEPTED)
    for bad in (OTHER_CUST, DRV, SYS):
        with pytest.raises(NotAllowedError):
            ride.cancel(bad)
    ride.cancel(CUST)
    assert ride.status is RideStatus.CANCELLED


# Only SYSTEM marks no-driver.
def test_mark_no_driver_requires_system():
    ride = new_ride()
    with pytest.raises(NotAllowedError):
        ride.mark_no_driver(CUST)


# --- Ride: reject behaviour ---

# Reject clears driver, records exclusion, and blocks re-offering to same driver.
def test_reject_excludes_driver():
    ride = ride_in(RideStatus.OFFERED)
    ride.reject(DRV)
    assert ride.status is RideStatus.REQUESTED
    assert ride.driver_id is None
    assert "d1" in ride.rejected_driver_ids
    with pytest.raises(NotAllowedError):
        ride.offer(SYS, "d1")
    ride.offer(SYS, "d2")
    assert ride.driver_id == "d2"


# Offer with empty driver id rejected.
def test_offer_empty_driver_id():
    with pytest.raises(ValidationError):
        new_ride().offer(SYS, "")
