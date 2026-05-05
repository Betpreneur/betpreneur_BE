from drf_spectacular.utils import extend_schema
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import serializers


class HealthSerializer(serializers.Serializer):
    status = serializers.CharField()


@extend_schema(
    summary="Health check",
    description="Check if the API is running",
    tags=["Health"],
    responses={200: HealthSerializer},
)
class HealthCheckView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({"status": "ok"})