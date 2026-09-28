import threading

import pytest

from engine.core import (
    EngineError,
    FixedClock,
    IdGenerator,
    InvalidTransitionError,
    NotAllowedError,
    NotFoundError,
    Point,
    ValidationError,
    manhattan,
)


# --- Clock ---

# Clock starts where told and advances by exact amounts.
def test_clock_starts_and_advances():
    clock = FixedClock(100)
    clock.advance(5)
    assert clock.now() == 105


# Advancing by zero is the lower edge and allowed.
def test_clock_advance_zero_edge():
    clock = FixedClock(0)
    clock.advance(0)
    assert clock.now() == 0


# One past the lower edge: negative advance rejected.
def test_clock_advance_negative_rejected():
    clock = FixedClock(0)
    with pytest.raises(ValidationError):
        clock.advance(-1)


# Start at 0 is allowed, -1 is not.
def test_clock_start_edges():
    assert FixedClock(0).now() == 0
    with pytest.raises(ValidationError):
        FixedClock(-1)


# Non-int inputs rejected.
@pytest.mark.parametrize("bad", [1.5, "1", True, None])
def test_clock_rejects_non_int(bad):
    with pytest.raises(ValidationError):
        FixedClock(bad)
    with pytest.raises(ValidationError):
        FixedClock(0).advance(bad)


# Concurrent advances never lose an update.
def test_clock_concurrent_advance():
    clock = FixedClock(0)
    threads = [threading.Thread(target=lambda: [clock.advance(1) for _ in range(1000)]) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
        assert not t.is_alive()
    assert clock.now() == 8000


# --- Point / distance ---

# Same point is distance zero.
def test_manhattan_same_point():
    assert manhattan(Point(3, 4), Point(3, 4)) == 0


# Adjacent cell is distance one.
def test_manhattan_adjacent():
    assert manhattan(Point(0, 0), Point(0, 1)) == 1


# Negative coordinates and symmetry.
def test_manhattan_negative_and_symmetric():
    a, b = Point(-2, 3), Point(4, -1)
    assert manhattan(a, b) == 10
    assert manhattan(b, a) == 10


# Non-int coordinates rejected, including bool.
@pytest.mark.parametrize("x,y", [(1.0, 0), (0, "1"), (True, 0), (None, 0)])
def test_point_rejects_non_int(x, y):
    with pytest.raises(ValidationError):
        Point(x, y)


# Printed point never shows coordinates.
def test_point_repr_hides_location():
    text = repr(Point(123, 456))
    assert "123" not in text and "456" not in text


# --- Errors ---

# Every error carries only its fixed safe message, even if given user data.
@pytest.mark.parametrize("cls", [ValidationError, NotAllowedError, InvalidTransitionError, NotFoundError])
def test_errors_have_fixed_messages(cls):
    err = cls()
    assert isinstance(err, EngineError)
    assert str(err) == cls.message


# Passing extra detail is not possible, so nothing sensitive can leak in.
def test_error_rejects_detail_argument():
    with pytest.raises(TypeError):
        ValidationError("secret phone 9999")


# --- Ids ---

# Ids are sequential and prefixed.
def test_id_generator_sequential():
    gen = IdGenerator("ride")
    assert [gen.next_id() for _ in range(3)] == ["ride-1", "ride-2", "ride-3"]


# Empty or non-string prefix rejected.
@pytest.mark.parametrize("bad", ["", None, 5])
def test_id_generator_bad_prefix(bad):
    with pytest.raises(ValidationError):
        IdGenerator(bad)


# Many threads never receive the same id.
def test_id_generator_concurrent_unique():
    gen = IdGenerator("d")
    results = []
    lock = threading.Lock()

    # Each worker grabs 500 ids and appends them.
    def worker():
        local = [gen.next_id() for _ in range(500)]
        with lock:
            results.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
        assert not t.is_alive()
    assert len(results) == 4000
    assert len(set(results)) == 4000
