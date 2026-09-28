from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import select

from opa.api.actions_service import invalidate_actions
from opa.api.deps import ClockDep, DbDep, OwnerDep, get_owned
from opa.api.tasks_service import requeue, transition
from opa.broker.ops import get_tool
from opa.core.canonical import args_hash
from opa.core.errors import Conflict, Invalid, NotFound
from opa.db.models import Action, Approval, AuditLog, Connection, Task
from opa.db.outbox import emit
from opa.sdk.contract import ConnectorError

router = APIRouter(prefix="/api", tags=["approvals"])


class ApprovalOut(BaseModel):
    id: str
    state: str
    expires_at: datetime
    decided_at: datetime | None
    reason: str | None
    args_hash: str
    action_id: str
    action_state: str
    task_id: str
    task_intent: str
    account: str
    preview: dict[str, Any]
    effect: str


class DecisionIn(BaseModel):
    decision: Literal["allow_once", "deny"]
    expected_args_hash: str


class DecisionOut(BaseModel):
    approval: ApprovalOut
    outcome: str  # approved | denied | unchanged | expired | invalidated


class ActionOut(BaseModel):
    id: str
    task_id: str
    tool_name: str
    state: str
    preview: dict[str, Any]
    provider_refs: list[str]
    error: dict[str, Any] | None
    resolution: str | None
    dispatch_started_at: datetime | None


class ResolveIn(BaseModel):
    outcome: Literal["happened", "did_not_happen"]


async def _out(db, ap: Approval) -> ApprovalOut:
    action = await db.get(Action, ap.action_id)
    task = await db.get(Task, ap.task_id)
    conn = await db.get(Connection, action.connection_id)
    return ApprovalOut(id=ap.id, state=ap.state, expires_at=ap.expires_at, decided_at=ap.decided_at, reason=ap.reason, args_hash=ap.args_hash,
                       action_id=action.id, action_state=action.state, task_id=task.id, task_intent=task.intent, account=conn.account_label,
                       preview=action.preview, effect=action.effect)


def _action_out(a: Action) -> ActionOut:
    return ActionOut(id=a.id, task_id=a.task_id, tool_name=a.tool_name, state=a.state, preview=a.preview, provider_refs=a.provider_refs,
                     error=a.error, resolution=a.resolution, dispatch_started_at=a.dispatch_started_at)


@router.get("/approvals", response_model=list[ApprovalOut])
async def list_approvals(db: DbDep, owner: OwnerDep, state: str | None = "pending", limit: int = 100) -> list[ApprovalOut]:
    q = select(Approval).where(Approval.owner_id == owner.id)
    if state:
        q = q.where(Approval.state == state)
    rows = await db.scalars(q.order_by(Approval.created_at.desc()).limit(max(1, min(limit, 200))))
    return [await _out(db, a) for a in rows]


@router.get("/approvals/{approval_id}", response_model=ApprovalOut)
async def get_approval(approval_id: str, db: DbDep, owner: OwnerDep) -> ApprovalOut:
    return await _out(db, await get_owned(db, Approval, approval_id, owner))


