"""Authenticated job routes: submit, poll, list, active, cancel."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from . import job_runner, store
from .auth import AuthenticatedUser, get_current_user

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

Mode = Literal["debug", "explain", "health", "general"]


class CreateJobRequest(BaseModel):
    ingestion_id: str
    mode: Mode = "debug"
    message: str | None = None
    max_attempts: int = 3


@router.post("", status_code=status.HTTP_201_CREATED)
def create_job(
    body: CreateJobRequest, user: AuthenticatedUser = Depends(get_current_user)
) -> dict[str, str]:
    ingestion_id = (body.ingestion_id or "").strip()
    if not ingestion_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="ingestion_id is required.",
        )
    message = (body.message or "").strip() or None
    try:
        return job_runner.submit_job(
            user_id=user.id,
            ingestion_id=ingestion_id,
            mode=body.mode,
            message=message,
            max_attempts=body.max_attempts,
        )
    except job_runner.IngestionMissingError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc


@router.get("/active")
def get_active_job(
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    row = store.get_active_job(user.id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No active investigation."
        )
    messages = store.list_messages(row["investigation_id"], user.id, 50)
    return job_runner.to_detail(row, messages)


@router.get("")
def list_jobs(
    limit: int = Query(10, ge=1, le=100),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    return {
        "jobs": [job_runner.to_card(row) for row in store.list_jobs(user.id, limit)]
    }


@router.get("/{job_id}")
def get_job(
    job_id: str, user: AuthenticatedUser = Depends(get_current_user)
) -> dict[str, object]:
    row = store.get_job(job_id, user.id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Job not found."
        )
    messages = store.list_messages(row["investigation_id"], user.id, 50)
    return job_runner.to_detail(row, messages)


@router.post("/{job_id}/cancel")
def cancel_job(
    job_id: str, user: AuthenticatedUser = Depends(get_current_user)
) -> dict[str, object]:
    try:
        return job_runner.cancel_job(job_id, user.id)
    except job_runner.JobNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Job not found."
        ) from exc
