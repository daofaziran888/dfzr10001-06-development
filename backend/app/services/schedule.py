"""调度编排业务规则：四阶段状态机、冲突裁决与改期、跨日恢复。

核心约定：
- 计划只能顺着「草稿 → 待校验 → 已启用 → 待确认」四个阶段向下推进，不允许逆向或跳级。
- 多个已启用计划争用同一执行窗口时，按业务影响裁决：优先级不同只看优先级（数值越大
  业务影响越高）；优先级相同时才依次比较关联任务、调度表达式、超时阈值、失败策略、通知渠道。
- 被挤出的计划保留原快照并转入待确认，同时生成新编号（SCHE-R-xxxx）的待确认改期条目，
  改期结果回写采集任务的下次运行时间。
- 裁决按批次幂等：同一计划重复提交只保留第一个裁决结果；历史已启动批次不追溯重排。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.store import store

MODULE = "schedule"
BATCH_MODULE = "schedule_batch"
TASK_MODULE = "task"

REQUIRED_FIELDS = ["计划编号", "关联任务", "调度表达式"]
# 四阶段：只能顺向推进，停用等离链动作不在允许范围内。
STATUS_ORDER = ["草稿", "待校验", "已启用", "待确认"]
# 动作 -> (要求的当前状态, 目标状态)，保证只能相邻阶段正向推进。
ACTION_RULES: dict[str, tuple[str, str]] = {
    "提交校验": ("草稿", "待校验"),
    "启用配置": ("待校验", "已启用"),
    "进入待确认": ("已启用", "待确认"),
}
# 裁决时同时比较的六个业务维度，批次记录里原样留存以便追溯。
ARBITRATION_DIMENSIONS = ["关联任务", "调度表达式", "优先级", "超时阈值", "失败策略", "通知渠道"]
PLAN_SNAPSHOT_FIELDS = [
    "计划编号", "关联任务", "调度表达式", "优先级", "超时阈值",
    "失败策略", "通知渠道", "是否启用", "执行时间",
]
EDITABLE_FIELDS = [
    "关联任务", "调度表达式", "优先级", "超时阈值",
    "失败策略", "通知渠道", "是否启用", "执行时间",
]
DEFAULT_WINDOW_MINUTES = 10
RESCHEDULE_PREFIX = "SCHE-R"
DAY_MINUTES = 24 * 60

# 关联任务越接近实际执行，业务影响越大。
TASK_STATUS_WEIGHT = {"执行中": 3, "待执行": 2, "已完成": 1, "草稿": 0}
URGENT_CHANNEL_KEYS = ("短信", "电话", "钉钉", "企业微信", "webhook")


def _parse_minutes(value: Any) -> int | None:
    """把 'HH:MM'、'HH:MM:SS' 或整数分钟解析为当日分钟数；无法解析返回 None。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if ":" in text:
            parts = text.split(":")
            return int(parts[0]) * 60 + int(parts[1])
        return int(float(text))
    except (ValueError, IndexError):
        return None


def _format_minutes(total: int) -> str:
    """分钟数格式化为 HH:MM，超出 24 小时的部分按跨日取模。"""
    total %= DAY_MINUTES
    return f"{total // 60:02d}:{total % 60:02d}"


def _priority_value(plan: dict[str, Any]) -> int:
    """优先级数值：数值越大代表业务影响越高、越应占用窗口。"""
    try:
        return int(float(str(plan.get("优先级") or 0)))
    except ValueError:
        return 0


def _timeout_seconds(plan: dict[str, Any]) -> int:
    """超时阈值秒数，兼容 '20秒'、'20' 等写法；无法解析按 0 处理。"""
    digits = "".join(ch for ch in str(plan.get("超时阈值") or "") if ch.isdigit())
    return int(digits) if digits else 0


def _failure_weight(plan: dict[str, Any]) -> int:
    """失败策略严格度：终止 > 重试 > 忽略/跳过。"""
    text = str(plan.get("失败策略") or "")
    if "终止" in text:
        return 3
    if "重试" in text:
        return 2
    if "忽略" in text or "跳过" in text:
        return 1
    return 0


