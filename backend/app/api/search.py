"""Historical search: the two match lanes, kept separate."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import CurrentUser, get_current_user
from app.models import Part
from app.schemas import MatchOut, SimilarOut
from app.services.history import geometry_matches, problem_matches

router = APIRouter(tags=["search"])


@router.get("/search/similar", response_model=SimilarOut)
def search_similar(
    part_id: int = Query(...),
    limit: int = Query(default=5, ge=1, le=25),
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
):
    """Geometry and problem lanes for one part.

    The lanes are returned separately rather than merged into a single
    ranking: "we have cut this shape before" and "this kind of job has hurt us
    before" are different claims and an estimator acts on them differently.
    """
    part = db.get(Part, part_id)
    if part is None:
        raise HTTPException(status_code=404, detail=f"Part {part_id} not found")
    return SimilarOut(
        geometry=[MatchOut(**m.as_dict()) for m in geometry_matches(db, part, limit=limit)],
        problem=[MatchOut(**m.as_dict()) for m in problem_matches(db, part, limit=limit)],
    )
