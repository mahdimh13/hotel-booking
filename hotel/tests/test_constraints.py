"""The database must refuse double bookings on its own, without help from services.py."""

import pytest
from django.db import IntegrityError, transaction

from hotel.models import Booking, Room
from hotel.tests.conftest import at

pytestmark = pytest.mark.django_db


def insert(room, ci, co):
    with transaction.atomic():
        return Booking.objects.create(user_id=1, room=room, check_in=ci, check_out=co)


def test_overlap_rejected_by_exclusion_constraint(make_room):
    room = make_room()
    insert(room, at(1), at(5))
    for ci, co in [(at(1), at(5)), (at(2), at(3)), (at(4), at(9)), (at(0), at(2))]:
        with pytest.raises(IntegrityError, match="booking_no_overlap_per_room"):
            insert(room, ci, co)


def test_adjacent_bookings_allowed_by_constraint(make_room):
    room = make_room()
    insert(room, at(1), at(5))
    insert(room, at(5), at(7))
    insert(room, at(0), at(1))
    assert Booking.objects.count() == 3


def test_same_period_in_different_rooms_allowed(make_room):
    insert(make_room(), at(1), at(5))
    insert(make_room(), at(1), at(5))


@pytest.mark.parametrize("co", [0, -1])
def test_check_out_must_be_after_check_in(make_room, co):
    with pytest.raises(IntegrityError, match="booking_check_out_after_check_in"):
        insert(make_room(), at(5), at(5 + co))


def test_room_db_constraints(db):
    for beds in (0, 6):
        with pytest.raises(IntegrityError), transaction.atomic():
            Room.objects.create(label=f"x{beds}", number_of_beds=beds, view="sea")
    Room.objects.create(label="dup", number_of_beds=1, view="sea")
    with pytest.raises(IntegrityError), transaction.atomic():
        Room.objects.create(label="dup", number_of_beds=2, view="yard")
