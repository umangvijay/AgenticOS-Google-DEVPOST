from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, ConfigDict
from backend.api.dependencies.auth import AuthenticatedUser, require_admin
from backend.security.rate_limiter import check_rate_limit

router = APIRouter(prefix="/blogs", tags=["blogs"])


class BlogPost(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=200)
    category: str = Field(min_length=1, max_length=80)
    excerpt: str = Field(min_length=1, max_length=4000)


@router.get("")
async def list_posts(request: Request):
    return await request.app.state.factory.content_repo.list_public_posts()


@router.post("", status_code=201)
async def publish_post(body: BlogPost, request: Request, user: AuthenticatedUser = Depends(require_admin)):
    check_rate_limit(user.user_id, "general")
    return await request.app.state.factory.content_repo.create("blog", {
        **body.model_dump(), "author_id": user.user_id,
        "date": datetime.now(timezone.utc).strftime("%B %d, %Y"),
    })