@router.post("/approvals/{approval_id}/decision", response_model=DecisionOut)
async def decide(approval_id: str, body: DecisionIn, db: DbDep, owner: OwnerDep, clock: ClockDep) -> DecisionOut:
    """APP-01: only this authenticated human endpoint can grant approval, for one immutable action."""
    ap = await db.scalar(select(Approval).where(Approval.id == approval_id, Approval.owner_id == owner.id).with_for_update())
    if ap is None:
        raise NotFound("approval not found")
    action = await db.scalar(select(Action).where(Action.id == ap.action_id).with_for_update())
    if body.expected_args_hash != ap.args_hash:
        raise Conflict("this approval no longer matches what you reviewed; reload it", code="payload_changed")
    if ap.state != "pending":
        return DecisionOut(approval=await _out(db, ap), outcome="unchanged")  # idempotent replay
    now = clock.now()
    task = await db.get(Task, ap.task_id)

    async def invalidate(reason: str, code: str) -> DecisionOut:
        await invalidate_actions(db, [action], reason, clock, block_code=code)
        return DecisionOut(approval=await _out(db, ap), outcome="invalidated")

    if args_hash(action.tool_name, action.tool_version, action.canonical_args) != ap.args_hash or action.args_hash != ap.args_hash:
        return await invalidate("the payload changed after it was proposed", "payload_changed")
    if ap.expires_at <= now:
        ap.state, ap.decided_at = "expired", now
        action.state = "expired"
        if task.state == "waiting_approval":
            await transition(db, task, "blocked", clock, blocker_code="approval_expired", blocker_detail="the approval request expired")
        return DecisionOut(approval=await _out(db, ap), outcome="expired")
    if task.state != "waiting_approval" or task.checkpoint.get("pending_action", {}).get("action_id") != action.id:
        return await invalidate("the task is no longer waiting for this approval", "stale_approval")

    if body.decision == "deny":
        ap.state, ap.decided_at = "denied", now
        action.state = "denied"
        db.add(AuditLog(owner_id=owner.id, actor="owner", type="approval.denied", subject_id=action.id, sanitized={"tool": action.tool_name}))
        await emit(db, owner.id, "approval.decided", {"approval_id": ap.id, "decision": "deny"}, task_id=task.id, action_id=action.id)
        await transition(db, task, "blocked", clock, blocker_code="approval_denied", blocker_detail="you denied the proposed action; resume to let the assistant continue without it")
        return DecisionOut(approval=await _out(db, ap), outcome="denied")

    conn = await db.get(Connection, action.connection_id)
    capability = get_tool(action.tool_name).capability
    mode = conn.modes.get(capability, "off")
    if conn.status != "connected" or mode == "off" or (action.effect == "write" and mode != "read_write"):
        return await invalidate(f"{conn.account_label} no longer allows this action", "permission_changed")
    ap.state, ap.decided_at = "approved", now
    action.state = "approved"
    db.add(AuditLog(owner_id=owner.id, actor="owner", type="approval.granted", subject_id=action.id, sanitized={"tool": action.tool_name, "args_hash": ap.args_hash}))
    await emit(db, owner.id, "approval.decided", {"approval_id": ap.id, "decision": "allow_once"}, task_id=task.id, action_id=action.id)
    await requeue(db, task, clock)
    return DecisionOut(approval=await _out(db, ap), outcome="approved")


@router.get("/actions", response_model=list[ActionOut])
async def list_actions(db: DbDep, owner: OwnerDep, state: str | None = None, task_id: str | None = None, limit: int = 100) -> list[ActionOut]:
    q = select(Action).where(Action.owner_id == owner.id)
    if state:
        q = q.where(Action.state == state)
    if task_id:
        q = q.where(Action.task_id == task_id)
    return [_action_out(a) for a in await db.scalars(q.order_by(Action.created_at.desc()).limit(max(1, min(limit, 200))))]


@router.post("/actions/{action_id}/reconcile", response_model=ActionOut)
async def reconcile(action_id: str, request: Request, db: DbDep, owner: OwnerDep) -> ActionOut:
    action = await get_owned(db, Action, action_id, owner)
    broker = request.app.state.broker
    if broker is None:
        raise Invalid("the connector service is not configured", code="not_configured")
    try:
        await broker.reconcile(action.id, owner.id)
    except ConnectorError as e:
        raise Invalid(e.message, code=e.code) from None
    await db.refresh(action)
    return _action_out(action)


@router.post("/actions/{action_id}/resolve", response_model=ActionOut)
async def resolve(action_id: str, body: ResolveIn, db: DbDep, owner: OwnerDep) -> ActionOut:
    """Owner's manual answer for an unknown outcome (e.g. they checked their Sent folder). Never triggers a resend."""
    action = await db.scalar(select(Action).where(Action.id == action_id, Action.owner_id == owner.id).with_for_update())
    if action is None:
        raise NotFound("action not found")
    if action.state != "unknown":
        raise Conflict(f"action is {action.state}, not unknown", code="invalid_state")
    if body.outcome == "happened":
        action.state, action.resolution, action.result = "succeeded", "owner_confirmed", {"confirmed_by_owner": True}
    else:
        action.state, action.resolution = "failed", "owner_confirmed_not_applied"
        action.error = {"code": "not_applied", "message": "the owner confirmed this did not happen; a new attempt needs a new approval"}
    db.add(AuditLog(owner_id=owner.id, actor="owner", type="action.resolved", subject_id=action.id, sanitized={"outcome": body.outcome}))
    await emit(db, owner.id, f"action.{action.state}", {"resolution": action.resolution}, task_id=action.task_id, action_id=action.id)
    return _action_out(action)
