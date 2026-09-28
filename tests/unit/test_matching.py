import pytest

from engine.core import Point, ValidationError
from engine.matching import (
    Candidate,
    DriverRanker,
    NearestDriverFinder,
    RankingWeights,
    performance_score,
    proximity_score,
    rating_score,
)
from engine.models import DriverStats, DriverStatus
from engine.registry import DriverView


# Build a driver view at (x, 0) with optional status and stats.
def view(driver_id, x, status=DriverStatus.AVAILABLE, stats=None):
    return DriverView(driver_id, Point(x, 0), status, 2, stats or DriverStats())


# --- NearestDriverFinder ---

# Radius edge: exactly at radius included, one past excluded.
def test_finder_radius_edge():
    finder = NearestDriverFinder(5)
    found = finder.find([view("at", 5), view("past", 6)], Point(0, 0))
    assert [c.view.driver_id for c in found] == ["at"]


# Sorted nearest first; equal distance broken by id.
def test_finder_order_and_ties():
    finder = NearestDriverFinder(10)
    found = finder.find([view("b", 2), view("a", 2), view("c", 1)], Point(0, 0))
    assert [c.view.driver_id for c in found] == ["c", "a", "b"]


# Non-available and excluded drivers skipped; empty input gives empty list.
def test_finder_filters():
    finder = NearestDriverFinder(10)
    views = [view("busy", 1, DriverStatus.BUSY), view("off", 1, DriverStatus.OFFLINE),
             view("res", 1, DriverStatus.RESERVED), view("no", 1), view("ok", 2)]
    assert [c.view.driver_id for c in finder.find(views, Point(0, 0), {"no"})] == ["ok"]
    assert finder.find([], Point(0, 0)) == []


# Radius 0 allowed (same cell only); negative rejected.
def test_finder_radius_bounds():
    assert len(NearestDriverFinder(0).find([view("a", 0), view("b", 1)], Point(0, 0))) == 1
    with pytest.raises(ValidationError):
        NearestDriverFinder(-1)


# --- Sub-scores ---

# New driver gets neutral performance and rating.
def test_new_driver_neutral():
    assert performance_score(DriverStats()) == 50
    assert rating_score(DriverStats()) == 50


# Perfect record -> 100; all rejects -> acceptance 0, completion neutral.
def test_performance_extremes():
    assert performance_score(DriverStats(accepted=4, completed=4)) == 100
    assert performance_score(DriverStats(rejected=3)) == 25


# Rating edges: all 1-star -> 0, all 5-star -> 100, mix 3 -> 50.
def test_rating_score_edges():
    assert rating_score(DriverStats(rating_sum=2, rating_count=2)) == 0
    assert rating_score(DriverStats(rating_sum=10, rating_count=2)) == 100
    assert rating_score(DriverStats(rating_sum=3, rating_count=1)) == 50


# Proximity: 0 -> 100, at radius -> 0, radius 0 -> 100.
def test_proximity_edges():
    assert proximity_score(0, 10) == 100
    assert proximity_score(10, 10) == 0
    assert proximity_score(5, 10) == 50
    assert proximity_score(0, 0) == 100


# --- Weights ---

# Must sum to exactly 100: 99 and 101 rejected; negative rejected.
def test_weights_sum_edges():
    RankingWeights(40, 30, 30)
    for bad in ((40, 30, 29), (40, 30, 31), (-1, 51, 50), (True, 49, 50)):
        with pytest.raises(ValidationError):
            RankingWeights(*bad)


# --- DriverRanker ---

# All three metrics matter: a slightly farther driver with better history wins.
def test_ranker_uses_all_three():
    ranker = DriverRanker(RankingWeights(), 10)
    good = DriverStats(accepted=5, completed=5, rating_sum=25, rating_count=5)
    bad = DriverStats(accepted=1, rejected=4, completed=1, rating_sum=5, rating_count=5)
    cands = [Candidate(view("near_bad", 1, stats=bad), 1), Candidate(view("far_good", 3, stats=good), 3)]
    assert [r.driver_id for r in ranker.rank(cands)] == ["far_good", "near_bad"]


# Same history: nearer driver wins.
def test_ranker_proximity_breaks_equal_history():
    ranker = DriverRanker(RankingWeights(), 10)
    cands = [Candidate(view("far", 4), 4), Candidate(view("near", 2), 2)]
    assert [r.driver_id for r in ranker.rank(cands)] == ["near", "far"]


# Exact score tie and same distance: id order decides.
def test_ranker_tie_by_id():
    ranker = DriverRanker(RankingWeights(), 10)
    cands = [Candidate(view("b", 2), 2), Candidate(view("a", 2), 2)]
    ranked = ranker.rank(cands)
    assert ranked[0].score == ranked[1].score
    assert [r.driver_id for r in ranked] == ["a", "b"]


# Max score is 10000 for a perfect driver at the pickup.
def test_ranker_max_score():
    ranker = DriverRanker(RankingWeights(), 10)
    perfect = DriverStats(accepted=1, completed=1, rating_sum=5, rating_count=1)
    assert ranker.rank([Candidate(view("p", 0, stats=perfect), 0)])[0].score == 10000


# Bad constructor inputs rejected.
def test_ranker_validation():
    with pytest.raises(ValidationError):
        DriverRanker((40, 30, 30), 10)
    with pytest.raises(ValidationError):
        DriverRanker(RankingWeights(), -1)
