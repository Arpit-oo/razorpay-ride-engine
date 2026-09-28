
# Goal
 Ride sharing matching pricing engine.Python, pytest, in-memory, no database, no other libraries.

# Requirements
- two users driver and customer
- customers can book, rate, cancel rides,
-  drivers can accept, complete rides, and reject rides
- drives are ranked based on the past performence  proximity and rating
- basic matching algorithm (nearest driver) 
-optimised routing for shared rides (pooling) considering multiple pickups/drop-offs and minimizing detours
-driver recommendation system based on past performance, rider ratings, and proximity.
-dynamic surge pricing based on real-time demand/supply, time of day, and predicted future demand.
- treat amount as paise to get whole int currency is inr 
-eta prediction model.


# Rules
1. Ask me if anything is unclear. Don't assume.
2. Keep it simple. Small classes, one job each, SOLID. No over-engineering.
3. One small phase at a time (reviewable in 2-3 minutes). Wait for my OK.
4. Never change a test to make it pass. If a test changes, say stronger or weaker.
5. Anything touched by concurrency must be thread-safe and never deadlock.
   Lock per item, never one global lock. Never hold a lock during slow work.
6. Time comes from an injectable clock. No randomness in decisions.
7. One-line plain-English comment above every code block.
8. If you can't follow an instruction, tell me. Never switch approach silently.

# Every phase
- Leak: no sensitive data in errors, logs or printed objects.
- Edges: test exactly at each limit and one past it.
- Who: each operation checks who is calling and rejects anyone not allowed.
- Tests: happy path, sad paths, exact edges, one concurrency test for shared data.
- Run fast tests only; stress tests and mutation checks at the end.

# After each phase, report
1. What you built, and which requirements it covers
2. Test count before -> after
3. Assumptions you made without asking me
4. Whether you followed each instruction I gave