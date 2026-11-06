# Interview Preparation

Goal: be able to explain, defend and modify every line of this project. Read the code with this
document open. Each section points at real functions and tests.

**Before you submit (checklist)**

- [ ] Run everything yourself: `docker compose up --build` and `docker compose run --rm web pytest`.
- [ ] Create the public GitHub repo, push, and open it in a private window to confirm it is public.
- [ ] Check `git log`: author name/e-mail must be yours, and the history should reflect how *you*
      actually worked. Do not hide that you used AI assistance; the assignment explicitly allows it.
- [ ] Be able to break the code on purpose and watch the right test fail (see Q18).
- [ ] Make sure `.env` is not committed (`git ls-files | grep env` should show only `.env.example`).

---

## A. Architecture

### Request flow (booking)

```
POST /api/bookings/
  → BookingViewSet.create (views.py)
      → BookingCreateSerializer validates types/ranges, check_out > check_in
      → services.create_booking(BookingRequest)
          loop (max 10 attempts), each attempt = transaction.atomic():
            1. _lock_first_free_room:  SELECT … WHERE beds/view match
                                        AND NOT EXISTS (overlapping booking)
                                        ORDER BY label LIMIT 1 FOR UPDATE
            2. none?               → NoRoomAvailableError  → HTTP 409 no_room_available
            3. re-check overlap on that room (new statement, fresh snapshot)
               overlap?            → _LostRace → rollback attempt (lock released) → retry
            4. INSERT booking      → commit
          INSERT violates exclusion constraint → BookingConflictError → HTTP 409 booking_conflict
      → BookingSerializer(booking) (nested room) → 201
```

### Layers

- **views.py**: HTTP only. Maps domain exceptions to status codes. No business rules.
- **serializers.py**: input validation and output shape. No DB logic beyond what DRF gives for free.
- **services.py**: the booking rules and the concurrency protocol. Knows nothing about HTTP.
- **models.py**: tables, indexes and constraints. The overlap rule lives here as an
  `ExclusionConstraint`, so it is enforced for any writer (admin script, shell, future code).
- **PostgreSQL**: final authority on integrity.

### Database interactions per booking (happy path)

`BEGIN` → 1 locking SELECT (room) → 1 overlap EXISTS (re-check) → 1 INSERT → `COMMIT`.
Constant, independent of room count (apart from the index scan inside the SELECT).

---

## B. Critical decisions and alternatives

| Decision | Why | Alternatives and why not |
|---|---|---|
| **PostgreSQL** | Exclusion constraints (`EXCLUDE USING gist`) and row-level locks give database-enforced non-overlap. Assignment prefers it. | SQLite: no range types/exclusion constraints and weak concurrency semantics. MySQL: no exclusion constraints; you would need gap locks or application-level locks. |
| **Monolith, single Django app, service layer** | Small domain; the assignment warns against overengineering. A service function is easy to test and keeps views thin. | Microservices, repositories, CQRS: cost without benefit here. Fat models/views: logic hard to test and reuse. |
| **Exclusion constraint as the guarantee** | Cannot be bypassed by a bug or another code path; correct under any concurrency; PostgreSQL makes concurrent inserters wait for each other. | Check-then-insert (racy). Unique `(room, date)` rows (breaks for hourly bookings, needs one row per night). Application locks (only protect code that remembers to use them). |
| **Row lock on the room + re-check** | Not needed for safety, needed for *behaviour*: the loser of a race picks another free room instead of erroring. Rooms exist, so they can be locked; bookings that do not exist yet cannot. | Advisory locks keyed by feature class (lighter, but invisible to the ORM and serialises a whole class). Table lock (serialises everything). `SKIP LOCKED` (false "no room"). |
| **`READ COMMITTED`** | PostgreSQL default; each statement sees the latest committed data, which the post-lock re-check relies on. | `SERIALIZABLE` + retry on `40001`: also correct, but more aborts, retry plumbing everywhere, and still needs the constraint-free reasoning to be redone. |
| **One lock at a time; roll back the attempt to release it** | Prevents lock-ordering deadlocks between requests with different periods. | Locking all matching rooms in label order (also deadlock-free, but locks many rows). |
| **Half-open `[check_in, check_out)`** | Same-day turnover: guest B can check in at the instant guest A checks out. | Closed intervals would forbid that and make "adjacent" ambiguous. |
| **`timestamptz`, UTC** | Unambiguous instants; offsets in input are normalised. | Naive timestamps cause DST/zone bugs. |
| **Lowest `label` wins** | Deterministic and simple to explain; the index `(beds, view, label)` returns rows in that order. | Random (non-deterministic, hard to test), "best fit" (extra rules not requested), lowest `id`. |
| **409 for "no room"** | Well-formed request that conflicts with current availability. | 404 (nothing "not found"), 422/400 (input is valid), 200 with empty body (hides failure). |
| **Booking is a top-level resource** (`/api/bookings/`) | The client does not choose the room; the server assigns it. | `/rooms/{id}/bookings/` implies the client picked the room. |
| **GiST-backed overlap probe** | One index serves the constraint and the lookup. | Extra b-tree `(room_id, check_in, check_out)`: more write cost, scans every earlier booking of the room. |

