from typing import Any

from fastapi import APIRouter, Depends, status

from app.routers.deps import get_active_organization
from app.schemas.files import FileBaseResponse
from app.schemas.uploads import PresignedUploadRequest, PresignedUploadResponse, UploadConfirmRequest
from app.services.uploads import UploadsService
from app.utils.uuid_compat import as_uuid

router = APIRouter()


@router.post(
    "/request-url",
    response_model=PresignedUploadResponse,
    status_code=status.HTTP_200_OK,
)
def request_presigned_upload_url(
    body: PresignedUploadRequest,
    active_organization: Any = Depends(get_active_organization),
):
    """
    Issue S3 presigned POST policy and a pending `files` row for direct browser upload.

    Full path: ``/api/v1/uploads/request-url``.
    """
    svc = UploadsService()
    data = svc.request_presigned_post(
        filename=body.filename,
        content_type=body.content_type,
        user_id=as_uuid(active_organization["member_user_id"]),
        org_id=as_uuid(active_organization["id"]),
    )
    return PresignedUploadResponse(**data)


@router.post(
    "/confirm",
    response_model=FileBaseResponse,
    status_code=status.HTTP_200_OK,
)
def confirm_workspace_upload(
    body: UploadConfirmRequest,
    active_organization: Any = Depends(get_active_organization),
):
    """
    Finalize a direct S3 upload: verify the object exists and update the `files` row.
    """
    svc = UploadsService()
    return svc.confirm_upload(
        file_id=body.file_id,
        user_id=as_uuid(active_organization["member_user_id"]),
        org_id=as_uuid(active_organization["id"]),
    )
