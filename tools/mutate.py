# Manual mutation check: plants one small bug at a time in a copy of the engine and confirms the fast tests catch it.
import pathlib
import shutil
import subprocess
import sys
import tempfile

SRC = pathlib.Path(__file__).resolve().parents[1]

# (file, original text, mutated text, description)
MUTANTS = [
    ("core.py", "abs(a.x - b.x) + abs(a.y - b.y)", "abs(a.x - b.x)", "distance ignores y"),
    ("models.py", "if user_id is not None and caller.user_id != user_id", "if False", "identity check off"),
    ("models.py", "RideStatus.IN_PROGRESS: {RideStatus.COMPLETED, RideStatus.CANCELLED}",
     "RideStatus.IN_PROGRESS: {RideStatus.COMPLETED}", "no cancel mid-trip"),
    ("registry.py", "                return False\n            return True", "                return True\n            return True",
     "claim always wins"),
    ("matching.py", "if distance <= self.max_radius:\n                found", "if distance < self.max_radius:\n                found",
     "radius edge excluded"),
    ("matching.py", "scored.sort(key=lambda s: (-s[0], s[1], s[2]))", "scored.sort(key=lambda s: (s[0], s[1], s[2]))",
     "ranking reversed"),
    ("matching.py", "return actual * 100 <= solo * (100 + detour_pct)", "return actual * 100 < solo * (100 + detour_pct)",
     "detour edge excluded"),
    ("matching.py", "len(pool.riders) >= pool.seats", "len(pool.riders) > pool.seats", "one seat too many"),
    ("pricing.py", "return max(BPS_ONE, min(BPS_MAX, bps))", "return max(BPS_ONE, bps)", "no surge cap"),
    ("pricing.py", "if start <= minute < end:", "if start <= minute <= end:", "peak end inclusive"),
    ("pricing.py", "return self._clock.now() < quote.expires_at", "return self._clock.now() <= quote.expires_at",
     "quote valid 1s too long"),
    ("pricing.py", "return solo_fare_paise * (100 - discount_pct) // 100", "return solo_fare_paise * 100 // 100",
     "no pool discount"),
    ("rides.py", "self._clock.now() >= ride.offered_at", "self._clock.now() > ride.offered_at", "offer timeout late"),
    ("rides.py", "for _ in range(self.max_attempts):", "for _ in range(self.max_attempts - 1):", "one retry short"),
    ("rides.py", "if slot.active_ride_id is not None:", "if False:", "double booking allowed"),
    ("rides.py", "            if self._pools.leave(SYSTEM, driver_id, ride.id):\n                self._drivers.release(SYSTEM, driver_id)\n            return",
     "            self._pools.leave(SYSTEM, driver_id, ride.id)\n            self._drivers.release(SYSTEM, driver_id)\n            return",
     "pool driver freed too early"),
]


# Copy project, apply one mutant, run fast tests; True if tests failed (mutant killed).
def run(file, old, new):
    with tempfile.TemporaryDirectory() as tmp:
        dst = pathlib.Path(tmp) / "proj"
        shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "tools"))
        target = dst / "engine" / file
        text = target.read_text(encoding="utf-8")
        assert text.count(old) == 1, (file, old)
        target.write_text(text.replace(old, new), encoding="utf-8")
        result = subprocess.run([sys.executable, "-m", "pytest", "-x", "-q", "-m", "not slow", "-p", "no:cacheprovider"],
                                cwd=dst, capture_output=True, text=True)
        return result.returncode != 0


killed = 0
for file, old, new, desc in MUTANTS:
    dead = run(file, old, new)
    killed += dead
    print(f"{'KILLED ' if dead else 'SURVIVED'}  {file:12} {desc}")
print(f"\n{killed}/{len(MUTANTS)} mutants killed")