---

## C. Difficult interview questions

Format: **Answer** → **In our code** → **Follow-up**.

### Q1. Why isn't "check availability, then insert" enough?
**Answer.** Two transactions can run the check at the same time; both see no overlap (neither
has committed), both insert. Under `READ COMMITTED` each statement sees only committed data,
so the check proves nothing about concurrent in-flight inserts. The gap between check and
insert is a classic TOCTOU race.
**In our code.** `test_constraint_stops_double_booking_even_without_locking` forces this exact
interleaving with a `threading.Barrier` between check and insert; without the constraint both
rows would be written, with it exactly one thread gets `IntegrityError`.
**Follow-up.** "Would wrapping both statements in `transaction.atomic()` fix it?" No: atomic
gives atomicity, not mutual exclusion; at `READ COMMITTED` the two transactions still don't see
each other.

### Q2. Walk me through the exclusion constraint.
**Answer.** `EXCLUDE USING gist (room_id WITH =, tstzrange(check_in, check_out, '[)') WITH &&)`
says: no two rows may have equal `room_id` *and* overlapping ranges. It is a generalisation of
`UNIQUE` where "equal" can be any operator. GiST supports `&&` on ranges; `btree_gist` adds
GiST operator classes for scalar types so `room_id WITH =` can be in the same index.
**In our code.** `Booking.Meta.constraints` (`ExclusionConstraint`) and `BtreeGistExtension()`
as the first operation in `0001_initial.py`. `test_constraints.py` tests identical, nested,
partially overlapping and adjacent ranges.
**Follow-up.** "What happens if the extension can't be created?" The migration fails; on
managed Postgres you need it allowed (trusted since PG13 for the database owner).

### Q3. How does PostgreSQL behave when two *uncommitted* transactions insert conflicting rows?
**Answer.** Like a unique index: the second inserter detects the first's uncommitted row,
waits for that transaction to finish, then raises an exclusion violation if the first
committed (or proceeds if it rolled back). Not deferrable here, so checked per statement.
**In our code.** `_is_overlap_violation` inspects `exc.__cause__.diag.constraint_name` to
translate only our constraint into `BookingConflictError`.
**Follow-up.** "Can exclusion constraints be deferred?" They can be `DEFERRABLE`, but then
violations surface at commit instead of at the statement; we keep it immediate so the failure
is raised exactly where the insert happens.

### Q4. If the constraint guarantees correctness, why also lock?
**Answer.** Safety vs liveness/behaviour. Without locks, two requests choose room 101, one
blocks on the constraint and then fails, even though 102 was free: a spurious failure. The
lock lets the loser find out *before* inserting and move to another room.
**In our code.** `_lock_first_free_room` (`FOR UPDATE`) plus the re-check in `create_booking`.
`test_waiter_rechecks_after_acquiring_the_room_lock` shows the loser ends up in room B with no
error.
**Follow-up.** "So is the lock needed for correctness?" No. If you remove it, correctness is
unchanged (the constraint still holds) but clients see more `409`s.

### Q5. Why re-check *after* taking the lock? Isn't the NOT EXISTS in the locking query enough?
**Answer.** The query's `NOT EXISTS` is evaluated with the statement's snapshot, taken
*before* waiting for the lock. `FOR UPDATE` only re-evaluates a row after a wait if the row
was *updated*; here the other transaction merely locked the room and inserted a booking
elsewhere, so there is no re-evaluation, and the decision may be stale. A new statement
afterwards gets a fresh snapshot that includes everything the previous lock-holder committed.
**In our code.** The `_overlapping_bookings(...).filter(room=room).exists()` check right after
`_lock_first_free_room`. Mutation test: replacing `raise _LostRace` with `pass` makes five
concurrency tests fail.
**Follow-up.** "Would it still work at `REPEATABLE READ`?" The snapshot is fixed at the first
statement, so the re-check would be stale; the constraint would still stop a double booking
but you would get `409 booking_conflict` instead of another room.

