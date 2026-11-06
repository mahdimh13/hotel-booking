"""Persistence layer: tables and the constraints that keep the data consistent.

Business rules for *choosing* a room live in ``services.py``; the rule that a room can
never have two overlapping bookings is enforced here, in the database, by an
``ExclusionConstraint`` so it holds no matter which code path writes a booking.
"""

from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateTimeRangeField, RangeBoundary, RangeOperators
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import F, Func, Q

BOOKING_NO_OVERLAP_CONSTRAINT = "booking_no_overlap_per_room"


class TsTzRange(Func):
    """SQL ``tstzrange(lower, upper, bounds)``; ``RangeBoundary()`` renders ``'[)'``."""

    function = "TSTZRANGE"
    output_field = DateTimeRangeField()


def booking_period() -> TsTzRange:
    """The half-open interval [check_in, check_out) of a booking, as a SQL expression."""
    return TsTzRange("check_in", "check_out", RangeBoundary())


class Room(models.Model):
    class View(models.TextChoices):
        SEA = "sea", "Sea"
        MOUNTAIN = "mountain", "Mountain"
        YARD = "yard", "Yard"

    label = models.CharField(max_length=50, unique=True)
    number_of_beds = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)]
    )
    view = models.CharField(max_length=16, choices=View.choices)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(number_of_beds__gte=1, number_of_beds__lte=5),
                name="room_number_of_beds_between_1_and_5",
            ),
        ]
        indexes = [
            # Serves the room-selection query: equality on (beds, view), ordered by label.
            models.Index(
                fields=["number_of_beds", "view", "label"], name="room_beds_view_label_idx"
            ),
        ]

    def __str__(self) -> str:
        return self.label


class Booking(models.Model):
    # No user table exists (user management is out of scope), so the user is an opaque id.
    user_id = models.PositiveBigIntegerField()
    room = models.ForeignKey(Room, on_delete=models.PROTECT, related_name="bookings")
    check_in = models.DateTimeField()
    check_out = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(check_out__gt=F("check_in")),
                name="booking_check_out_after_check_in",
            ),
            # Two bookings of the same room may not overlap. Intervals are half-open
            # [check_in, check_out), so back-to-back bookings are allowed.
            # Backed by a GiST index on (room_id, tstzrange(check_in, check_out)).
            ExclusionConstraint(
                name=BOOKING_NO_OVERLAP_CONSTRAINT,
                expressions=[
                    ("room", RangeOperators.EQUAL),
                    (booking_period(), RangeOperators.OVERLAPS),
                ],
            ),
        ]
        indexes = [
            models.Index(fields=["user_id"], name="booking_user_idx"),
        ]

    def __str__(self) -> str:
        return f"Booking #{self.pk} room={self.room_id} [{self.check_in}, {self.check_out})"
