"""DATA-01: export and delete-all."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select

from opa.api.deps import ClockDep, DbDep, OwnerDep, SettingsDep, get_owned
from opa.core.errors import Invalid, NotFound
from opa.data.erase import erase_owner
from opa.data.export import build_export, export_path
from opa.db.models import Export

router = APIRouter(prefix="/api", tags=["data"])


class ExportOut(BaseModel):
    id: str
    file_name: str
    size: int
    sha256: str
    counts: dict[str, int]
    created_at: datetime


class DeleteAllIn(BaseModel):
    confirm: str  # the owner's email address, typed out


@router.post("/export", response_model=ExportOut, status_code=201)
async def create_export(db: DbDep, owner: OwnerDep, settings: SettingsDep, clock: ClockDep) -> ExportOut:
    return ExportOut.model_validate(await build_export(db, settings, clock, owner), from_attributes=True)


@router.get("/exports", response_model=list[ExportOut])
async def list_exports(db: DbDep, owner: OwnerDep) -> list[ExportOut]:
    rows = await db.scalars(select(Export).where(Export.owner_id == owner.id).order_by(Export.created_at.desc()))
    return [ExportOut.model_validate(r, from_attributes=True) for r in rows]


@router.get("/exports/{export_id}/download")
async def download_export(export_id: str, db: DbDep, owner: OwnerDep, settings: SettingsDep) -> FileResponse:
    row = await get_owned(db, Export, export_id, owner)
    path = export_path(settings, row)
    if not path.is_file():
        raise NotFound("export file not found")
    return FileResponse(path, media_type="application/zip", filename=row.file_name,
                        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})


@router.post("/delete-all")
async def delete_all(body: DeleteAllIn, request: Request, owner: OwnerDep, settings: SettingsDep, clock: ClockDep) -> dict[str, Any]:
    """Deletes everything except the account itself. Requires the owner's email address typed exactly."""
    if body.confirm.strip().lower() != owner.email.lower():
        raise Invalid("type your account email address exactly to confirm", code="confirmation_required")
    return await erase_owner(request.app.state.sessionmaker, settings, clock, owner.id, request.app.state.broker, request.app.state.browser)
