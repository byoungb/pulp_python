import os
from gettext import gettext as _
import pkginfo
import shutil
import tempfile

from django.db import transaction
from drf_yasg.utils import swagger_auto_schema
from rest_framework import decorators, status, serializers
from rest_framework.response import Response

from pulpcore.plugin import viewsets as platform
from pulpcore.plugin.models import Artifact, RepositoryVersion
from pulpcore.plugin.serializers import (
    AsyncOperationResponseSerializer,
    RepositoryPublishURLSerializer,
    RepositorySyncURLSerializer,
)
from pulpcore.plugin.tasking import enqueue_with_reservation

from pulp_python.app import models as python_models
from pulp_python.app import serializers as python_serializers
from pulp_python.app import tasks
from pulp_python.app.utils import parse_project_metadata

DIST_EXTENSIONS = {
    ".whl": "bdist_wheel",
    ".exe": "bdist_wininst",
    ".egg": "bdist_egg",
    ".tar.bz2": "sdist",
    ".tar.gz": "sdist",
    ".zip": "sdist",
}

DIST_TYPES = {
    "bdist_wheel": pkginfo.Wheel,
    "bdist_wininst": pkginfo.Distribution,
    "bdist_egg": pkginfo.BDist,
    "sdist": pkginfo.SDist,
}


class PythonPackageContentFilter(platform.ContentFilter):
    """
    FilterSet for PythonPackageContent.
    """

    class Meta:
        model = python_models.PythonPackageContent
        fields = {
            'type': ['exact', 'in'],
            'name': ['exact', 'in'],
            'author': ['exact', 'in'],
            'packagetype': ['exact', 'in'],
            'filename': ['exact', 'in', 'contains'],
            'keywords': ['in', 'contains'],
        }


class PythonPackageContentViewSet(platform.ContentViewSet):
    """
    The ViewSet for PythonPackageContent.
    """

    endpoint_name = 'python/packages'
    queryset = python_models.PythonPackageContent.objects.all()
    serializer_class = python_serializers.PythonPackageContentSerializer
    minimal_serializer_class = python_serializers.MinimalPythonPackageContentSerializer
    filterset_class = PythonPackageContentFilter

    @transaction.atomic
    def create(self, request):
        """
        Create a new PythonPackageContent from a request.
        """
        try:
            artifact = self.get_resource(request.data['artifact'], Artifact)
        except KeyError:
            raise serializers.ValidationError(detail={'artifact': _('This field is required')})

        try:
            filename = request.data['filename']
        except KeyError:
            raise serializers.ValidationError(detail={'filename': _('This field is required')})

        # iterate through extensions since splitext does not support things like .tar.gz
        for ext, packagetype in DIST_EXTENSIONS.items():
            if filename.endswith(ext):

                # Copy file to a temp directory under the user provided filename, we do this
                # because pkginfo validates that the filename has a valid extension before
                # reading it
                with tempfile.TemporaryDirectory() as td:
                    temp_path = os.path.join(td, filename)
                    shutil.copy2(artifact.file.path, temp_path)
                    metadata = DIST_TYPES[packagetype](temp_path)
                    metadata.packagetype = packagetype
                    break
        else:
            raise serializers.ValidationError(_(
                "Extension on {} is not a valid python extension "
                "(.whl, .exe, .egg, .tar.gz, .tar.bz2, .zip)").format(filename)
            )

        data = parse_project_metadata(vars(metadata))
        data['classifiers'] = [{'name': classifier} for classifier in metadata.classifiers]
        data['packagetype'] = metadata.packagetype
        data['version'] = metadata.version
        data['filename'] = filename
        data['artifact'] = request.data['artifact']

        serializer = self.get_serializer(data=data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)

        headers = self.get_success_headers(request.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)


class PythonRemoteFilter(platform.BaseFilterSet):
    """
    FilterSet for PythonRemote.
    """

    class Meta:
        model = python_models.PythonRemote
        fields = {
            'name': ['exact', 'in'],
            'last_updated': ['lt', 'lte', 'gt', 'gte', 'range'],
        }


class PythonRemoteViewSet(platform.RemoteViewSet):
    """
    A ViewSet for PythonRemote.
    """

    endpoint_name = 'python'
    queryset = python_models.PythonRemote.objects.all()
    serializer_class = python_serializers.PythonRemoteSerializer
    filterset_class = PythonRemoteFilter

    @swagger_auto_schema(
        operation_description="Trigger an asynchronous task to sync python content.",
        responses={202: AsyncOperationResponseSerializer}
    )
    @decorators.detail_route(methods=('post',), serializer_class=RepositorySyncURLSerializer)
    def sync(self, request, pk):
        """
        Dispatches a sync task.
        """
        remote = self.get_object()
        serializer = RepositorySyncURLSerializer(
            data=request.data,
            context={'request': request}
        )
        serializer.is_valid(raise_exception=True)
        repository = serializer.validated_data.get('repository')
        mirror = serializer.validated_data.get('mirror')

        result = enqueue_with_reservation(
            tasks.sync,
            [repository, remote],
            kwargs={
                'remote_pk': remote.pk,
                'repository_pk': repository.pk,
                'mirror': mirror
            }
        )
        return platform.OperationPostponedResponse(result, request)


class PythonPublisherViewSet(platform.PublisherViewSet):
    """
    A ViewSet for PythonPublisher.
    """

    endpoint_name = 'python'
    queryset = python_models.PythonPublisher.objects.all()
    serializer_class = python_serializers.PythonPublisherSerializer

    @swagger_auto_schema(
        operation_description="Trigger an asynchronous task to publish python content.",
        responses={202: AsyncOperationResponseSerializer}
    )
    @decorators.detail_route(methods=('post',), serializer_class=RepositoryPublishURLSerializer)
    def publish(self, request, pk):
        """
        Dispatches a publish task.
        """
        publisher = self.get_object()
        serializer = RepositoryPublishURLSerializer(
            data=request.data,
            context={'request': request}
        )
        serializer.is_valid(raise_exception=True)
        repository_version = serializer.validated_data.get('repository_version')

        # Safe because version OR repository is enforced by serializer.
        if not repository_version:
            repository = serializer.validated_data.get('repository')
            repository_version = RepositoryVersion.latest(repository)

        result = enqueue_with_reservation(
            tasks.publish,
            [repository_version.repository, publisher],
            kwargs={
                'publisher_pk': publisher.pk,
                'repository_version_pk': repository_version.pk
            }
        )
        return platform.OperationPostponedResponse(result, request)
