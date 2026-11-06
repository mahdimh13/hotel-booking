from datetime import UTC, datetime, timedelta

import pytest
from rest_framework.test import APIClient

from hotel.models import Room


def at(day: int, hour: int = 0) -> datetime:
    """Timezone-aware datetime: day N of January 2030, at the given hour (UTC)."""
    return datetime(2030, 1, 1, hour, tzinfo=UTC) + timedelta(days=day - 1)


@pytest.fixture
def api() -> APIClient:
    return APIClient()


@pytest.fixture
def make_room(db):
    counter = {"n": 0}

    def _make(label: str | None = None, beds: int = 2, view: str = "sea") -> Room:
        counter["n"] += 1
        return Room.objects.create(
            label=label or f"R{counter['n']:03d}", number_of_beds=beds, view=view
        )

    return _make
