"""Document-aligned Record timings, independent of the capped low-level trace.

Operation scopes partition work at their own level: S1-S6 are children of 1.6
or 2.7, not additional run time. Uninstrumented control time stays explicit.
Only fixed labels and numeric step indices are recorded, never user payloads.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from threading import get_ident
from time import monotonic

from palinreplay.core.progress import ProgressEvent, ProgressReporter

LABELS = {
    "1.1": "检查运行参数",
    "1.2": "加载任务与模型配置",
    "1.3": "创建 Docker 环境",
    "1.4": "建立录制组件",
    "1.5": "构造初始 Agent 上下文",
    "1.6": "捕获并提交 C0",
    "2.1": "检查预算、开始 Step 与查询控制",
    "2.2": "调用模型并录制",
    "2.3": "接纳模型结果、声明工具组",
    "2.4": "执行工具并录制",
    "2.5": "接纳工具结果与本步收束",
    "2.6": "检查步骤完整性",
    "2.7": "捕获状态并提交 Ci",
    "3.1": "记录结束状态",
    "3.2": "清理运行资源",
    "3.3": "重新打开 Store 检查",
    "3.4": "完成运行报告",
    "S1": "固定录制边界",
    "S2": "采集环境状态与捕获调度",
    "S3": "保存文件材料",
    "S4": "保存状态及诊断",
    "S5": "再次核对边界",
    "S6": "原子提交节点",
}
PARTS = {
    "1.2": {
        "1.2.1": "读取任务包",
        "1.2.2": "检查并加载 Agent",
        "1.2.3": "读取模型连接配置",
    },
    "1.3": {
        "1.3.1": "连接 Docker",
        "1.3.2": "构造任务环境对象",
        "1.3.3": "启动并检查任务容器",
    },
    "1.4": {
        "1.4.1": "配置文件状态后端",
        "1.4.2": "创建 Store",
        "1.4.3": "配置 Agent 适配器与模型",
        "1.4.4": "创建 Agent 与 Recorder",
    },
    "2.1": {
        "2.1.1": "预算检查与查询调度",
        "2.1.2": "创建本步录制记录",
    },
    "2.5": {
        "2.5.1": "执行工具组与格式化结果",
        "2.5.2": "接纳工具结果",
        "2.5.3": "接纳提交或控制消息",
        "2.5.4": "原生步骤保存",
    },
    "2.6": {
        "2.6.1": "适配器完整性预检",
        "2.6.2": "Recorder 提交前检查",
    },
    "3.1": {
        "3.1.1": "保存结束状态与最终上下文",
        "3.1.2": "保存失败诊断与最终上下文",
        "3.1.3": "关闭 Recorder 生命周期",
    },
    "3.2": {
        "3.2.1": "清理任务容器",
        "3.2.2": "关闭 Store",
        "3.2.3": "关闭 Docker 客户端",
    },
    "3.4": {
        "3.4.1": "整理 Agent 结果与边界统计",
        "3.4.2": "汇总校验结果并写入报告",
    },
    "S3": {
        "S3.1": "保存内容包、完整清单与索引",
        "S3.2": "保存旧格式完整清单",
        "S3.3": "校验旧格式复用材料",
        "S3.4": "发布并校验旧格式归档",
    },
    "S4": {
        "S4.1": "保存或复用文件状态",
        "S4.2": "保存捕获诊断",
        "S4.3": "更新增量基线",
        "S4.4": "确认 State 持久化",
    },
}
PHASES = ("initialization", "steps", "finalization")
PHASE_LABELS = ("开始初始化", "Step 循环", "最终收尾")
_active: ContextVar = ContextVar("record_flow_timing", default=None)
_step: ContextVar = ContextVar("record_flow_step", default=None)


def _family(code):
    return "capture" if code.startswith("S") else "operation"


def _row(code, value=None, *, label=None):
    value = dict(value or {
        "count": 0, "duration_sec": None, "inclusive_sec": None,
        "raised": 0, "measurement": "not_observed",
    })
    parts = value.pop("parts", {})
    row = {"id": code, "label": label if label is not None else LABELS[code], **value}
    if code in PARTS:
        row["parts"] = [
            _row(key, parts.get(key), label=name) for key, name in PARTS[code].items()
        ]
        row["unclassified_sec"] = (
            value["duration_sec"] - sum(p["duration_sec"] for p in parts.values())
            if value["duration_sec"] is not None else None
        )
    return row


@dataclass
class RecordTiming:
    started: float = field(default_factory=monotonic)
    owner: int = field(default_factory=get_ident)
    max_steps: int = 10_000
    notify: ProgressReporter = field(default_factory=ProgressReporter, repr=False)
    phase: int = 0
    phase_started: float = 0.0
    phase_seconds: list = field(default_factory=lambda: [0.0, 0.0, 0.0])
    totals: dict = field(default_factory=dict)
    groups: dict = field(default_factory=dict)
    stack: list = field(default_factory=list)
    omitted_groups: bool = False
    duration_sec: float | None = None

    def transition(self, phase):
        index = PHASES.index(phase)
        if index <= self.phase:
            return
        now = monotonic() - self.started
        previous = self.phase
        self.phase_seconds[previous] += now - self.phase_started
        self.phase, self.phase_started = index, now
        self.phase_event(previous, "finished", self.phase_seconds[previous])
        self.phase_event(index, "started")

    def phase_event(self, index, status, seconds=None):
        self.notify(ProgressEvent(
            "flow_phase", code=str(index + 1), label=PHASE_LABELS[index],
            status=status, duration_sec=seconds,
        ))

    def finish(self):
        self.duration_sec = monotonic() - self.started
        self.phase_seconds[self.phase] += self.duration_sec - self.phase_started
        self.phase_event(self.phase, "finished", self.phase_seconds[self.phase])

    def add(self, code, step, elapsed, exclusive, raised, part=None):
        def accumulate(target, key):
            value = target.setdefault(key, {
                "count": 0, "duration_sec": 0.0, "inclusive_sec": 0.0,
                "raised": 0, "measurement": "measured",
            })
            value["count"] += 1
            value["duration_sec"] += exclusive
            value["inclusive_sec"] += elapsed
            value["raised"] += raised
            return value

        def record(target):
            value = accumulate(target, code)
            if part is not None:
                accumulate(value.setdefault("parts", {}), part)

        record(self.totals)
        if step is not None:
            if step not in self.groups and len(self.groups) >= self.max_steps:
                self.omitted_groups = True
                return
            record(self.groups.setdefault(step, {}))

    def snapshot(self):
        phases = []
        for index, (phase, label, seconds) in enumerate(
            zip(PHASES, PHASE_LABELS, self.phase_seconds, strict=True), 1
        ):
            rows = [_row(k, self.totals.get(k)) for k in LABELS if k.startswith(f"{index}.")]
            measured = sum(r["duration_sec"] or 0 for r in rows)
            phases.append({
                "id": phase, "label": label, "duration_sec": seconds,
                "substeps": rows, "measured_substeps_sec": measured,
                "other_sec": seconds - measured,
            })
        groups = []
        for step, values in sorted(self.groups.items()):
            code = "1.6" if step == 0 else "2.7"
            capture = values.get(code)
            substeps = [_row(k, values.get(k)) for k in LABELS if k.startswith("S")]
            captured = sum(r["duration_sec"] or 0 for r in substeps)
            groups.append({
                "index": step,
                "substeps": [_row(k, values.get(k)) for k in LABELS if k.startswith("2.")]
                if step else [],
                "capture": {
                    "duration_sec": capture["duration_sec"] if capture else None,
                    "raised": capture["raised"] if capture else None,
                    "substeps": substeps,
                    "other_sec": capture["duration_sec"] - captured if capture else None,
                },
            })
        return {
            "schema_version": "palinreplay.record-flow-timing.v1",
            "clock": "time.monotonic",
            "scope": "record-body-excluding-final-debug-serialization",
            "duration_sec": self.duration_sec,
            "phases": phases,
            "steps": groups,
            "step_details_complete": not self.omitted_groups,
            "max_step_groups": self.max_steps,
        }


def current():
    timing = _active.get()
    return timing if timing is not None and timing.owner == get_ident() else None


@contextmanager
def record_timing(enabled, *, max_steps=10_000, progress=None):
    timing = RecordTiming(max_steps=max_steps, notify=ProgressReporter(progress)) if enabled else None
    token = _active.set(timing)
    step_token = _step.set(None)
    try:
        if timing is not None:
            timing.phase_event(0, "started")
        yield timing
    finally:
        try:
            if timing is not None:
                timing.finish()
        finally:
            _step.reset(step_token)
            _active.reset(token)


def record_phase(phase):
    if (timing := current()) is not None:
        timing.transition(phase)


@contextmanager
def record_step(index):
    token = _step.set(index)
    try:
        yield
    finally:
        _step.reset(token)


@contextmanager
def flow_scope(code, *, step=None, part=None, tool=None, tools=None):
    timing = current()
    if timing is None:
        yield
        return
    if code not in LABELS:
        raise ValueError("unknown Record flow timing code")
    if part is not None and part not in PARTS.get(code, {}):
        raise ValueError("unknown Record flow timing substep")
    parent = next((f for f in reversed(timing.stack) if f["family"] == _family(code)), None)
    index = _step.get() if step is None else step
    identity = {
        "code": code, "subcode": part, "step": index,
        "label": PARTS[code][part] if part is not None else LABELS[code],
        "depth": len(timing.stack), "tool": tool, "tools": tools,
    }
    frame = {"family": _family(code), "children": 0.0}
    timing.stack.append(frame)
    started = monotonic()
    raised = False
    try:
        timing.notify(ProgressEvent("flow_start", **identity))
        yield
    except BaseException:
        raised = True
        raise
    finally:
        elapsed = monotonic() - started
        timing.stack.pop()
        if parent is not None:
            parent["children"] += elapsed
        exclusive = elapsed - frame["children"]
        timing.add(code, index, elapsed, exclusive, raised, part)
        timing.notify(ProgressEvent(
            "flow_end", **identity,
            status="raised" if raised else "finished", duration_sec=exclusive,
            inclusive_sec=elapsed,
        ))


def flow_timed(code, *, part=None):
    def decorate(function):
        @wraps(function)
        def call(*args, **kwargs):
            with flow_scope(code, part=part):
                return function(*args, **kwargs)
        return call
    return decorate
