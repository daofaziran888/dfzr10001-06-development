"""调度编排冲突裁决与改期的验收测试。

覆盖：
- 验收序列 F：09:00 SCHE-0001 与 09:05 SCHE-0003 争用，负方保留原窗口/通知渠道并生成新编号条目。
- 四阶段状态机：只能草稿→待校验→已启用→待确认顺向推进。
- 23:50/23:55 边界与跨日恢复：只重排待确认项，已启动批次不移动。
- 超时阈值差异不改变优先级裁决；失败策略只在批次启动时固化。
- 多计划同窗口：只有最高优先级占用窗口，其余各留快照。
"""
from __future__ import annotations

import pytest

from app.services.schedule import ScheduleService
from app.store import store


@pytest.fixture(autouse=True)
def reset_store():
    store.reset()
    yield
    store.reset()


@pytest.fixture
def svc():
    return ScheduleService()


def _enable(svc: ScheduleService, entry_id: int) -> None:
    """把计划沿四阶段推到已启用；已启用则跳过。"""
    entry = svc.get_entry(entry_id)
    if entry["status"] == "草稿":
        svc.run_action(entry_id, "提交校验")
    if entry["status"] == "待校验":
        svc.run_action(entry_id, "启用配置")


def _make_plan(svc: ScheduleService, code: str, execute_at: str, priority: int, **overrides) -> dict:
    values = {
        "计划编号": code,
        "关联任务": overrides.pop("关联任务", "TASK-0001"),
        "调度表达式": overrides.pop("调度表达式", f"0 {int(execute_at[:2])} * * *"),
        "优先级": priority,
        "超时阈值": overrides.pop("超时阈值", "30秒"),
        "失败策略": overrides.pop("失败策略", "重试2次"),
        "通知渠道": overrides.pop("通知渠道", "邮件"),
        "是否启用": "是",
        "执行时间": execute_at,
    }
    values.update(overrides)
    entry, missing = svc.create_entry(values)
    assert not missing, missing
    _enable(svc, entry["id"])
    return entry


# ---------- 验收序列 F ----------
class TestAcceptanceF:
    def test_f_sequence_full_flow(self, svc):
        # SCHE-0001 从草稿顺向推到已启用，与已启用的 SCHE-0003 同处一个 10 分钟窗口。
        _enable(svc, 1)
        result = svc.arbitrate()
        assert result["ok"]
        assert len(result["batches"]) == 1
        batch = result["batches"][0]

        # 09:00 的 SCHE-0001（优先级 3）胜出，09:00 不被重排。
        assert batch["占用计划编号"] == "SCHE-0001"
        winner = svc.get_entry(1)
        assert winner["status"] == "已启用"
        assert winner["执行时间"] == "09:00"
        assert winner["裁决结果"] == "占用窗口"

        # 09:05 的 SCHE-0003 进入待确认，原窗口与通知渠道仍可查看。
        loser = svc.get_entry(3)
        assert loser["status"] == "待确认"
        assert loser["原始执行时间"] == "09:05"
        assert loser["原始快照"]["执行时间"] == "09:05"
        assert loser["原始快照"]["通知渠道"] == "邮件"
        assert loser["通知渠道"] == "邮件"

        # 改期条目使用新编号，不沿用旧计划编号。
        r_codes = [r["计划编号"] for r in store.rows("schedule") if r["计划编号"].startswith("SCHE-R-")]
        assert r_codes == ["SCHE-R-0001"]
        r_entry = next(r for r in store.rows("schedule") if r["计划编号"] == "SCHE-R-0001")
        assert r_entry["来源计划编号"] == "SCHE-0003"
        assert r_entry["status"] == "待确认"
        assert r_entry["改期时间"] == "09:10"
        assert r_entry["原始快照"]["通知渠道"] == "邮件"
        assert batch["改期条目"] == ["SCHE-R-0001"]

        # 改期结果回写采集任务下次运行时间；同日改期保留原日期，胜方任务不动。
        task3 = next(t for t in store.rows("task") if t["任务编号"] == "TASK-0003")
        assert task3["下次运行时间"] == "2026-11-13 09:10"
        task1 = next(t for t in store.rows("task") if t["任务编号"] == "TASK-0001")
        assert task1["下次运行时间"] == "2026-11-13 09:00"

        # 工作台同步出现待确认计划（原计划 + 改期条目）。
        pending_codes = [p["计划编号"] for p in svc.list_pending()]
        assert set(pending_codes) == {"SCHE-0003", "SCHE-R-0001"}

    def test_repeat_submission_keeps_first_arbitration(self, svc):
        _enable(svc, 1)
        first = svc.arbitrate(plan_ids=[1, 3])
        first_batch = first["batches"][0]["裁决批次号"]

        again = svc.arbitrate(plan_ids=[1, 3])
        assert again["reused"] is True
        assert again["batches"][0]["裁决批次号"] == first_batch
        # 不产生第二个改期条目。
        r_codes = [r["计划编号"] for r in store.rows("schedule") if r["计划编号"].startswith("SCHE-R-")]
        assert r_codes == ["SCHE-R-0001"]

    def test_single_plan_window_no_arbitration(self, svc):
        # 只有一个已启用计划时不构成争用，不生成裁决批次。
        result = svc.arbitrate()
        assert result["ok"]
        assert result["batches"] == []


