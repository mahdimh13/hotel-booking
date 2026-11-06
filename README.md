# Hotel Room Booking System

A small Django + Django REST Framework service that manages hotel rooms and books the first
available room matching requested features (number of beds, view) for a time period.
A room can never have two overlapping bookings, **even under concurrent requests**: this is
enforced by PostgreSQL itself, not only by application code.

- Python 3.12, Django 5.2, DRF, PostgreSQL 16, pytest
- One Django app (`hotel`), a thin HTTP layer, and a service layer holding the booking logic
- 71 tests, including real multi-threaded tests against PostgreSQL

## Contents

1. [Quick start](#quick-start) · 2. [Configuration](#environment-variables) · 3. [API](#api) ·
4. [Architecture](#architecture) · 5. [Database design](#database-design) ·
6. [Booking algorithm](#booking-algorithm) · 7. [Concurrency strategy](#concurrency-strategy) ·
8. [Indexing and performance](#indexing-and-performance) · 9. [Testing](#testing) ·
10. [Decisions and assumptions](#technical-decisions-and-assumptions) ·
11. [Limitations and future work](#known-limitations-and-future-improvements)

## Quick start

### With Docker (recommended)

```bash
cp .env.example .env        # then edit DJANGO_SECRET_KEY and POSTGRES_PASSWORD
docker compose up --build   # starts PostgreSQL, applies migrations, serves on :8000
```

The API is then available at `http://localhost:8000/api/`.
Run the test suite inside the container:

```bash
docker compose run --rm web pytest
```

### Without Docker

Requires Python 3.12+ and a running PostgreSQL 13+ (the `btree_gist` extension must be
creatable by the database user, which is the case for the database owner on PostgreSQL 13+).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env && set -a && source .env && set +a   # POSTGRES_HOST=localhost
python manage.py migrate
python manage.py runserver
pytest
```

The user running the tests needs the `CREATEDB` privilege (pytest-django creates `test_<db>`).

## Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `DJANGO_SECRET_KEY` | yes | – | Django secret key. The app refuses to start without it. |
| `DJANGO_DEBUG` | no | `false` | Django debug mode. |
| `DJANGO_ALLOWED_HOSTS` | no | `localhost,127.0.0.1` | Comma-separated host names. |
| `POSTGRES_DB` | no | `hotel` | Database name. |
| `POSTGRES_USER` | no | `hotel` | Database user. |
| `POSTGRES_PASSWORD` | yes in practice | empty | Database password. |
| `POSTGRES_HOST` | no | `localhost` | Database host (`db` under Docker Compose). |
| `POSTGRES_PORT` | no | `5432` | Database port. |

No credentials are stored in the repository; `.env` is git-ignored.

## API

Base path: `/api/`. JSON only. No authentication (explicitly out of scope).
Datetimes are ISO 8601; send an explicit offset (`Z` or `+02:00`). They are stored and
returned in UTC. Room views are lowercase: `sea`, `mountain`, `yard`.

### Rooms

| Method | Path | Description | Success |
|---|---|---|---|
| `POST` | `/api/rooms/` | Create a room | `201` |
| `GET` | `/api/rooms/` | List rooms (paginated; filters `number_of_beds`, `view`) | `200` |
| `GET` | `/api/rooms/{id}/` | Retrieve a room | `200` |
| `PUT` / `PATCH` | `/api/rooms/{id}/` | Full / partial update | `200` |
| `DELETE` | `/api/rooms/{id}/` | Delete a room (`409` if it has bookings) | `204` |

```bash
curl -X POST localhost:8000/api/rooms/ -H 'Content-Type: application/json' \
     -d '{"label":"101","number_of_beds":2,"view":"sea"}'
```
```json
{"id": 1, "label": "101", "number_of_beds": 2, "view": "sea"}
```

Validation failure (`400`), e.g. duplicate label, 9 beds, unknown view:
```json
{
  "label": ["room with this label already exists."],
  "number_of_beds": ["Ensure this value is less than or equal to 5."],
  "view": ["\"garden\" is not a valid choice."]
}
```

List response (`GET /api/rooms/?view=sea`, 20 items per page, `?page=2` for more):
```json
{"count": 2, "next": null, "previous": null,
 "results": [{"id": 1, "label": "101", "number_of_beds": 2, "view": "sea"},
             {"id": 2, "label": "102", "number_of_beds": 2, "view": "sea"}]}
```

### Bookings

| Method | Path | Description | Success |
|---|---|---|---|
| `POST` | `/api/bookings/` | Book the first free matching room | `201` |
| `GET` | `/api/bookings/` | List bookings (paginated; filters `user_id`, `room`) | `200` |
| `GET` | `/api/bookings/{id}/` | Retrieve a booking | `200` |

Bookings cannot be updated or deleted (no cancellation requirement; `405`).

```bash
curl -X POST localhost:8000/api/bookings/ -H 'Content-Type: application/json' -d '{
  "user_id": 7, "number_of_beds": 2, "view": "sea",
  "check_in": "2030-01-01T14:00:00Z", "check_out": "2030-01-03T11:00:00Z"}'
```
```json
{
  "id": 1,
  "user_id": 7,
  "room": {"id": 1, "label": "101", "number_of_beds": 2, "view": "sea"},
  "check_in": "2030-01-01T14:00:00Z",
  "check_out": "2030-01-03T11:00:00Z",
  "created_at": "2026-11-06T10:57:21.033816Z"
}
```

| Situation | Status | Body |
|---|---|---|
| Invalid input (`check_out <= check_in`, beds outside 1–5, unknown view, bad date…) | `400` | `{"check_out": ["Must be after check_in."]}` |
| No room with these features is free for the whole period | `409` | `{"detail": "No room matches the requested features for this period.", "code": "no_room_available"}` |
| Allocation could not be completed under contention (see below) | `409` | `{"detail": "...", "code": "booking_conflict"}` |
| Deleting a room that has bookings | `409` | `{"detail": "Room has bookings and cannot be deleted.", "code": "room_has_bookings"}` |

`409 Conflict` is used for "no room" because the request is well-formed but conflicts with the
current state of the resource (availability). `booking_conflict` is a safety net that should be
practically unreachable (see [Concurrency strategy](#concurrency-strategy)).

## Architecture

```
HTTP request
   │
   ▼
hotel/views.py        parse request, call service, map domain errors → status codes
   │      ▲
   │      └── hotel/serializers.py   input/output validation only
   ▼
hotel/services.py     room selection + race-free allocation (the business logic)
   │
   ▼
hotel/models.py       tables, indexes, CHECK and EXCLUDE constraints
   │
   ▼
PostgreSQL            final authority on data integrity
```

```
config/                 settings (all from env), root urls, wsgi
hotel/
  models.py             Room, Booking, exclusion constraint
  services.py           create_booking(): selection, locking, re-check
  serializers.py        RoomSerializer, BookingCreateSerializer, BookingSerializer
  views.py              RoomViewSet, BookingViewSet
  urls.py               router
  migrations/0001_initial.py   includes the btree_gist extension
  tests/                API, service, constraint and concurrency tests
Dockerfile, docker-compose.yml, .env.example
```

## Database design

**`hotel_room`** — `id`, `label` (unique, ≤50 chars), `number_of_beds` (smallint, CHECK 1–5),
`view` (`sea`/`mountain`/`yard`).
Index `room_beds_view_label_idx (number_of_beds, view, label)`.

**`hotel_booking`** — `id`, `user_id` (bigint, opaque: there is no user table), `room_id`
(FK, `ON DELETE PROTECT`), `check_in`, `check_out` (`timestamptz`), `created_at`.

Constraints on `hotel_booking`:

- `CHECK (check_out > check_in)`
- `EXCLUDE USING gist (room_id WITH =, tstzrange(check_in, check_out, '[)') WITH &&)`
  — named `booking_no_overlap_per_room`. This is what makes double booking impossible.
- Index `booking_user_idx (user_id)` for listing a user's bookings.

Intervals are **half-open `[check_in, check_out)`**: a guest may check in at the exact instant
another checks out. Two bookings overlap iff
`existing.check_in < new.check_out AND existing.check_out > new.check_in`, which is exactly
what `tstzrange(...,'[)') && tstzrange(...,'[)')` computes (a test compares both formulations
on a grid of intervals).

## Booking algorithm

`services.create_booking()` (see the code, it is ~60 lines):

1. Reject `check_out <= check_in`.
2. Start an attempt (a transaction, or a savepoint if the caller already has one).
3. One query picks the **first matching room by `label`** that has no overlapping booking and
   locks it: `SELECT … WHERE beds = ? AND view = ? AND NOT EXISTS (overlapping booking)
   ORDER BY label LIMIT 1 FOR UPDATE`. No rows → `NoRoomAvailableError` → `409`.
4. With the lock held, **re-check** (a new statement) that the room has no overlapping
   booking. If it does, we lost a race: roll the attempt back (releases the lock) and go to 3.
5. Insert the booking and commit.

Selection is deterministic: lowest `label` in plain string order (`"10" < "9"`; it is not a
natural sort). Everything happens in a bounded number of queries (no per-room loops in Python,
no N+1).

## Concurrency strategy

**The guarantee comes from the database.** PostgreSQL checks an exclusion constraint on every
insert against all rows that could conflict, including rows inserted by transactions that have
not committed yet: the second inserter waits for the first to finish, then fails with
`IntegrityError` if the first committed. Therefore, whatever the application does, no two
committed bookings of the same room can overlap. The constraint is not deferrable, so it is
checked at insert time.

**The lock + re-check make the application behave well.** Without them, two requests for the
same slot would both pick room 101 and one would fail with an error even though room 102 was
free. With them:

1. `FOR UPDATE` on the room row acts as a per-room mutex. We lock the *room* (a row that
   already exists) because you cannot lock a booking row that does not exist yet; this is
   the standard answer to "locking rows that aren't there".
2. Under `READ COMMITTED` (PostgreSQL's default, set explicitly in `settings.py`) every
   statement gets a new snapshot. The room query's `NOT EXISTS` was evaluated *before* we
   waited for the lock, and `FOR UPDATE` does not re-evaluate a subquery after waiting, so
   that decision may be stale. The re-check runs after the lock is acquired and sees every
   booking committed by whoever held the lock before us.
3. Every code path that inserts a booking locks the room first, so while we hold the lock no
   one else can add a booking for that room. Check and insert are therefore atomic with
   respect to other bookers of the same room.
4. If the re-check fails, the loser releases the lock and picks the next free room instead of
   returning an error. Each attempt holds at most one room lock, so requests cannot
   deadlock by waiting on each other's rooms.

**Why not `SKIP LOCKED`?** A request that skips a locked room can report "no room available"
for a room that is merely busy with someone else's *different* dates. That would be a wrong
answer, so we block instead; waits are short (one query + one insert).

**Assumptions to be aware of**

- PostgreSQL with `btree_gist`; `READ COMMITTED`. Under `REPEATABLE READ`/`SERIALIZABLE` the
  re-check would use an old snapshot; the constraint would still prevent a double booking,
  but the request would end as `409 booking_conflict` instead of using another room.
- If a caller wraps `create_booking` in its own outer transaction, a lost race rolls back
  only a savepoint; the lock is released correctly, but the outer transaction's length now
  determines how long other requests wait.

## Indexing and performance

- **Room selection** uses `room_beds_view_label_idx`: equality on `(beds, view)` and rows come
  out already ordered by `label`, so `LIMIT 1` can stop early. With 3,000 rooms and 90,000
  bookings, `EXPLAIN` showed an index scan on this index plus an anti-join probing the next
  index.
- **Overlap probe** is written with the range `&&` operator so it uses the GiST index that
  backs the exclusion constraint. One index therefore serves both integrity and lookup; no
  extra b-tree on `(room_id, check_in, check_out)` is needed.
- The FK index on `room_id` created by Django is also redundant with the GiST index for most
  purposes; it is kept because it speeds up `ON DELETE` checks and `?room=` filtering.
- `booking_user_idx` serves `?user_id=`.
- Booking list queries use `select_related("room")`; a test asserts a constant query count.
- Transactions are short (≈3 statements) to keep lock hold times small.

Cost grows with the number of *booked* rooms that sort before the first free one for the
requested feature class; this is fine for a hotel-sized dataset and is discussed under
limitations.

## Testing

```bash
pytest                      # all 71 tests (needs PostgreSQL)
pytest hotel/tests/test_concurrency.py -v
```

| File | What it covers |
|---|---|
| `test_rooms_api.py` | CRUD, duplicate labels, bed bounds (0, 1, 5, 6), invalid views, partial updates, delete rules |
| `test_booking_api.py` | success payload, 400 validation matrix, 409s, back-to-back bookings, timezone normalisation, list/filter query count |
| `test_booking_service.py` | overlap matrix (identical, partial, contained, 1-hour edges), adjacency, deterministic choice, feature matching, equivalence of the range overlap with the explicit comparison |
| `test_constraints.py` | the database rejects overlaps and bad intervals without the service |
| `test_concurrency.py` | real threads against PostgreSQL: one room / N requests → exactly one winner; N rooms → exactly N winners in distinct rooms; disjoint periods never get a false "no room"; mixed overlapping spans never double-book; 40-request contention run with no errors or deadlocks; deterministic interleaving proving the post-lock re-check; naive check-then-insert shown to be stopped by the constraint |

The concurrency tests use `django_db(transaction=True)` (committed data, one connection per
thread) and only make sense on PostgreSQL; SQLite would prove nothing about locking or
exclusion constraints. They were validated by mutation: removing the re-check or the lock
makes them fail.

Lint/format: `ruff check . && ruff format --check .`

## Technical decisions and assumptions

- **PostgreSQL only.** Exclusion constraints and row-level locks are the right tools here.
- **Opaque `user_id`.** No user model, so it is a positive integer with no foreign key.
- **Lowercase view values** (`sea`, `mountain`, `yard`); input is case-sensitive.
- **Room labels are unique case-sensitively**, whitespace is trimmed by DRF.
- **No restriction on past dates or maximum stay**: the assignment does not ask for it.
- **Timezones:** inputs are converted to UTC; naive datetimes are interpreted as UTC.
- **Deleting a room that has bookings is refused** (`409`) rather than cascading.
- **Updating a room's beds/view does not touch existing bookings.**
- **Bookings are append-only** (create, read). Cancellation is a natural next step.
- **No authentication**, as the assignment states user management is not required.
- **Plain DRF, no extra abstractions**: no repositories, no event bus, no cache.

## Known limitations and future improvements

- **Hot spot:** every request for a feature class first targets the lowest-label free room,
  so concurrent requests queue on that row's lock. Correct, and fast at this scale, but
  throughput for one popular class is bounded by single-room serialisation.
- **Selection cost** is proportional to how many earlier-sorted rooms are booked for the
  period. Fine for hundreds of rooms; for very large inventories see
  `INTERVIEW_PREPARATION.md` §D.
- **Label ordering is lexicographic**, not natural.
- **No pagination cap parameter** (`page_size` fixed at 20), no OpenAPI schema, no rate limiting.
- **No cancellation/modify-booking endpoints**, no room availability search endpoint.
- Not load-tested; no claims are made about production scale.
