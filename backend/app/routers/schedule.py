"""调度编排接口：四阶段推进、冲突裁决与改期、批次启动、跨日恢复、工作台待确认。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.schedule import (
    DEFAULT_WINDOW_MINUTES,
    ScheduleService,
)

router = APIRouter(prefix="/api/schedule", tags=["调度编排"])

service = ScheduleService()

# 四阶段：草稿 → 待校验 → 已启用 → 待确认，只允许顺向推进。
STATUSES = ["草稿", "待校验", "已启用", "待确认"]


class ArbitratePayload(BaseModel):
    """裁决入参：可选指定计划 id 集合与争用窗口时长（分钟）。"""

    plan_ids: list[int] | None = Field(default=None, description="参与裁决的计划 id；不传则全量扫描")
    window_minutes: int = Field(default=DEFAULT_WINDOW_MINUTES, description="争用窗口时长，默认 10 分钟")


class RecoverPayload(BaseModel):
    base_date: str | None = Field(default=None, description="恢复基准日 YYYY-MM-DD，默认取次日")
    window_minutes: int = Field(default=DEFAULT_WINDOW_MINUTES)


class DecisionPayload(BaseModel):
    decision: str = Field(description="确认改期 或 拒绝改期")


# 注意：FastAPI 按声明顺序匹配，所有静态路径必须排在 /{entry_id} 动态路径之前。
@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按计划编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、待确认"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按计划编号与状态过滤调度编排列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/pending", response_model=dict)
def list_pending() -> dict[str, Any]:
    """工作台口径：汇总待确认计划（含改期生成的新编号条目）。"""
    items = service.list_pending()
    return {"module": "schedule", "total": len(items), "items": items}


@router.get("/batches", response_model=dict)
def list_batches() -> dict[str, Any]:
    """裁决批次清单：含六维度比较、占用计划与改期条目。"""
    items = service.list_batches()
    return {"module": "schedule_batch", "total": len(items), "items": items}


@router.get("/batches/{batch_ref}", response_model=dict)
def get_batch(batch_ref: str) -> dict[str, Any]:
    batch = service.get_batch(batch_ref)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"裁决批次 {batch_ref} 不存在")
    return batch


@router.post("/arbitrate", response_model=dict)
def arbitrate(payload: ArbitratePayload) -> dict[str, Any]:
    """提交裁决：同窗口冲突按业务影响排序，只保留第一次裁决结果，重复提交直接沿用。"""
    result = service.arbitrate(plan_ids=payload.plan_ids, window_minutes=payload.window_minutes)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result["message"])
    return result


@router.post("/recover", response_model=dict)
def recover(payload: RecoverPayload) -> dict[str, Any]:
    """跨日恢复：只重排待确认改期条目，已启动批次不追溯重排。"""
    return service.recover(base_date=payload.base_date, window_minutes=payload.window_minutes)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出调度编排清单：返回当前全量数据（含待确认条目与改期信息）。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "schedule", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条调度计划明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"调度计划 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条调度计划，缺字段或编号重复时说明原因而不是静默丢弃。"""
    entry, message = service.create_entry(payload.values)
    if entry is None:
        detail = "；".join(message) if isinstance(message, list) else str(message)
        return ActionResult(ok=False, message=detail)
    return ActionResult(ok=True, message="调度计划已登记", entry=entry)


@router.patch("/{entry_id}", response_model=ActionResult)
def update_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """局部更新调度计划字段（用于验收场景调整优先级、执行时间等）。"""
    entry, message = service.update_entry(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """四阶段顺向推进：提交校验、启用配置、进入待确认；逆向/跳级动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/reschedule-decision", response_model=ActionResult)
def decide_reschedule(entry_id: int, payload: DecisionPayload) -> ActionResult:
    """工作台对待确认改期条目做确认改期或拒绝改期。"""
    entry, message = service.decide_reschedule(entry_id, payload.decision)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/batches/{batch_ref}/start", response_model=ActionResult)
def start_batch(batch_ref: str) -> ActionResult:
    """启动裁决批次：失败策略在启动时固化，只作用于本批次；已启动批次不追溯。"""
    batch, message = service.start_batch(batch_ref)
    if batch is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=batch)
