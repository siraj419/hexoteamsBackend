from fastapi import APIRouter, Depends, status
from pydantic import BaseModel

from app.routers.deps import get_current_user
from app.core.config import Settings

router = APIRouter()
settings = Settings()


class CommentDepthResponse(BaseModel):
    max_comment_reply_depth: int


class SubtaskDepthResponse(BaseModel):
    max_subtask_depth: int


@router.get('/comment-depth', response_model=CommentDepthResponse, status_code=status.HTTP_200_OK)
def get_comment_depth(
    user: any = Depends(get_current_user),
):
    """
    Get the maximum allowed comment reply depth.
    
    This endpoint returns the configured maximum depth for comment replies.
    Only authenticated users can access this endpoint.
    
    Returns:
        CommentDepthResponse with max_comment_reply_depth value
    """
    return CommentDepthResponse(
        max_comment_reply_depth=settings.MAX_COMMENT_REPLY_DEPTH
    )


@router.get('/subtask-depth', response_model=SubtaskDepthResponse, status_code=status.HTTP_200_OK)
def get_subtask_depth(
    user: any = Depends(get_current_user),
):
    """
    Get the maximum allowed subtask depth.
    
    This endpoint returns the configured maximum depth for task subtasks.
    Only authenticated users can access this endpoint.
    
    Returns:
        SubtaskDepthResponse with max_subtask_depth value
    """
    return SubtaskDepthResponse(
        max_subtask_depth=settings.MAX_SUBTASK_DEPTH
    )

