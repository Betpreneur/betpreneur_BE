from rest_framework import status, viewsets
from rest_framework.response import Response

from .models import AlgoRun
from .serializers import AlgoRunCreateSerializer, AlgoRunSerializer
from .services import algo_runner_service


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
