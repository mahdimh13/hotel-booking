"""HTTP layer: parse, call the service, map domain errors to status codes."""

from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.exceptions import APIException
from rest_framework.response import Response

from . import services
from .models import Booking, Room
from .serializers import BookingCreateSerializer, BookingSerializer, RoomSerializer


class Conflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "conflict"


class RoomViewSet(viewsets.ModelViewSet):
    queryset = Room.objects.order_by("id")
    serializer_class = RoomSerializer
    filterset_fields = ["number_of_beds", "view"]

    def perform_create(self, serializer) -> None:
        self._save(serializer)

    def perform_update(self, serializer) -> None:
        self._save(serializer)

    @staticmethod
    def _save(serializer) -> None:
        # The serializer's uniqueness check is check-then-insert, so two simultaneous
        # requests can both pass it; the UNIQUE constraint then decides. Report that as a
        # normal validation error instead of a 500.
        try:
            with transaction.atomic():
                serializer.save()
        except IntegrityError as exc:
            raise serializers.ValidationError(
                {"label": ["room with this label already exists."]}
            ) from exc

    def perform_destroy(self, instance: Room) -> None:
        try:
            instance.delete()
        except ProtectedError as exc:
            raise Conflict(
                {"detail": "Room has bookings and cannot be deleted.", "code": "room_has_bookings"}
            ) from exc


class BookingViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    queryset = Booking.objects.select_related("room").order_by("id")
    filterset_fields = ["user_id", "room"]

    def get_serializer_class(self):
        return BookingCreateSerializer if self.action == "create" else BookingSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            booking = services.create_booking(services.BookingRequest(**serializer.validated_data))
        except services.NoRoomAvailableError as exc:
            raise Conflict({"detail": str(exc), "code": "no_room_available"}) from exc
        except services.BookingConflictError as exc:
            raise Conflict({"detail": str(exc), "code": "booking_conflict"}) from exc
        return Response(BookingSerializer(booking).data, status=status.HTTP_201_CREATED)
