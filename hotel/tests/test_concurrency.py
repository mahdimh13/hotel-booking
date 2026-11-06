"""Concurrency tests. They run real threads against real PostgreSQL connections.

They need ``transaction=True`` (committed data, one connection per thread) and PostgreSQL
(row locks and the exclusion constraint). They would prove nothing on SQLite.
"""

import itertools
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import IntegrityError, connection, transaction

from hotel.models import Booking, Room
from hotel.services import (
    BookingConflictError,
    BookingRequest,
    NoRoomAvailableError,
    create_booking,
)
from hotel.tests.conftest import at

pytestmark = pytest.mark.django_db(transaction=True)

THREAD_TIMEOUT = 30


def run_concurrently(callables):
    """Start every callable at the same instant; return each result or raised exception."""
    barrier = threading.Barrier(len(callables))

    def worker(fn):
        try:
            barrier.wait(THREAD_TIMEOUT)
            return fn()
        except Exception as exc:  # noqa: BLE001 - outcomes are asserted by the tests
            return exc
        finally:
            connection.close()  # each thread owns a connection; don't leak it

    with ThreadPoolExecutor(max_workers=len(callables)) as pool:
        futures = [pool.submit(worker, fn) for fn in callables]
        return [f.result(THREAD_TIMEOUT) for f in futures]


def request(ci, co, user=1, beds=2, view="sea"):
    return BookingRequest(user, beds, view, ci, co)


def assert_no_overlaps_in_db():
    by_room = {}
    for b in Booking.objects.order_by("room_id", "check_in"):
        by_room.setdefault(b.room_id, []).append(b)
    for bookings in by_room.values():
        for earlier, later in itertools.pairwise(bookings):
            assert earlier.check_out <= later.check_in, (earlier, later)


def test_same_slot_one_room_exactly_one_winner(make_room):
    make_room()
    results = run_concurrently(
        [lambda u=u: create_booking(request(at(1), at(3), user=u)) for u in range(1, 13)]
    )
    winners = [r for r in results if isinstance(r, Booking)]
    losers = [r for r in results if isinstance(r, NoRoomAvailableError)]
    assert len(winners) == 1
    assert len(losers) == 11  # nobody got an unexpected error
    assert Booking.objects.count() == 1


def test_same_slot_n_rooms_exactly_n_winners_in_distinct_rooms(make_room):
    for _ in range(3):
        make_room()
    results = run_concurrently(
        [lambda u=u: create_booking(request(at(1), at(3), user=u)) for u in range(1, 11)]
    )
    winners = [r for r in results if isinstance(r, Booking)]
    assert len(winners) == 3
    assert len({w.room_id for w in winners}) == 3
    assert all(isinstance(r, NoRoomAvailableError) for r in results if not isinstance(r, Booking))
    assert_no_overlaps_in_db()


def test_non_conflicting_requests_for_one_room_all_succeed(make_room):
    """Locking must not produce false 'no room' answers for disjoint periods."""
    make_room()
    results = run_concurrently(
        [lambda d=d: create_booking(request(at(d), at(d + 1))) for d in range(1, 11)]
    )
    assert all(isinstance(r, Booking) for r in results), results
    assert Booking.objects.count() == 10


def test_mixed_overlapping_requests_never_double_book(make_room):
    rooms = [make_room() for _ in range(2)]
    spans = [(d, d + length) for d in range(1, 8) for length in (1, 2, 3)]
    results = run_concurrently(
        [lambda s=s: create_booking(request(at(s[0]), at(s[1]))) for s in spans]
    )

    assert not [r for r in results if isinstance(r, BookingConflictError)], results
    assert all(isinstance(r, Booking | NoRoomAvailableError) for r in results), results
    assert Booking.objects.count() == sum(isinstance(r, Booking) for r in results)
    assert Booking.objects.filter(room__in=rooms).count() == Booking.objects.count()
    assert_no_overlaps_in_db()


def test_waiter_rechecks_after_acquiring_the_room_lock(make_room):
    """Deterministic interleaving of the race the post-lock re-check exists for.

    B picks room A (it looks free in B's snapshot) and blocks on A's row lock. Meanwhile
    the lock holder commits an overlapping booking for A. When B wakes up its snapshot
    from the candidate query is stale; the re-check must notice and move on to room B
    rather than inserting (which the constraint would reject -> BookingConflictError).
    """
    room_a, room_b = make_room(label="A"), make_room(label="B")
    holder_has_lock, release_holder = threading.Event(), threading.Event()

    def holder():
        try:
            with transaction.atomic():
                Room.objects.select_for_update().get(pk=room_a.pk)
                holder_has_lock.set()
                assert release_holder.wait(THREAD_TIMEOUT)
                Booking.objects.create(user_id=1, room=room_a, check_in=at(1), check_out=at(3))
        finally:
            connection.close()

    def waiter():
        try:
            return create_booking(request(at(2), at(4), user=2))
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        holder_future = pool.submit(holder)
        assert holder_has_lock.wait(THREAD_TIMEOUT)
        waiter_future = pool.submit(waiter)
        time.sleep(0.7)
        assert not waiter_future.done(), "waiter should be blocked on the room lock"
        release_holder.set()
        holder_future.result(THREAD_TIMEOUT)
        booking = waiter_future.result(THREAD_TIMEOUT)

    assert booking.room_id == room_b.pk
    assert_no_overlaps_in_db()


def test_constraint_stops_double_booking_even_without_locking(make_room):
    """Last line of defence: naive check-then-insert, forced into the classic race.

    Both threads see the room as free, then both insert. Without the exclusion constraint
    both rows would be written. This documents *why* the check alone is not enough.
    """
    room = make_room()
    seen_free = threading.Barrier(2)

    def naive_booking(user):
        with transaction.atomic():
            clash = Booking.objects.filter(
                room=room, check_in__lt=at(3), check_out__gt=at(1)
            ).exists()
            assert not clash
            seen_free.wait(THREAD_TIMEOUT)  # both have checked before either inserts
            return Booking.objects.create(user_id=user, room=room, check_in=at(1), check_out=at(3))

    results = run_concurrently([lambda: naive_booking(1), lambda: naive_booking(2)])
    assert sum(isinstance(r, Booking) for r in results) == 1
    assert sum(isinstance(r, IntegrityError) for r in results) == 1
    assert Booking.objects.count() == 1