def _channel_weight(plan: dict[str, Any]) -> int:
    """带实时告警能力的通知渠道业务影响更高。"""
    channel = str(plan.get("通知渠道") or "").lower()
    return 1 if any(key.lower() in channel for key in URGENT_CHANNEL_KEYS) else 0


def _cron_specificity(plan: dict[str, Any]) -> int:
    """调度表达式精确度：非通配字段越多，计划约束越精确。"""
    return sum(1 for part in str(plan.get("调度表达式") or "").split() if part and part != "*")


def _find_task(task_ref: Any) -> dict[str, Any] | None:
    """按任务编号或任务名称定位采集任务。"""
    ref = str(task_ref or "")
    for row in store.rows(TASK_MODULE):
        if row.get("任务编号") == ref or row.get("任务名称") == ref:
            return row
    return None


def _task_criticality(plan: dict[str, Any]) -> tuple[int, int]:
    """关联任务重要度：任务状态权重 + 任务 id 兜底。"""
    task = _find_task(plan.get("关联任务"))
    if task is None:
        return (0, 0)
    return (TASK_STATUS_WEIGHT.get(str(task.get("status") or ""), 0), int(task.get("id", 0)))


def _rank_key(plan: dict[str, Any]) -> tuple[int, ...]:
    """同窗口排序依据，键值越大越优先占用窗口。

    第一位是优先级：优先级不同的计划只按优先级裁决，其余维度不参与；
    仅当优先级相同，才继续比较关联任务、表达式、超时阈值、失败策略、通知渠道，
    最后以创建顺序（id 小者先）作稳定兜底，保证结果可复现。
    """
    return (
        _priority_value(plan),
        _task_criticality(plan)[0],
        _task_criticality(plan)[1],
        _cron_specificity(plan),
        -_timeout_seconds(plan),
        _failure_weight(plan),
        _channel_weight(plan),
        -int(plan.get("id", 0)),
    )


def _explain_rank(plan: dict[str, Any]) -> str:
    """生成可读的排序依据说明，写入裁决批次便于审计。"""
    criticality = _task_criticality(plan)
    return (
        f"优先级={_priority_value(plan)}；关联任务权重={criticality[0]}；"
        f"表达式精度={_cron_specificity(plan)}；超时={_timeout_seconds(plan)}秒；"
        f"失败策略权重={_failure_weight(plan)}；通知渠道权重={_channel_weight(plan)}"
    )


