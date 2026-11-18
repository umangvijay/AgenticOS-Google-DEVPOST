"""Recovery episode API (Phase 11 §11.17).

Authorized retrieval of recovery episodes with their evidence, proposed
repairs, policy results, and applicable next actions.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from backend.api.dependencies.auth import AuthenticatedUser, get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/recovery", tags=["recovery"])


def _repo(request: Request):
    factory = getattr(request.app.state, "factory", None)
    repo = getattr(factory, "recovery_episode_repo", None) if factory else None
    if repo is None:
        raise HTTPException(status_code=503, detail={
            "code": "RECOVERY_UNAVAILABLE",
            "message": "Recovery episodes are not configured on this deployment"})
    return repo


@router.get("/episodes/{episode_id}")
async def get_episode(episode_id: str, request: Request,
                      user: AuthenticatedUser = Depends(get_current_user)):
    repo = _repo(request)
    episode = await repo.get_episode(episode_id)
    if not episode:
        raise HTTPException(status_code=404, detail="Episode not found")
    if episode.get("tenant_id") != user.user_id and not user.is_admin():
        raise HTTPException(status_code=403, detail="Access denied")
    return {
        "episode_id": episode["episode_id"],
        "state": episode.get("state"),
        "state_version": episode.get("state_version"),
        "failure_code": episode.get("failure_code"),
        "failure_category": episode.get("failure_category"),
        "proposal_action": episode.get("proposal_action"),
        "proposal_explanation": episode.get("proposal_explanation"),
        "policy_result": episode.get("policy_result"),
        "policy_reason": episode.get("policy_reason"),
        "attempt_count": episode.get("attempt_count"),
        "deadline_at": episode.get("deadline_at"),
        "verification_verdict": episode.get("verification_verdict"),
        "evidence_references": episode.get("evidence_references_json"),
        "created_at": episode.get("created_at"),
        "updated_at": episode.get("updated_at"),
    }


@router.get("/runs/{run_id}/episodes")
async def list_episodes_for_run(run_id: str, request: Request,
                                user: AuthenticatedUser = Depends(get_current_user)):
    repo = _repo(request)
    episodes = await repo.db.fetch_all(
        "SELECT * FROM recovery_episodes WHERE run_id=? ORDER BY created_at DESC LIMIT 20",
        (run_id,))
    result = []
    for ep in episodes:
        if ep["tenant_id"] != user.user_id and not user.is_admin():
            continue
        result.append({
            "episode_id": ep["episode_id"],
            "state": ep["state"],
            "task_id": ep["task_id"],
            "failure_category": ep["failure_category"],
            "attempt_count": ep["attempt_count"],
            "created_at": ep["created_at"],
        })
    return {"run_id": run_id, "episodes": result, "count": len(result)}