### Q6. "You can't lock a row that doesn't exist yet." How do you serialise bookings?
**Answer.** Lock a row that *does* exist and that every booker of that resource must touch:
the room row. It acts as a mutex for that room's bookings. (Alternatives: advisory locks,
serialisable isolation with predicate locks.) Gap/predicate locking via range scans isn't
available in PostgreSQL's row locking.
**In our code.** `.select_for_update()` on `Room`; inserts always happen after holding that lock.
**Follow-up.** "What if some script inserts bookings without the lock?" The exclusion
constraint still protects integrity; that script may get an `IntegrityError`.

### Q7. Why not `SKIP LOCKED`?
**Answer.** It would skip a room that is locked because someone is booking it for
*different dates* and falsely report no availability (or choose a worse room). `SKIP LOCKED`
suits work queues where any row will do, not exact availability answers.
**In our code.** `test_non_conflicting_requests_for_one_room_all_succeed`: ten disjoint
periods on a single room must all succeed under concurrency; with `SKIP LOCKED` they would not.
**Follow-up.** "When would you reintroduce it?" At large scale as a first pass to spread load,
with a blocking fallback pass before answering "no room".

### Q8. Can this deadlock?
**Answer.** A deadlock needs a cycle of waits. A transaction that held room A while waiting for
room B, and another holding B while waiting for A, would deadlock (possible with different
periods). We avoid it: each attempt holds at most one room lock, and a lost race rolls the
attempt back, releasing the lock before the next candidate is locked.
**In our code.** `_LostRace` raised inside `transaction.atomic()`; also
`test_heavy_contention_no_errors_no_deadlocks` (40 simultaneous overlapping requests; a
deadlock would show up as `OperationalError`).
**Follow-up.** "What if it did deadlock?" PostgreSQL detects it after `deadlock_timeout` (1s),
aborts one transaction with `40P01`, and Django raises `OperationalError`; you'd retry.

### Q9. Which isolation level do you run at and why?
**Answer.** `READ COMMITTED`, PostgreSQL's default, set explicitly in `settings.py` so nobody
changes it unknowingly: the post-lock re-check depends on per-statement snapshots.
**In our code.** `DATABASES["default"]["OPTIONS"]["isolation_level"]`.
**Follow-up.** "What anomalies exist at READ COMMITTED?" Non-repeatable reads and phantoms;
we deliberately use them (we *want* the fresh snapshot after the lock) and the constraint
covers the rest.

### Q10. Why not just use `SERIALIZABLE`?
**Answer.** It can also prevent double booking (SSI aborts one of the conflicting
transactions with `40001`), but every write path must retry on serialization failures, aborts
rise under contention, and you still want a hard constraint. It also makes "pick another
room" harder, because the failing transaction is simply aborted.
**In our code.** Not used. Mention it as the alternative in §B.
**Follow-up.** "Is SSI exact?" No: it can produce false-positive aborts.

### Q11. Compare alternatives: advisory lock, table lock, `UNIQUE(room, date)`.
**Answer.**
- *Advisory lock per feature class* (`pg_advisory_xact_lock(hash(beds, view))`): one cheap
  lock instead of N row locks; but invisible to ORM tooling and serialises the whole class.
- *Table lock*: trivial and correct; serialises all bookings in the system.
- *`UNIQUE(room, night)` rows*: needs one row per night, doesn't model hourly check-in/out;
  extra write amplification.
**In our code.** None of these; we chose constraint + room row lock.
**Follow-up.** "Which would you use at 1,000 bookings/second?" See §D.

### Q12. Why half-open intervals?
**Answer.** `[check_in, check_out)` lets a stay end exactly when the next starts without
conflict, and makes the overlap condition `a.in < b.out AND a.out > b.in` with strict
comparisons. Closed intervals would make same-instant turnover a conflict.
**In our code.** `RangeBoundary()` renders `'[)'`; `test_adjacent_requests_allowed` and
`test_adjacent_bookings_allowed_by_constraint`; `test_overlap_condition_matches_explicit_comparison`
checks the range form equals the explicit comparisons.
**Follow-up.** "What about a cleaning buffer between guests?" Add a buffer to `check_out` when
storing (or widen the range in the constraint expression).

