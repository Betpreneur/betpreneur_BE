from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import status, viewsets
from rest_framework.response import Response

from .models import AlgoRun
from .serializers import AlgoRunCreateSerializer, AlgoRunSerializer
from .services import algo_runner_service


@extend_schema_view(
    list=extend_schema(
        summary="List algo runs",
        description="Get all algorithm execution records",
        tags=["Algo"],
    ),
    retrieve=extend_schema(
        summary="Get algo run",
        description="Get a specific algo run by ID",
        tags=["Algo"],
    ),
    create=extend_schema(
        summary="Run algo",
        description="""
        Execute the betting algorithm for a target date.
        
        **Optional payload:**
        ```json
        {
          "target_date": "2026-05-04"
        }
        ```
        
        If no target_date is provided, runs for today.
        """,
        tags=["Algo"],
        request=AlgoRunCreateSerializer,
        responses={201: AlgoRunSerializer},
    ),
)
class AlgoRunViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = AlgoRun.objects.prefetch_related("picks").all()
    serializer_class = AlgoRunSerializer

    def create(self, request):
        serializer = AlgoRunCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        target_date = serializer.validated_data.get("target_date")
        algo_run = algo_runner_service.create_run(user=request.user, target_date=target_date)
        algo_run = algo_runner_service.run(algo_run)
        return Response(AlgoRunSerializer(algo_run).data, status=status.HTTP_201_CREATED)