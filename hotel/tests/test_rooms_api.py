import pytest

from hotel.models import Room
from hotel.services import BookingRequest, create_booking
from hotel.tests.conftest import at

LIST_URL = "/api/rooms/"


def payload(**overrides):
    return {"label": "101", "number_of_beds": 2, "view": "sea", **overrides}


@pytest.mark.django_db
class TestRoomCreate:
    def test_creates_room(self, api):
        resp = api.post(LIST_URL, payload(), format="json")
        assert resp.status_code == 201
        assert resp.json() == {
            "id": resp.json()["id"],
            "label": "101",
            "number_of_beds": 2,
            "view": "sea",
        }
        assert Room.objects.count() == 1

    def test_duplicate_label_rejected(self, api, make_room):
        make_room(label="101")
        resp = api.post(LIST_URL, payload(label="101"), format="json")
        assert resp.status_code == 400
        assert "label" in resp.json()
        assert Room.objects.count() == 1

    def test_duplicate_label_race_is_400_not_500(self, api, make_room, monkeypatch):
        """Lose the validator/insert race: the DB UNIQUE error must still become a 400."""
        from rest_framework.validators import UniqueValidator

        make_room(label="101")
        monkeypatch.setattr(UniqueValidator, "__call__", lambda self, value, serializer_field: None)
        resp = api.post(LIST_URL, payload(label="101"), format="json")
        assert resp.status_code == 400
        assert "label" in resp.json()
        assert Room.objects.count() == 1

    @pytest.mark.parametrize("beds", [0, 6, -1, 100])
    def test_bed_count_out_of_range_rejected(self, api, beds):
        resp = api.post(LIST_URL, payload(number_of_beds=beds), format="json")
        assert resp.status_code == 400
        assert "number_of_beds" in resp.json()

    @pytest.mark.parametrize("beds", [1, 5])
    def test_bed_count_boundaries_accepted(self, api, beds):
        assert api.post(LIST_URL, payload(number_of_beds=beds), format="json").status_code == 201

    @pytest.mark.parametrize("view", ["garden", "", "Sea", "SEA"])
    def test_invalid_view_rejected(self, api, view):
        resp = api.post(LIST_URL, payload(view=view), format="json")
        assert resp.status_code == 400
        assert "view" in resp.json()

    def test_non_integer_beds_rejected(self, api):
        assert api.post(LIST_URL, payload(number_of_beds="two"), format="json").status_code == 400

    def test_missing_fields_rejected(self, api):
        resp = api.post(LIST_URL, {}, format="json")
        assert resp.status_code == 400
        assert set(resp.json()) == {"label", "number_of_beds", "view"}


@pytest.mark.django_db
class TestRoomReadUpdateDelete:
    def test_retrieve(self, api, make_room):
        room = make_room(label="A1", beds=3, view="yard")
        resp = api.get(f"{LIST_URL}{room.id}/")
        assert resp.status_code == 200
        assert resp.json()["label"] == "A1"

    def test_retrieve_missing_is_404(self, api):
        assert api.get(f"{LIST_URL}999999/").status_code == 404

    def test_list_is_paginated_and_filterable(self, api, make_room):
        make_room(beds=1, view="sea")
        make_room(beds=2, view="sea")
        make_room(beds=2, view="yard")
        resp = api.get(LIST_URL)
        assert resp.json()["count"] == 3
        assert set(resp.json()) == {"count", "next", "previous", "results"}
        resp = api.get(LIST_URL, {"number_of_beds": 2, "view": "sea"})
        assert resp.json()["count"] == 1

    def test_put_updates_room(self, api, make_room):
        room = make_room(label="A1", beds=1, view="sea")
        resp = api.put(
            f"{LIST_URL}{room.id}/",
            payload(label="A1", number_of_beds=4, view="yard"),
            format="json",
        )
        assert resp.status_code == 200
        room.refresh_from_db()
        assert (room.number_of_beds, room.view) == (4, "yard")

    def test_patch_keeps_own_label_but_rejects_another_rooms_label(self, api, make_room):
        a, b = make_room(label="A"), make_room(label="B")
        assert (
            api.patch(
                f"{LIST_URL}{a.id}/", {"label": "A", "number_of_beds": 3}, format="json"
            ).status_code
            == 200
        )
        assert api.patch(f"{LIST_URL}{a.id}/", {"label": "B"}, format="json").status_code == 400
        b.refresh_from_db()
        assert b.label == "B"

    def test_update_validates(self, api, make_room):
        room = make_room()
        assert (
            api.patch(f"{LIST_URL}{room.id}/", {"number_of_beds": 9}, format="json").status_code
            == 400
        )

    def test_delete(self, api, make_room):
        room = make_room()
        assert api.delete(f"{LIST_URL}{room.id}/").status_code == 204
        assert not Room.objects.filter(pk=room.pk).exists()

    def test_delete_room_with_bookings_is_409(self, api, make_room):
        room = make_room()
        create_booking(BookingRequest(1, room.number_of_beds, room.view, at(1), at(2)))
        resp = api.delete(f"{LIST_URL}{room.id}/")
        assert resp.status_code == 409
        assert resp.json()["code"] == "room_has_bookings"
        assert Room.objects.filter(pk=room.pk).exists()