### Q13. Why `timestamptz` and UTC? What about naive datetimes?
**Answer.** `timestamptz` stores an absolute instant (converted to UTC); offsets in input are
honoured. A naive datetime has no instant; DRF with `USE_TZ=True` interprets it in the current
timezone (UTC here). Hotels care about *local* check-in times, so a real product would store
the hotel's timezone.
**In our code.** `USE_TZ=True`, `test_offsets_are_normalised`.
**Follow-up.** "How would you model 'check-out at 11:00 hotel-local'?" Store hotel timezone,
convert local policy times to instants before persisting.

### Q14. Show the room-selection query and explain its plan.
**Answer.** `Room.objects.filter(beds, view).filter(~Exists(overlapping)).order_by("label")
.select_for_update()[:1]` → one SQL statement: index scan on `(number_of_beds, view, label)`
returns rooms in label order, each probed with an anti-join against the GiST index for
overlapping bookings; `LIMIT 1` stops at the first free room. On 3,000 rooms and 90,000
bookings `EXPLAIN` showed exactly this (index scan on `room_beds_view_label_idx`, index scan on
`booking_no_overlap_per_room`).
**In our code.** `_lock_first_free_room`, `_overlapping_bookings`.
**Follow-up.** "Why does the plan matter for the lock?" Rows are locked as they are
returned; with `LIMIT 1` only the chosen room is locked.

### Q15. Why that index column order? Why GiST for bookings?
**Answer.** `(number_of_beds, view, label)`: equality columns first, then the sort column, so
the planner reads matching rooms already ordered and stops early. For bookings, `&&` on a range
needs GiST; the same index backs the exclusion constraint, so we don't maintain another.
**In our code.** `Room.Meta.indexes`, `Booking.Meta.constraints`; `_overlapping_bookings`
is written with the range operator so the planner can use that index.
**Follow-up.** "Is `room_id` FK index redundant?" Largely; kept because Django creates it,
it helps `?room=` filters and FK checks on delete.

### Q16. Where could N+1 queries appear, and how did you prevent them?
**Answer.** Serialising a list of bookings with a nested room would run one room query per
booking. Prevented with `select_related("room")`. Selection itself is a single SQL statement, no
Python loop over rooms.
**In our code.** `BookingViewSet.queryset`; `test_retrieve_and_list_with_filters_and_constant_query_count`
asserts ≤2 queries (count + joined select) regardless of row count.
**Follow-up.** "Pagination count cost?" `COUNT(*)` on large tables is slow in PostgreSQL;
consider cursor pagination.

### Q17. Django pitfalls with `select_for_update` and `atomic`?
**Answer.** `select_for_update` must run inside a transaction (else `TransactionManagementError`);
querysets are lazy, so evaluate inside the `atomic` block; `ATOMIC_REQUESTS` or an outer atomic
turns our attempt into a savepoint (locks are then held until the outer transaction ends);
`select_for_update` locks all joined tables unless `of=` is given (we have no joins).
**In our code.** `next(iter(candidates), None)` evaluates the query inside `atomic`; README
notes the outer-transaction caveat. `_LostRace` rolls back the savepoint, which does release
locks acquired inside it.
**Follow-up.** "Does `RELEASE SAVEPOINT` release locks?" No; only `ROLLBACK TO SAVEPOINT` or
ending the top-level transaction does.

### Q18. How do you test concurrency, and how do you know the tests would catch a bug?
**Answer.** Real threads, each with its own DB connection, released together by a
`threading.Barrier`, against PostgreSQL with committed data (`transaction=True`). Assertions are
on invariants (exactly one winner; N winners in N distinct rooms; no overlapping rows in the
DB), not on timing. One test uses events to force a specific interleaving deterministically.
I also validated them by mutation: removing the re-check makes 5 tests fail; removing lock and
re-check makes 4 fail.
**In our code.** `hotel/tests/test_concurrency.py`; helper `run_concurrently`.
**Follow-up.** "Can concurrency tests prove absence of races?" No: they find bugs, they
don't prove correctness. The proof is the argument in README §Concurrency strategy: the
constraint makes overlap impossible; locking only affects which request wins.

