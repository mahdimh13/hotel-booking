"""Booking business logic: automatic room selection and race-free allocation.

Concurrency strategy (details and proof sketch in README):

1. **Hard guarantee** - the ``booking_no_overlap_per_room`` exclusion constraint. Even if
   every other line of this module were wrong, PostgreSQL refuses an overlapping row.
2. **Coordination** - the chosen room row is locked with ``SELECT ... FOR UPDATE`` and the
   overlap is re-checked *after* the lock is held (a new statement, hence a fresh
   READ COMMITTED snapshot). Competing requests for the same room queue up and the
   loser moves on to the next free room instead of failing.
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef
from psycopg.types.range import Range

from .models import BOOKING_NO_OVERLAP_CONSTRAINT, Booking, Room, booking_period

logger = logging.getLogger(__name__)

# Each retry means another request committed a conflicting booking between our candidate
# query and our lock. That can only repeat while there are still rooms being taken, so a
# small cap is a safety net, not a tuning knob.
MAX_ALLOCATION_ATTEMPTS = 10


class InvalidBookingPeriodError(ValueError):
    """check_out is not strictly after check_in."""


class NoRoomAvailableError(Exception):
    """No room with the requested features is free for the whole period."""


class BookingConflictError(Exception):
    """A room was taken by a concurrent request and we could not recover in time."""


@dataclass(frozen=True)
class BookingRequest:
    user_id: int
    number_of_beds: int
    view: str
    check_in: datetime
    check_out: datetime


def _period(check_in: datetime, check_out: datetime) -> Range:
    return Range(check_in, check_out, "[)")


def _overlapping_bookings(check_in: datetime, check_out: datetime):
    """Bookings whose [check_in, check_out) overlaps the given interval.

    Written with the range ``&&`` operator so PostgreSQL can answer it from the GiST index
    that backs the exclusion constraint. It is equivalent to
    ``existing.check_in < check_out AND existing.check_out > check_in`` (tested).
    """
    return Booking.objects.annotate(period=booking_period()).filter(
        period__overlap=_period(check_in, check_out)
    )


def _lock_first_free_room(req: BookingRequest) -> Room | None:
    """First (by label) matching room that looked free, with its row locked.

    The NOT EXISTS is evaluated against the statement's snapshot, so it is only a
    *candidate* filter: the caller must re-check once the lock is held.
    """
    taken = _overlapping_bookings(req.check_in, req.check_out).filter(room=OuterRef("pk"))
    candidates = (
        Room.objects.filter(number_of_beds=req.number_of_beds, view=req.view)
        .filter(~Exists(taken))
        .order_by("label")
        .select_for_update()[:1]
    )
    return next(iter(candidates), None)


def _is_overlap_violation(exc: IntegrityError) -> bool:
    diag = getattr(exc.__cause__, "diag", None)
    return getattr(diag, "constraint_name", None) == BOOKING_NO_OVERLAP_CONSTRAINT


def create_booking(req: BookingRequest) -> Booking:
    """Book the first free matching room for [check_in, check_out).

    Raises ``InvalidBookingPeriodError``, ``NoRoomAvailableError`` or
    ``BookingConflictError``. Never returns a booking that overlaps another one.
    """
    if req.check_out <= req.check_in:
        raise InvalidBookingPeriodError("check_out must be after check_in.")

    try:
        with transaction.atomic():
            for _ in range(MAX_ALLOCATION_ATTEMPTS):
                room = _lock_first_free_room(req)
                if room is None:
                    raise NoRoomAvailableError(
                        "No room matches the requested features for this period."
                    )

                # We now hold the room's row lock: anyone else booking this room is waiting
                # for us. A new statement sees every booking committed before we got the
                # lock, so this check is accurate. If it fails we lost a race; the next
                # candidate query (fresh snapshot) will no longer return this room.
                if _overlapping_bookings(req.check_in, req.check_out).filter(room=room).exists():
                    continue

                return Booking.objects.create(
                    user_id=req.user_id,
                    room=room,
                    check_in=req.check_in,
                    check_out=req.check_out,
                )
    except IntegrityError as exc:
        if _is_overlap_violation(exc):
            # Only reachable if the locking protocol was bypassed or the isolation level
            # is stricter than READ COMMITTED; the database still refused the double booking.
            logger.warning("Exclusion constraint rejected a booking: %s", exc)
            raise BookingConflictError("The room was booked by a concurrent request.") from exc
        raise

    raise BookingConflictError("Could not allocate a room under contention; please retry.")