# ---------- 四阶段状态机 ----------
class TestStateMachine:
    def test_forward_progression(self, svc):
        entry, _ = svc.run_action(1, "提交校验")
        assert entry["status"] == "待校验"
        entry, _ = svc.run_action(1, "启用配置")
        assert entry["status"] == "已启用"
        assert entry["pending"] is False
        entry, _ = svc.run_action(1, "进入待确认")
        assert entry["status"] == "待确认"
        assert entry["pending"] is True

    def test_reverse_action_rejected(self, svc):
        entry, message = svc.run_action(1, "停用配置")
        assert entry is None
        assert "四阶段" in message

    def test_skip_stage_rejected(self, svc):
        # 已启用计划不能再提交校验（跳级/逆向）。
        entry, message = svc.run_action(3, "提交校验")
        assert entry is None
        assert "顺向推进" in message

    def test_unknown_action_rejected(self, svc):
        entry, message = svc.run_action(1, "任意动作")
        assert entry is None
        assert "不属于四阶段" in message

    def test_draft_cannot_enable_directly(self, svc):
        entry, message = svc.run_action(1, "启用配置")
        assert entry is None
        assert "草稿" in message


# ---------- 裁决维度与优先级 ----------
class TestArbitrationRules:
    def test_priority_dominates_timeout(self, svc):
        # SCHE-0002 优先级 2、超时 45 秒；SCHE-0003 优先级 1、超时 20 秒，同窗口仍按优先级裁决。
        _enable(svc, 2)
        s2 = svc.get_entry(2)
        s2["执行时间"] = "10:00"
        s3 = svc.get_entry(3)
        s3["执行时间"] = "10:00"
        result = svc.arbitrate()
        batch = result["batches"][0]
        assert batch["占用计划编号"] == "SCHE-0002"
        # 六个维度都要参与比较并留痕。
        assert batch["比较维度"] == [
            "关联任务", "调度表达式", "优先级", "超时阈值", "失败策略", "通知渠道",
        ]
        loser = svc.get_entry(3)
        assert loser["status"] == "待确认"

    def test_three_plans_same_window_only_highest_priority_occupies(self, svc):
        # 多计划边界：优先级 1/2/3 同时到期，只允许优先级 3 占用窗口，其余各留快照。
        for entry_id, priority in ((1, 1), (2, 2), (3, 3)):
            _enable(svc, entry_id)
            plan = svc.get_entry(entry_id)
            plan["执行时间"] = "14:00"
            plan["优先级"] = priority
        result = svc.arbitrate()
        assert len(result["batches"]) == 1
        batch = result["batches"][0]
        assert batch["占用计划编号"] == "SCHE-0003"
        assert len(batch["排名"]) == 3
        assert batch["排名"][0]["计划编号"] == "SCHE-0003"

        losers = [svc.get_entry(1), svc.get_entry(2)]
        for loser in losers:
            assert loser["status"] == "待确认"
            assert loser["原始快照"]["执行时间"] == "14:00"
            assert loser["裁决结果"] == "挤出改期"
        r_entries = [r for r in store.rows("schedule") if r["计划编号"].startswith("SCHE-R-")]
        assert len(r_entries) == 2
        assert {r["来源计划编号"] for r in r_entries} == {"SCHE-0001", "SCHE-0002"}
        # 新编号彼此不同且都不沿用旧编号。
        assert len({r["计划编号"] for r in r_entries}) == 2

    def test_equal_priority_uses_secondary_dimensions(self, svc):
        # 优先级相同时，超时更短的计划优先释放资源，应胜出。
        a = _make_plan(svc, "SCHE-0101", "16:00", 5, 超时阈值="60秒", 关联任务="TASK-0001")
        b = _make_plan(svc, "SCHE-0102", "16:00", 5, 超时阈值="20秒", 关联任务="TASK-0001")
        result = svc.arbitrate()
        batch = result["batches"][0]
        assert batch["占用计划编号"] == b["计划编号"]
        assert svc.get_entry(a["id"])["status"] == "待确认"