### Q19. Why `409` for "no room available"? Why not `404` or `422`?
**Answer.** The input is valid and the endpoint exists; the request conflicts with the
current availability state. `409` expresses that and clients can retry with other dates.
`400/422` imply malformed input; `404` implies a missing resource.
**In our code.** `Conflict` exception in `views.py`; body includes a machine-readable `code`.
**Follow-up.** "Distinguish a temporary conflict (race) from permanent 'no room'?" We use the
codes `no_room_available` vs `booking_conflict`; the latter is retryable.

### Q20. What happens if a client retries `POST /bookings/` after a timeout?
**Answer.** We might create a second booking (in a different room, or the same user twice).
POST isn't idempotent. Production fix: an `Idempotency-Key` header stored with a unique
constraint, returning the original response on retry.
**In our code.** Not implemented; listed as a limitation.
**Follow-up.** "Where would the key be stored?" A column on `Booking` with a unique
`(user_id, idempotency_key)` constraint.

### Q21. How would you add cancellation without breaking the constraint?
**Answer.** Add `status`, and make the exclusion constraint partial:
`condition=Q(status="confirmed")`, so cancelled bookings stop blocking the room. Index and
queries then also filter on status; the overlap probe must include `status="confirmed"`.
**In our code.** Not implemented; `_overlapping_bookings` is the single place to change, plus
the constraint.
**Follow-up.** "Soft delete vs hard delete?" Soft: keep history/audit; the partial constraint
makes it work.

### Q22. A room's beds/view is edited while future bookings exist. Problem?
**Answer.** Existing bookings were made for the old features; the guest might now be in a room
that no longer matches. We accept this (assumption in README) and keep data consistent
(no overlaps). A real system would block such edits or require re-allocation.
**In our code.** `RoomViewSet` allows update; no check on bookings.
**Follow-up.** "How would you block it?" Check for future bookings in `perform_update` and
return `409`.

### Q23. Security review: what did you consider?
**Answer.** All input validated by serializers (types, ranges, choices); ORM prevents SQL
injection; secrets only from environment, app refuses to start without `DJANGO_SECRET_KEY`;
`DEBUG` off by default; `ALLOWED_HOSTS` configurable; `.env` git-ignored; containers run as
non-root. Authentication and rate limiting are intentionally absent (out of scope), which is
the main gap for exposure to the internet.
**In our code.** `config/settings.py`, `Dockerfile`, `.gitignore`.
**Follow-up.** "What's the first thing you'd add?" Authentication (user id from the token,
not from the request body), then rate limiting.

### Q24. Why is `user_id` in the request body? Isn't that insecure?
**Answer.** The assignment says the API receives a user identifier and that user management
isn't required. With real auth the user would come from the credentials, and accepting a body
`user_id` would be an authorization hole (booking on behalf of others).
**In our code.** `BookingCreateSerializer.user_id`.
**Follow-up.** "How would you migrate?" Remove the field, set `user_id=request.user.id` in the view.

### Q25. Why only one lock attempt cap (10) and what happens when it's exhausted?
**Answer.** A lost race means another request got a room first. With R matching rooms at most
about R races can be lost before no candidates remain, so the loop terminates naturally; the
cap is a safety net. On exhaustion we return `409 booking_conflict` (retryable).
**In our code.** `MAX_ALLOCATION_ATTEMPTS`.
**Follow-up.** "Is 10 right for a class with 200 rooms and a stampede?" Possibly not; make it
configurable or loop until no candidates, with metrics on retries.

### Q26. How does the design behave with multiple app instances?
**Answer.** No state in the app: locks and constraints live in PostgreSQL, so any number of
processes/instances is safe. Throughput is bounded by the database and the per-room lock.
**In our code.** gunicorn runs 3 workers in `docker-compose.yml`; concurrency tests simulate
parallel connections.
**Follow-up.** "Does PgBouncer transaction pooling break this?" No: our locks are
transaction-scoped (no session advisory locks, no `SET`).

### Q27. Your DRF `UniqueValidator` is racy. How is it handled?
**Answer.** It's check-then-insert, so two simultaneous creates with one label can both pass;
the database `UNIQUE` constraint is authoritative. We catch `IntegrityError` and return
`400` for label.
**In our code.** `RoomViewSet._save`; `test_duplicate_label_race_is_400_not_500` simulates the race
by disabling the validator.
**Follow-up.** "Same pattern elsewhere?" Yes: it's the same philosophy as bookings, let the DB be the arbiter.

