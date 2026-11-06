"""Input/output validation only; no business logic."""

from rest_framework import serializers

from .models import Booking, Room


class RoomSerializer(serializers.ModelSerializer):
    class Meta:
        model = Room
        fields = ["id", "label", "number_of_beds", "view"]


class BookingCreateSerializer(serializers.Serializer):
    user_id = serializers.IntegerField(min_value=1)
    number_of_beds = serializers.IntegerField(min_value=1, max_value=5)
    view = serializers.ChoiceField(choices=Room.View.choices)
    check_in = serializers.DateTimeField()
    check_out = serializers.DateTimeField()

    def validate(self, attrs: dict) -> dict:
        if attrs["check_out"] <= attrs["check_in"]:
            raise serializers.ValidationError({"check_out": "Must be after check_in."})
        return attrs


class BookingSerializer(serializers.ModelSerializer):
    room = RoomSerializer(read_only=True)

    class Meta:
        model = Booking
        fields = ["id", "user_id", "room", "check_in", "check_out", "created_at"]