class ScheduleService:
    # ---------- 列表与明细 ----------
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("计划编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def list_pending(self) -> list[dict[str, Any]]:
        """工作台口径：所有处于待确认阶段的计划与改期条目。"""
        return [row for row in store.rows(MODULE) if row.get("status") == "待确认"]

    def list_batches(self) -> list[dict[str, Any]]:
        return list(store.rows(BATCH_MODULE))

    def get_batch(self, batch_ref: str) -> dict[str, Any] | None:
        for batch in store.rows(BATCH_MODULE):
            if batch.get("裁决批次号") == batch_ref or str(batch.get("id")) == str(batch_ref):
                return batch
        return None

    # ---------- 登记与编辑 ----------
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        code = str(values.get("计划编号") or "").strip()
        if any(row.get("计划编号") == code for row in rows):
            return None, [f"计划编号 {code} 已存在"]
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        for field in PLAN_SNAPSHOT_FIELDS:
            entry[field] = values.get(field)
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def update_entry(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"调度计划 {entry_id} 不存在或已归档"
        for field in EDITABLE_FIELDS:
            # 按键存在性更新，避免优先级 0 这类假值被当成“未填写”而丢弃。
            if field in values and values[field] is not None and str(values[field]).strip() != "":
                entry[field] = values[field]
        return entry, "调度计划已更新"

    # ---------- 四阶段状态机 ----------
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"调度计划 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, (
                f"动作「{action}」不属于四阶段推进范围，"
                f"只允许：提交校验、启用配置、进入待确认（草稿→待校验→已启用→待确认）"
            )
        source_status, target = ACTION_RULES[action]
        current = str(entry.get("status") or "")
        if current != source_status:
            return None, f"计划当前为「{current}」，只能顺向推进，不能从该状态执行「{action}」"
        entry["status"] = target
        # 已启用是稳定运行态不算待处理；草稿、待校验、待确认都需要人跟进。
        entry["pending"] = target != "已启用"
        entry["abnormal"] = False
        return entry, f"调度计划已{action}，阶段推进为「{target}」"

    # ---------- 冲突裁决与改期 ----------
    def arbitrate(
        self,
        *,
        plan_ids: list[int] | None = None,
        window_minutes: int = DEFAULT_WINDOW_MINUTES,
    ) -> dict[str, Any]:
        """对已启用计划按执行窗口裁决；已裁决过的计划沿用第一个裁决结果。"""
        rows = store.rows(MODULE)
        window_minutes = max(int(window_minutes or DEFAULT_WINDOW_MINUTES), 1)

        if plan_ids:
            id_set = {int(pid) for pid in plan_ids}
            targets = [row for row in rows if int(row.get("id", 0)) in id_set]
            missing = sorted(id_set - {int(row.get("id", 0)) for row in targets})
            if missing:
                return {"ok": False, "message": f"调度计划 {missing} 不存在或已归档", "batches": []}
            existing = next((row for row in targets if row.get("裁决批次")), None)
            if existing is not None:
                # 提交计划时只保留第一个裁决结果，不重复裁决、不追溯重排。
                batch = self.get_batch(str(existing["裁决批次"]))
                return {
                    "ok": True,
                    "reused": True,
                    "message": f"计划已存在裁决批次 {existing['裁决批次']}，沿用第一次裁决结果",
                    "batches": [batch] if batch else [],
                    "skipped": [],
                }
        else:
            targets = rows

        # 只有已启用、尚未裁决的计划参与窗口争用。
        candidates = [
            row for row in targets
            if row.get("status") == "已启用" and not row.get("裁决批次")
        ]
        groups: dict[int, list[dict[str, Any]]] = {}
        skipped: list[dict[str, str]] = []
        for plan in candidates:
            minutes = _parse_minutes(plan.get("执行时间"))
            if minutes is None:
                skipped.append({"计划编号": str(plan.get("计划编号")), "原因": "执行时间无法解析，跳过裁决"})
                continue
            groups.setdefault(minutes // window_minutes, []).append(plan)

        batches: list[dict[str, Any]] = []
        for slot in sorted(groups):
            plans = groups[slot]
            if len(plans) < 2:
                continue  # 单计划独占窗口，不产生冲突裁决。
            ranked = sorted(plans, key=_rank_key, reverse=True)
            batches.append(self._create_batch(slot, window_minutes, ranked[0], ranked[1:], ranked))

        if not batches:
            message = "本次没有发现多计划争用同一执行窗口的冲突"
        else:
            message = f"裁决完成，生成 {len(batches)} 个裁决批次"
        return {"ok": True, "reused": False, "message": message, "batches": batches, "skipped": skipped}

    def _create_batch(
        self,
        slot: int,
        window_minutes: int,
        winner: dict[str, Any],
        losers: list[dict[str, Any]],
        ranked: list[dict[str, Any]],
    ) -> dict[str, Any]:
        batch_rows = store.rows(BATCH_MODULE)
        seq = max((int(batch.get("seq", 0)) for batch in batch_rows), default=0) + 1
        batch_code = f"ARB-{datetime.now().strftime('%Y%m%d')}-{seq:03d}"
        slot_start = slot * window_minutes
        batch: dict[str, Any] = {
            "id": seq,
            "seq": seq,
            "裁决批次号": batch_code,
            "窗口起点": _format_minutes(slot_start),
            "窗口时长分钟": window_minutes,
            "状态": "已裁决",
            "占用计划编号": winner.get("计划编号"),
            "比较维度": list(ARBITRATION_DIMENSIONS),
            "排名": [
                {
                    "计划编号": plan.get("计划编号"),
                    "关联任务": plan.get("关联任务"),
                    "调度表达式": plan.get("调度表达式"),
                    "优先级": _priority_value(plan),
                    "超时阈值": plan.get("超时阈值"),
                    "失败策略": plan.get("失败策略"),
                    "通知渠道": plan.get("通知渠道"),
                    "排序依据": _explain_rank(plan),
                }
                for plan in ranked
            ],
            "改期条目": [],
        }
        batch_rows.append(batch)

        # 胜方占用窗口：不重排、不改写任务下次运行时间。
        winner["裁决批次"] = batch_code
        winner["裁决结果"] = "占用窗口"
        winner["裁决理由"] = (
            f"同窗口业务影响排序第一（优先级 {_priority_value(winner)}），保留原执行窗口"
        )

        # 负方保留原快照、进入待确认，并生成新编号的待确认改期条目。
        for loser in losers:
            self._reschedule_loser(loser, batch, slot, window_minutes)
        return batch

    def _reschedule_loser(
        self,
        loser: dict[str, Any],
        batch: dict[str, Any],
        slot: int,
        window_minutes: int,
    ) -> None:
        original_time = str(loser.get("执行时间") or "")
        next_minutes = (slot + 1) * window_minutes
        cross_day = next_minutes >= DAY_MINUTES
        next_time = _format_minutes(next_minutes)
        snapshot = {field: loser.get(field) for field in PLAN_SNAPSHOT_FIELDS}

        # 原计划保留原快照与原窗口，只把阶段推进到待确认。
        loser["原始快照"] = snapshot
        loser["原始执行时间"] = original_time
        loser["裁决批次"] = batch["裁决批次号"]
        loser["裁决结果"] = "挤出改期"
        loser["裁决理由"] = (
            f"同窗口业务影响低于 {batch['占用计划编号']}，原窗口 {original_time} 保留并改期至 {next_time}"
        )
        loser["status"] = "待确认"
        loser["pending"] = True
        loser["abnormal"] = False
        loser["确认状态"] = "待处理"

        # 待确认改期条目必须使用新编号，不能沿用旧计划编号。
        rows = store.rows(MODULE)
        new_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        r_seq = max(
            (
                int(str(row.get("计划编号", "")).split("-")[-1])
                for row in rows
                if str(row.get("计划编号", "")).startswith(f"{RESCHEDULE_PREFIX}-")
                and str(row.get("计划编号", "")).split("-")[-1].isdigit()
            ),
            default=0,
        ) + 1
        new_code = f"{RESCHEDULE_PREFIX}-{r_seq:04d}"
        entry: dict[str, Any] = {
            "id": new_id,
            "计划编号": new_code,
            "来源计划编号": loser.get("计划编号"),
            "关联任务": loser.get("关联任务"),
            "调度表达式": loser.get("调度表达式"),
            "优先级": loser.get("优先级"),
            "超时阈值": loser.get("超时阈值"),
            "失败策略": loser.get("失败策略"),
            "通知渠道": loser.get("通知渠道"),
            "是否启用": loser.get("是否启用"),
            "执行时间": next_time,
            "改期时间": next_time,
            "原始执行时间": original_time,
            "跨日": cross_day,
            "原始快照": snapshot,
            "裁决批次": batch["裁决批次号"],
            "裁决结果": "挤出改期",
            "确认状态": "待处理",
            "status": "待确认",
            "pending": True,
            "abnormal": False,
        }
        rows.append(entry)
        loser["改期条目"] = new_code
        batch["改期条目"].append(new_code)

        # 改期结果回写采集任务的下次运行时间（胜方不回写）。
        self._write_back_task(loser.get("关联任务"), next_time, cross_day, new_code)

    def _write_back_task(
        self,
        task_ref: Any,
        next_time: str,
        cross_day: bool,
        entry_code: str,
        base_date: datetime | None = None,
    ) -> dict[str, Any] | None:
        task = _find_task(task_ref)
        if task is None:
            return None
        # 优先沿用任务原下次运行时间的日期部分：同日改期不改日期，跨日才顺延一天；
        # 原字段缺失或不可解析时再退回到传入基准日/当天。
        day = None
        original = str(task.get("下次运行时间") or "")
        if original:
            try:
                day = datetime.strptime(original[:10], "%Y-%m-%d").date()
            except ValueError:
                day = None
        if day is None:
            day = (base_date or datetime.now()).date()
        if cross_day:
            day = day + timedelta(days=1)
        task["下次运行时间"] = f"{day.isoformat()} {next_time}"
        task["最近改期条目"] = entry_code
        return task

    # ---------- 批次启动：失败策略只作用于被启动批次 ----------
    def start_batch(self, batch_ref: str) -> tuple[dict[str, Any] | None, str]:
        batch = self.get_batch(batch_ref)
        if batch is None:
            return None, f"裁决批次 {batch_ref} 不存在"
        if batch.get("状态") == "已启动":
            return batch, "该批次已启动，历史已启动计划不追溯重排"
        if batch.get("状态") != "已裁决":
            return None, f"批次状态为「{batch.get('状态')}」，不可启动"
        winner = next(
            (row for row in store.rows(MODULE) if row.get("计划编号") == batch.get("占用计划编号")),
            None,
        )
        batch["状态"] = "已启动"
        batch["启动时间"] = datetime.now().isoformat(timespec="seconds")
        # 失败策略在批次启动时才固化，只对这个被启动的批次生效。
        batch["生效失败策略"] = winner.get("失败策略") if winner else None
        return batch, f"批次 {batch['裁决批次号']} 已启动，失败策略仅作用于本批次"

    # ---------- 跨日恢复：只重排待确认项 ----------
    def recover(
        self,
        *,
        base_date: str | None = None,
        window_minutes: int = DEFAULT_WINDOW_MINUTES,
    ) -> dict[str, Any]:
        """跨日恢复：仅重排未处理的待确认改期条目，已启动批次的窗口一律不动。"""
        window_minutes = max(int(window_minutes or DEFAULT_WINDOW_MINUTES), 1)
        day = (
            datetime.strptime(base_date, "%Y-%m-%d").date()
            if base_date
            else datetime.now().date() + timedelta(days=1)
        )
        started_batches = [b for b in store.rows(BATCH_MODULE) if b.get("状态") == "已启动"]
        started_slots = {
            (_parse_minutes(b.get("窗口起点")) or 0) // window_minutes for b in started_batches
        }

        moved: list[dict[str, Any]] = []
        for row in store.rows(MODULE):
            if row.get("status") != "待确认":
                continue
            if row.get("确认状态") != "待处理" or not row.get("来源计划编号"):
                continue  # 只动待确认改期条目；原计划快照、已确认/已拒绝项都不动。
            minutes = _parse_minutes(row.get("改期时间") or row.get("执行时间"))
            if minutes is None:
                continue
            slot = minutes // window_minutes
            while slot in started_slots:
                slot += 1  # 已启动批次不让位，改期条目顺延到空闲窗口。
            new_time = _format_minutes(slot * window_minutes)
            cross_day = slot * window_minutes >= DAY_MINUTES
            row["执行时间"] = new_time
            row["改期时间"] = new_time
            row["跨日"] = cross_day
            row["恢复日期"] = day.isoformat()
            self._write_back_task(row.get("关联任务"), new_time, cross_day, str(row["计划编号"]),
                                  base_date=datetime.combine(day, datetime.min.time()))
            moved.append({
                "计划编号": row.get("计划编号"),
                "来源计划编号": row.get("来源计划编号"),
                "改期时间": new_time,
                "恢复日期": day.isoformat(),
            })

        return {
            "ok": True,
            "base_date": day.isoformat(),
            "moved": moved,
            "started_batches_untouched": [b.get("裁决批次号") for b in started_batches],
            "message": f"跨日恢复完成，重排 {len(moved)} 条待确认改期条目，已启动批次保持不变",
        }

    # ---------- 工作台确认动作 ----------
    def decide_reschedule(
        self,
        entry_id: int,
        decision: str,
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"待确认条目 {entry_id} 不存在或已归档"
        if entry.get("status") != "待确认" or not entry.get("来源计划编号"):
            return None, "该记录不是改期生成的待确认条目，不能执行确认动作"
        if entry.get("确认状态") != "待处理":
            return None, f"该条目已处理（{entry.get('确认状态')}），不能重复确认"
        if decision == "确认改期":
            entry["确认状态"] = "已确认改期"
        elif decision == "拒绝改期":
            entry["确认状态"] = "已拒绝"
        else:
            return None, f"确认动作「{decision}」不支持，只允许确认改期或拒绝改期"
        entry["pending"] = False
        # 同步原计划的确认状态，工作台两边口径一致。
        source = next(
            (row for row in store.rows(MODULE) if row.get("计划编号") == entry.get("来源计划编号")),
            None,
        )
        if source is not None:
            source["确认状态"] = entry["确认状态"]
            source["pending"] = False
        return entry, f"改期条目已{entry['确认状态']}"