---

## D. Weaknesses and what would change at scale

**Actual limitations**

1. **Hot-spot on the lowest-label free room.** Concurrent requests for a feature class queue on
   one row lock. Correct but not maximal throughput.
2. **Selection cost** grows with booked rooms that sort before the first free one.
3. **No idempotency keys**, no cancellation, no modify, no "search availability" endpoint.
4. **No authentication/authorization**; `user_id` is trusted.
5. **Lexicographic label order**, so `"10"` sorts before `"9"`.
6. **Timezone naïveté**: everything is UTC; no hotel timezone.
7. **Offset pagination and `COUNT(*)`** are unsuitable for very large tables.
8. **Load not measured.** Tests prove correctness, not performance.

**If the system had…**

- **100,000 rooms.** The query still starts from `(beds, view, label)` but a class might hold
  thousands of rooms; scanning booked ones before the first free one becomes costly for popular
  dates. Options: partition inventory by hotel (rooms belong to a hotel anyway; every query is
  scoped by `hotel_id`), keep an availability table/materialised calendar per room-class and
  date, or randomise/hash the starting point to spread load. Check `EXPLAIN` before optimising.
- **Millions of bookings.** The GiST index grows; keep it small by partitioning `booking` by
  time (range partitions on `check_in`) and archiving past stays. Exclusion constraints on
  partitioned tables require the partition key in the constraint, so you'd adapt the design.
  Switch to cursor pagination; avoid `COUNT(*)`.
- **Thousands of concurrent booking requests.** Row-lock queueing per room class becomes the
  bottleneck and connections run out. Use a connection pooler (PgBouncer transaction mode),
  shard/scope by hotel, use `SKIP LOCKED` as a first pass (blocking fallback before answering
  "no room"), or a per-class advisory lock to cut lock count; consider a queue in front for
  flash-sale peaks only if measurements justify it.
- **Multiple application instances.** Already safe: all coordination is in PostgreSQL. Add
  health checks, structured logging, metrics on retry/conflict counts, idempotency keys, and
  statement/lock timeouts (`lock_timeout`) so a stuck transaction cannot block requests forever.
- **Read replicas** for list/retrieve endpoints; never for the allocation path.

---

## E. Code walkthrough: the 10 files that matter

1. **`hotel/services.py`**: the heart. Know `create_booking` line by line: the attempt loop,
   `_lock_first_free_room`, the re-check, `_LostRace`, how `IntegrityError` is translated, why a
   lost race rolls back. Be able to draw the timeline of two competing requests.
2. **`hotel/models.py`**: the `ExclusionConstraint` (`room =`, range `&&`), `TsTzRange`,
   `booking_period()`, `CheckConstraint`s, the `Room` index and why the column order is what it is.
3. **`hotel/migrations/0001_initial.py`**: `BtreeGistExtension()` must come first; you should be
   able to explain what the generated SQL looks like (`\d hotel_booking`).
4. **`hotel/tests/test_concurrency.py`**: each test, the invariant it asserts, the barrier
   technique, and the deterministic interleaving in `test_waiter_rechecks_…`.
5. **`hotel/views.py`**: thin controllers, `Conflict`, `perform_destroy` mapping `ProtectedError`,
   `_save` mapping the unique-label race, `select_related`.
6. **`hotel/serializers.py`**: why there are two booking serializers (input vs output), where
   `check_out > check_in` is validated (and again in the service and the DB).
7. **`config/settings.py`**: env-based config, explicit isolation level, `django.contrib.postgres`,
   no authentication classes (deliberate), pagination and filter defaults.
8. **`hotel/tests/test_constraints.py`**: proves the database enforces the rules without the
   service; know the adjacent vs overlapping cases.
9. **`hotel/tests/test_booking_service.py`**: overlap matrix, deterministic selection, and the
   test comparing the range operator with the explicit `<`/`>` condition.
10. **`docker-compose.yml` (+ `Dockerfile`, `.env.example`)**: health-checked DB before
    migrations, no secrets in the repo, how to run tests in the container.

**Drill:** open `services.py`, comment out the re-check, run
`pytest hotel/tests/test_concurrency.py`, and explain every failure. Then restore it. Do the
same with the `ExclusionConstraint` in a scratch migration to see `test_constraints.py` fail.
