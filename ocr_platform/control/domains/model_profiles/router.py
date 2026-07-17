from __future__ import annotations

from typing import Callable, Generator

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ...schemas import ModelProfileRequest, ModelProfileResponse
from . import commands, queries
from .schemas import model_profile_to_response


GetDb = Callable[[], Generator[Session, None, None]]


def create_router(get_db: GetDb) -> APIRouter:
    router = APIRouter()

    @router.get("/api/model-profiles", response_model=list[ModelProfileResponse])
    def api_list_model_profiles(session: Session = Depends(get_db)):
        return [model_profile_to_response(profile) for profile in queries.list_model_profiles(session)]

    @router.put("/api/model-profiles/{profile_id}", response_model=ModelProfileResponse)
    def api_upsert_model_profile(
        profile_id: str,
        request: ModelProfileRequest,
        session: Session = Depends(get_db),
    ):
        try:
            return model_profile_to_response(commands.upsert_model_profile(session, profile_id, request))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router
