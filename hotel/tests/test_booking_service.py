import pytest

from hotel.models import Booking
from hotel.services import (
    BookingRequest,
    InvalidBookingPeriodError,
    NoRoomAvailableError,
    create_booking,
)
from hotel.tests.conftest import at

pytestmark = pytest.mark.django_db


def req(ci, co, beds=2, view="sea", user=1):
    return BookingRequest(user, beds, view, ci, co)


def test_books_matching_room(make_room):
    room = make_room(beds=2, view="sea")
    booking = create_booking(req(at(1), at(3), user=42))
    assert booking.room == room
    assert (booking.user_id, booking.check_in, booking.check_out) == (42, at(1), at(3))


def test_no_room_with_requested_features(make_room):
    make_room(beds=2, view="sea")
    with pytest.raises(NoRoomAvailableError):
        create_booking(req(at(1), at(3), beds=3))
    with pytest.raises(NoRoomAvailableError):
        create_booking(req(at(1), at(3), view="yard"))
    assert Booking.objects.count() == 0


def test_no_rooms_at_all():
    with pytest.raises(NoRoomAvailableError):
        create_booking(req(at(1), at(2)))


@pytest.mark.parametrize(
    ("ci", "co"),
    [
        (at(5), at(8)),  # identical
        (at(4), at(6)),  # overlaps the start
        (at(7), at(9)),  # overlaps the end
        (at(6), at(7)),  # strictly inside
        (at(3), at(10)),  # fully contains
        (at(5, 0), at(5, 1)),  # one hour inside the first day
        (at(7, 23), at(9)),  # overlaps by the last hour
    ],
)
def test_overlapping_request_rejected(make_room, ci, co):
    make_room()
    create_booking(req(at(5), at(8)))
    with pytest.raises(NoRoomAvailableError):
        create_booking(req(ci, co))
    assert Booking.objects.count() == 1


@pytest.mark.parametrize(
    ("ci", "co"),
    [
        (at(2), at(5)),  # ends exactly when the existing one starts
        (at(8), at(10)),  # starts exactly when the existing one ends
        (at(1), at(4, 23)),  # ends one hour before
        (at(8, 1), at(9)),  # starts one hour after
    ],
)
def test_adjacent_requests_allowed(make_room, ci, co):
    make_room()
    create_booking(req(at(5), at(8)))
    assert create_booking(req(ci, co)).pk
    assert Booking.objects.count() == 2


@pytest.mark.parametrize("co_offset", [0, -1])
def test_invalid_period_rejected(make_room, co_offset):
    make_room()
    with pytest.raises(InvalidBookingPeriodError):
        create_booking(req(at(5), at(5 + co_offset)))


def test_picks_lowest_label_deterministically(make_room):
    make_room(label="C")
    make_room(label="A")
    make_room(label="B")
    assert create_booking(req(at(1), at(2))).room.label == "A"
    assert create_booking(req(at(1), at(2))).room.label == "B"
    assert create_booking(req(at(1), at(2))).room.label == "C"
    with pytest.raises(NoRoomAvailableError):
        create_booking(req(at(1), at(2)))


def test_skips_room_booked_for_that_period_and_uses_next(make_room):
    a, b = make_room(label="A"), make_room(label="B")
    create_booking(req(at(1), at(5)))  # A
    assert create_booking(req(at(3), at(4))).room == b
    # A is free again after day 5
    assert create_booking(req(at(5), at(6))).room == a


def test_other_features_never_selected(make_room):
    make_room(label="A", beds=1, view="sea")
    make_room(label="B", beds=2, view="yard")
    target = make_room(label="Z", beds=2, view="sea")
    assert create_booking(req(at(1), at(2))).room == target


def test_overlap_condition_matches_explicit_comparison(make_room):
    """The range-operator query must equal `e.in < r.out AND e.out > r.in` everywhere."""
    from hotel.services import _overlapping_bookings

    room = make_room()
    existing = [(at(5), at(8)), (at(10), at(11))]
    for ci, co in existing:
        Booking.objects.create(user_id=1, room=room, check_in=ci, check_out=co)

    hours = [at(d, h) for d in range(3, 14) for h in (0, 12)]
    for r_in in hours:
        for r_out in hours:
            if r_out <= r_in:
                continue
            expected = {
                b.pk for b in Booking.objects.filter(check_in__lt=r_out, check_out__gt=r_in)
            }
            actual = set(_overlapping_bookings(r_in, r_out).values_list("pk", flat=True))
            assert actual == expected, (r_in, r_out)
