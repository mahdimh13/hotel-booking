from rest_framework.routers import SimpleRouter

from .views import BookingViewSet, RoomViewSet

router = SimpleRouter()
router.register("rooms", RoomViewSet, basename="room")
router.register("bookings", BookingViewSet, basename="booking")

urlpatterns = router.urls
