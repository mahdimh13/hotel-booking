import pytest

from hotel.models import Booking
from hotel.tests.conftest import at

URL = "/api/bookings/"

pytestmark = pytest.mark.django_db


def body(**overrides):
    return {
        "user_id": 7,
        "number_of_beds": 2,
        "view": "sea",
        "check_in": "2030-01-01T14:00:00Z",
        "check_out": "2030-01-03T11:00:00Z",
        **overrides,
    }


def test_successful_booking_returns_room_and_details(api, make_room):
    room = make_room(label="101", beds=2, view="sea")
    resp = api.post(URL, body(), format="json")
    assert resp.status_code == 201
    data = resp.json()
    assert data["user_id"] == 7
    assert data["room"] == {"id": room.id, "label": "101", "number_of_beds": 2, "view": "sea"}
    assert data["check_in"].startswith("2030-01-01T14:00:00")
    assert data["check_out"].startswith("2030-01-03T11:00:00")
    assert Booking.objects.count() == 1


def test_no_matching_room_is_409(api, make_room):
    make_room(beds=1, view="sea")
    resp = api.post(URL, body(), format="json")
    assert resp.status_code == 409
    assert resp.json()["code"] == "no_room_available"


def test_overlap_is_409_and_adjacent_is_201(api, make_room):
    make_room()
    assert api.post(URL, body(), format="json").status_code == 201
    overlapping = body(check_in="2030-01-02T00:00:00Z", check_out="2030-01-04T00:00:00Z")
    assert api.post(URL, overlapping, format="json").status_code == 409
    back_to_back = body(check_in="2030-01-03T11:00:00Z", check_out="2030-01-05T11:00:00Z")
    assert api.post(URL, back_to_back, format="json").status_code == 201


def test_offsets_are_normalised(api, make_room):
    make_room()
    # 14:00+02:00 == 12:00Z
    resp = api.post(
        URL,
        body(check_in="2030-01-01T14:00:00+02:00", check_out="2030-01-01T20:00:00+02:00"),
        format="json",
    )
    assert resp.status_code == 201
    assert Booking.objects.get().check_in == at(1, 12)


@pytest.mark.parametrize(
    "overrides",
    [
        {"check_out": "2030-01-01T14:00:00Z"},  # zero length
        {"check_out": "2029-12-31T00:00:00Z"},  # before check-in
        {"check_in": "not-a-date"},
        {"number_of_beds": 0},
        {"number_of_beds": 6},
        {"view": "garden"},
        {"user_id": 0},
        {"user_id": "abc"},
    ],
)
def test_invalid_input_is_400(api, make_room, overrides):
    make_room()
    assert api.post(URL, body(**overrides), format="json").status_code == 400
    assert Booking.objects.count() == 0


def test_missing_fields_is_400(api):
    resp = api.post(URL, {}, format="json")
    assert resp.status_code == 400
    assert set(resp.json()) == {"user_id", "number_of_beds", "view", "check_in", "check_out"}


def test_retrieve_and_list_with_filters_and_constant_query_count(
    api, make_room, django_assert_max_num_queries
):
    for _ in range(5):
        make_room()
    for user in (1, 1, 2):
        api.post(URL, body(user_id=user), format="json")
    booking_id = Booking.objects.first().id

    assert api.get(f"{URL}{booking_id}/").status_code == 200
    assert api.get(f"{URL}999999/").status_code == 404
    with django_assert_max_num_queries(2):  # count + one joined select, independent of rows
        resp = api.get(URL)
    assert resp.json()["count"] == 3
    assert api.get(URL, {"user_id": 1}).json()["count"] == 2


def test_bookings_are_read_only_after_creation(api, make_room):
    make_room()
    booking_id = api.post(URL, body(), format="json").json()["id"]
    assert api.put(f"{URL}{booking_id}/", body(), format="json").status_code == 405
    assert api.delete(f"{URL}{booking_id}/").status_code == 405
