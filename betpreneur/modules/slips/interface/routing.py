from django.urls import re_path

from .consumers import SlipReviewConsumer

websocket_urlpatterns = [
    re_path(r"^ws/slip-reviews/(?P<review_id>\d+)/$", SlipReviewConsumer.as_asgi()),
]