# ---------- 23:50 边界与跨日恢复 ----------
class TestNightBoundaryAndRecovery:
    def test_night_squeeze_keeps_original_window(self, svc):
        # 5 分钟窗口：23:50 槽与 23:55 槽各放一个高优先级竞争者。
        _enable(svc, 2)  # SCHE-0002 23:50 优先级 2
        _enable(svc, 3)  # SCHE-0003 调整到 23:55，优先级 1
        svc.get_entry(3)["执行时间"] = "23:55"
        comp_2350 = _make_plan(svc, "SCHE-0201", "23:50", 9, 关联任务="TASK-0001")
        comp_2355 = _make_plan(svc, "SCHE-0202", "23:55", 9, 关联任务="TASK-0001")

        result = svc.arbitrate(window_minutes=5)
        assert len(result["batches"]) == 2

        # SCHE-0002 在 23:50 被挤出，保留 23:50 原窗口。
        s2 = svc.get_entry(2)
        assert s2["status"] == "待确认"
        assert s2["原始执行时间"] == "23:50"
        assert s2["原始快照"]["执行时间"] == "23:50"

        # SCHE-0003 在 23:55 进入待确认，23:55 原窗口可查。
        s3 = svc.get_entry(3)
        assert s3["status"] == "待确认"
        assert s3["原始执行时间"] == "23:55"

        # 两个竞争者各自占用窗口，未被重排。
        assert svc.get_entry(comp_2350["id"])["裁决结果"] == "占用窗口"
        assert svc.get_entry(comp_2355["id"])["裁决结果"] == "占用窗口"

        # 23:55 槽的改期落入次日 00:00，回写任务时日期顺延一天。
        r3 = next(
            r for r in store.rows("schedule")
            if r.get("来源计划编号") == "SCHE-0003"
        )
        assert r3["改期时间"] == "00:00"
        assert r3["跨日"] is True
        task3 = next(t for t in store.rows("task") if t["任务编号"] == "TASK-0003")
        assert task3["下次运行时间"] == "2026-11-14 00:00"

    def test_recover_only_moves_pending_entries(self, svc):
        _enable(svc, 2)
        winner = _make_plan(svc, "SCHE-0301", "23:50", 9, 关联任务="TASK-0001")
        result = svc.arbitrate(window_minutes=5)
        batch = result["batches"][0]

        # 启动批次后，历史已启动计划不追溯重排。
        started, _ = svc.start_batch(batch["裁决批次号"])
        assert started["状态"] == "已启动"
        winner_before = svc.get_entry(winner["id"])["执行时间"]

        recover = svc.recover(base_date="2026-11-13", window_minutes=5)
        assert recover["ok"]
        assert batch["裁决批次号"] in recover["started_batches_untouched"]

        # 已启动批次的胜方窗口纹丝不动。
        assert svc.get_entry(winner["id"])["执行时间"] == winner_before
        # 待确认改期条目被重排，并带上恢复日期。
        moved_codes = {item["计划编号"] for item in recover["moved"]}
        r_entries = [r for r in store.rows("schedule") if r["计划编号"].startswith("SCHE-R-")]
        assert {r["计划编号"] for r in r_entries} <= moved_codes
        for r in r_entries:
            assert r["恢复日期"] == "2026-11-13"

    def test_recover_skips_started_slots(self, svc):
        # 待确认条目重排时若撞上已启动批次的窗口，继续顺延而不是抢占。
        _enable(svc, 2)
        winner = _make_plan(svc, "SCHE-0401", "23:50", 9, 关联任务="TASK-0001")
        result = svc.arbitrate(window_minutes=5)
        batch = result["batches"][0]
        svc.start_batch(batch["裁决批次号"])  # 占用 23:50 槽（slot 286）

        r_entry = next(r for r in store.rows("schedule") if r["计划编号"].startswith("SCHE-R-"))
        # 改期条目原落在 23:55 槽；恢复时把它挪回 23:50 附近也必须绕开已启动槽。
        r_entry["改期时间"] = "23:50"
        r_entry["执行时间"] = "23:50"
        recover = svc.recover(base_date="2026-11-13", window_minutes=5)
        moved = next(item for item in recover["moved"] if item["计划编号"] == r_entry["计划编号"])
        assert moved["改期时间"] != "23:50"
        assert svc.get_entry(winner["id"])["执行时间"] == "23:50"


