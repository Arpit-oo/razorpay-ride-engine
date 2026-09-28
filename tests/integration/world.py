from engine.core import FixedClock, IdGenerator, Point
from engine.matching import DriverRanker, NearestDriverFinder, PoolMatcher, RankingWeights, RoutePlanner
from engine.models import Caller, Driver, Role
from engine.pricing import (
    DemandForecaster,
    DemandTracker,
    EtaPredictor,
    FareCalculator,
    Quoter,
    SurgeCalculator,
    TimeOfDayFactor,
    ZoneMap,
)
from engine.registry import DriverRegistry, RideRegistry
from engine.rides import SYSTEM, FakeGateway, PaymentService, RideService

# Reusable callers.
CUST = Caller("c1", Role.CUSTOMER)
OTHER_CUST = Caller("c2", Role.CUSTOMER)
D1 = Caller("d1", Role.DRIVER)
D2 = Caller("d2", Role.DRIVER)


# Everything wired together; by default d1 at 1 cell and d2 at 3 cells from pickup (0, 0).
class World:
    # Build with an optional scripted gateway and optional extra drivers as (id, x) pairs.
    def __init__(self, script=None, drivers=(("d1", 1), ("d2", 3))):
        self.clock = FixedClock(0)
        self.drivers = DriverRegistry()
        for driver_id, x in drivers:
            caller = Caller(driver_id, Role.DRIVER)
            self.drivers.register(caller, Driver(driver_id, "N", Point(x, 0), 2))
            self.drivers.go_online(caller, driver_id)
        tracker = DemandTracker(self.clock)
        tod = TimeOfDayFactor()
        surge = SurgeCalculator(tracker, DemandForecaster(tracker), tod, self.clock)
        self.eta = EtaPredictor(tod, self.clock)
        zones = ZoneMap(10)
        self.quoter = Quoter(FareCalculator(), surge, self.eta, zones, self.clock)
        self.gateway = FakeGateway(script)
        self.rides = RideRegistry()
        self.pools = PoolMatcher(RoutePlanner())
        self.service = RideService(
            self.drivers, self.rides, NearestDriverFinder(10), DriverRanker(RankingWeights(), 10),
            self.quoter, zones, tracker, self.eta, PaymentService(self.gateway), self.clock, IdGenerator("ride"),
            pools=self.pools)

    # Quote and book a trip (default (0,0) -> (5,0)) for a customer.
    def book(self, caller=CUST, pickup=(0, 0), dropoff=(5, 0), pooled=False):
        quote = self.quoter.quote(caller, Point(*pickup), Point(*dropoff), supply=2)
        return self.service.request_ride(caller, quote, pooled=pooled)

    # Current status of a driver.
    def driver_status(self, driver_id):
        return self.drivers.view(SYSTEM, driver_id).status

    # Current status of a ride as seen by its customer.
    def status(self, ride_id, caller=CUST):
        return self.service.ride_status(caller, ride_id)

    # Driver currently attached to a ride (test-only peek).
    def driver_of(self, ride_id):
        return self.rides.apply(ride_id, lambda r: r.driver_id)