# ---------- 批次启动与失败策略 ----------
class TestBatchStart:
    def test_failure_policy_only_binds_started_batch(self, svc):
        _enable(svc, 1)
        result = svc.arbitrate()
        batch = result["batches"][0]
        winner_policy = svc.get_entry(1)["失败策略"]
        assert "生效失败策略" not in batch

        started, message = svc.start_batch(batch["裁决批次号"])
        assert started["生效失败策略"] == winner_policy
        assert "本批次" in message

        # 重复启动不重排，直接返回已启动状态。
        again, again_msg = svc.start_batch(batch["裁决批次号"])
        assert again["状态"] == "已启动"
        assert "不追溯" in again_msg

    def test_start_unknown_batch(self, svc):
        batch, message = svc.start_batch("ARB-20990101-999")
        assert batch is None
        assert "不存在" in message


# ---------- 工作台确认 ----------
class TestWorkbenchDecision:
    def test_confirm_and_reject_flow(self, svc):
        _enable(svc, 1)
        svc.arbitrate()
        r_entry = next(r for r in store.rows("schedule") if r["计划编号"].startswith("SCHE-R-"))

        confirmed, msg = svc.decide_reschedule(r_entry["id"], "确认改期")
        assert confirmed["确认状态"] == "已确认改期"
        assert confirmed["pending"] is False
        # 原计划确认状态同步。
        source = svc.get_entry(3)
        assert source["确认状态"] == "已确认改期"

        # 已处理条目不能重复确认。
        again, message = svc.decide_reschedule(r_entry["id"], "拒绝改期")
        assert again is None
        assert "不能重复确认" in message

    def test_decision_unsupported(self, svc):
        _enable(svc, 1)
        svc.arbitrate()
        r_entry = next(r for r in store.rows("schedule") if r["计划编号"].startswith("SCHE-R-"))
        entry, message = svc.decide_reschedule(r_entry["id"], "随便处理")
        assert entry is None
        assert "确认改期或拒绝改期" in message

    def test_original_plan_cannot_be_decided(self, svc):
        # 非改期条目（没有来源计划编号）不能走确认动作。
        entry, message = svc.decide_reschedule(1, "确认改期")
        assert entry is None
        assert "不是改期生成" in message
