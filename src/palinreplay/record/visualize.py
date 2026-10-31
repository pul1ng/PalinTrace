"""Build a self-contained, read-only viewer from a Btrfs Record run and its Store.

CLI: palin record visualize RUN_DIR [--output PAGE.html]
No filesystem payloads are copied; this viewing document is not a restore bundle.
"""

# Chinese UI copy intentionally uses full-width punctuation.
# ruff: noqa: RUF001

from __future__ import annotations

import json
from pathlib import Path

from palinreplay.core.models import Execution, Node, Run, StateCapture, Step
from palinreplay.record.inspect import call_document
from palinreplay.storage.io import outside_store
from palinreplay.storage.store import RecordStore


def build_data(run_dir: Path) -> dict:
    run_dir = run_dir.resolve()
    report = json.loads((run_dir / "report.json").read_text())
    timing = report.get("debug_timing", {})
    spans = timing.get("spans", [])
    by_id = {s["id"]: s for s in spans}
    total = next((s for s in spans if s["name"] == "record.total"), None)
    origin = total["offset_sec"] if total else 0
    total_sec = total["duration_sec"] if total else report.get("duration_sec")
    rows, groups = [], []

    def interval(span):
        return {"start": span["offset_sec"] - origin, "duration": span["duration_sec"]}

    def descendants(span, name):
        if span is None:
            return []
        found = []
        for child in spans:
            if child["name"] != name:
                continue
            parent = by_id.get(child["parent_id"])
            while parent is not None:
                if parent["id"] == span["id"]:
                    found.append(child)
                    break
                parent = by_id.get(parent["parent_id"])
        return found

    def add(group, kind, title, preview, *, span=None, **extra):
        row = {
            "id": f"event-{len(rows)}",
            "group": group,
            "kind": kind,
            "title": title,
            "preview": preview,
            "start": None,
            "duration": None,
            **extra,
        }
        if span is not None:
            row.update(interval(span))
        rows.append(row)
        return row

    with RecordStore.open(run_dir / "record") as store, store.snapshot():
        runs = store.list(Run)
        if len(runs) != 1:
            raise ValueError("Select a run directory containing exactly one Run")
        run = runs[0]
        nodes = sorted(store.list(Node), key=lambda n: n.index)
        steps = {s.step_id: s for s in store.list(Step)}
        if len(nodes) != len(steps) + 1:
            raise ValueError("This viewer currently requires a fully committed trajectory")
        calls = {e.execution_id: call_document(store, e.execution_id)
                 for e in store.list(Execution)}
        captures = {c.node_id: store.read_json(c.details) for c in store.list(StateCapture)}
        boundaries = {c["node_id"]: c for c in report["capture_boundaries"]}
        node_views = []
        initial = store.read_json(nodes[0].context)
        capture_spans = {s["step"]: s for s in spans if s["name"] == "node.capture_commit"}
        c0_span = capture_spans.get(0)
        c0_start = c0_span["offset_sec"] - origin if c0_span else None
        c0_allocated = captures[nodes[0].node_id].get("storage", {}).get("image_allocated_bytes")
        groups.append(
            {
                "id": "prepare",
                "label": "运行准备",
                "note": "任务 · C0 基座 · 独立容器 · Recorder",
                "duration": c0_start,
            }
        )
        prep = add(
            "prepare",
            "system",
            "启动 Record",
            "核对任务与 C0 基座，从基座创建独立任务环境并创建 Store。",
            start=0 if total else None,
            duration=c0_start,
            data={
                "task_id": run.task_id,
                "agent": run.agent_name,
                "agent_version": run.agent_version,
                "options": report["options"],
                "preload": report.get("preload"),
            },
            source="record/run.py:_run_record",
        )

        for node in nodes:
            i, group = node.index, f"C{node.index}"
            capture_span = capture_spans.get(i)
            d = captures[node.node_id]
            b = boundaries[node.node_id]
            if d.get("capture_mode") != "btrfs-cow-snapshot":
                raise ValueError("This viewer requires Btrfs snapshot capture details")
            allocated = d.get("storage", {}).get("image_allocated_bytes")
            capture_duration = capture_span["duration_sec"] if capture_span else b.get("capture_sec")
            node_info = {
                "label": group,
                "index": i,
                "node_id": node.node_id,
                "parent_node_id": node.parent_node_id,
                "state_id": node.state_id,
                "reuses_c0": i == 0,
                "accepted_event_sequence": node.accepted_event_sequence,
                "duration": capture_duration,
                "mode": d["capture_mode"],
                "capture": d,
                "snapshot": d["snapshot"],
                "image_allocated_bytes": allocated,
                "image_capacity_bytes": d.get("storage", {}).get("image_capacity_bytes"),
                "pool_delta_from_c0_bytes": allocated - c0_allocated
                if allocated is not None and c0_allocated is not None else None,
                "store_delta": b["store_bytes_after_capture"] - b["store_bytes_before_capture"],
                "context": store.read_json(node.context),
                "input_position": store.read_json(node.input_position),
            }
            step_calls = [calls[execution_id] for execution_id in (
                node.model_execution_id, *node.tool_execution_ids
            ) if execution_id is not None]
            tool_calls = [c for c in step_calls if c["operation"]["kind"] == "tool"]
            group_start = min(
                [
                    s["offset_sec"]
                    for s in spans
                    if s.get("step") == i and s["name"] == "model.request"
                ]
                or [capture_span["offset_sec"] if capture_span else None]
            )
            groups.append(
                {
                    "id": group,
                    "label": "初始节点 C0" if i == 0 else f"第 {i} 步",
                    "note": "首次模型调用之前"
                    if i == 0
                    else f"C{i - 1} → C{i} · {len(tool_calls)} 次工具执行",
                    "duration": capture_span["offset_sec"] + capture_duration - group_start
                    if capture_span and group_start is not None else None,
                }
            )
            if i == 0:
                for message in initial["messages"]:
                    add(
                        group,
                        "input",
                        "系统提示词" if message["role"] == "system" else "任务输入",
                        message["content"],
                        data=message,
                        source="DefaultAgent.run / add_messages",
                    )
            else:
                model_spans = [
                    s for s in spans if s["name"] == "model.request" and s.get("step") == i
                ]
                tool_spans = [
                    s for s in spans if s["name"] == "tool.execute" and s.get("step") == i
                ]
                model_calls = [c for c in step_calls if c["operation"]["kind"] == "model"]
                if len(model_calls) != 1:
                    raise ValueError("Unsupported call/timing association; refusing to guess")
                if len(tool_calls) != len(tool_spans):
                    tool_spans = [None] * len(tool_calls)
                adopt_spans = [
                    s
                    for s in spans
                    if s["name"] == "agent.adopt"
                    and group_start is not None and capture_span is not None
                    and group_start <= s["offset_sec"] < capture_span["offset_sec"]
                ]
                for call in step_calls:
                    kind = call["operation"]["kind"]
                    events = {e["kind"]: e for e in call["events"]}
                    result = events.get("result", {})
                    raw = (result.get("content") or {}).get("value")
                    accepted = (events.get("accepted", {}).get("content") or {}).get("value")
                    request = call["request"]["value"]
                    data = {
                        "execution_id": call["execution"]["execution_id"],
                        "operation_id": call["operation"]["operation_id"],
                        "phase": call["phase"],
                        "request": request,
                        "context": (call["context"] or {}).get("value"),
                        "result": raw,
                        "accepted": accepted,
                        "events": [
                            {
                                "kind": e["kind"],
                                "sequence": e["sequence"],
                                "created_at": e["created_at"],
                                "outcome": e["outcome"],
                            }
                            for e in call["events"]
                        ],
                    }
                    if kind == "model":
                        model_span = None
                        if model_spans:
                            first_request, last_request = model_spans[0], model_spans[-1]
                            model_span = first_request | {
                                "duration_sec": last_request["offset_sec"]
                                + last_request["duration_sec"] - first_request["offset_sec"]
                            }
                        message = raw["choices"][0]["message"]
                        actions = message.get("tool_calls") or []
                        preview = (
                            message.get("content")
                            or f"返回 {len(actions)} 个工具动作；完整工具组已声明。"
                        )
                        data["usage"] = raw.get("usage", {})
                        add(
                            group,
                            "model",
                            "模型请求",
                            preview,
                            span=model_span,
                            data=data,
                            source="agents/_mini_native.py:ObservedModel.query / _query",
                        )
                        if adopt_spans:
                            add(
                                group,
                                "accepted",
                                "接纳模型输出",
                                "写入助手消息，记录接纳位置并声明全部 tool_call_id。",
                                span=adopt_spans[0],
                                data={"accepted": accepted, "events": data["events"]},
                                source="RecordingAgent._adopt / declare_tools",
                            )
                    else:
                        tool_index = tool_calls.index(call)
                        details = (result.get("detail_content") or {}).get("value", {})
                        code = details.get("backend", {}).get("decoded", {}).get("returncode")
                        add(
                            group,
                            "tool",
                            request["action"]["command"],
                            str(raw or ""),
                            span=tool_spans[tool_index],
                            data=data,
                            exit_code=code,
                            error=code not in (None, 0),
                            source="agents/_mini_native.py:BorrowedEnvironment.execute",
                        )
                if len(adopt_spans) > 1:
                    accepted_tools = [
                        next(e["content"]["value"] for e in c["events"] if e["kind"] == "accepted")
                        for c in tool_calls
                    ]
                    add(
                        group,
                        "accepted",
                        "接纳整组工具结果",
                        f"{len(tool_calls)} 个工具结果已写入 Agent 上下文；步骤完整性检查通过。",
                        span=adopt_spans[-1],
                        data={"accepted": accepted_tools},
                        source="RecordingAgent._adopt / RecordStore.accepted_step",
                    )
            snapshot = add(
                group,
                "snapshot",
                f"{group} · {'复用 C0 基座' if i == 0 else 'Btrfs 增量快照'}",
                "引用 preload 的只读基座，不重新采集初始文件。" if i == 0
                else "暂停任务进程、同步写入并创建只读快照，共享未变化的数据块。",
                span=capture_span,
                duration=capture_duration,
                node=group,
                data=node_info,
                source="record/recorder.py / record/state.py:BtrfsStateBackend.capture",
            )
            commit_spans = descendants(capture_span, "node.commit")
            commit = add(
                group,
                "commit",
                f"发布完整节点 {group}",
                "上下文、输入位置、文件 State 和调用接纳事实已关联；推进当前节点。",
                span=commit_spans[0] if len(commit_spans) == 1 else None,
                node=group,
                data={
                    "node": node.model_dump(mode="json"),
                    "input_position": store.read_json(node.input_position),
                },
                source="storage/store.py:RecordStore.commit_node",
            )
            node_views.append(
                node_info | {"snapshot_event": snapshot["id"], "commit_event": commit["id"]}
            )

        last = capture_spans.get(nodes[-1].index)
        finish_start = last["offset_sec"] + last["duration_sec"] - origin if last else None
        groups.append(
            {
                "id": "finish",
                "label": "结束与校验",
                "note": report["agent_exit_status"],
                "duration": total_sec - finish_start if finish_start is not None else None,
            }
        )
        end_title = (
            "达到预算，保存 RunEnd"
            if report["agent_exit_status"] == "LimitsExceeded"
            else "保存 RunEnd"
        )
        add(
            "finish",
            "system",
            end_title,
            f"{report['run_end_status']}：已提交节点完整保留；任务评测状态 {report['task_success']}。",
            data={
                "status": report["status"],
                "run_end_status": report["run_end_status"],
                "agent_exit_status": report["agent_exit_status"],
                "task_success": report["task_success"],
            },
            source="RecordingAgent.run / Recorder.pause",
        )
        cleanup = [s for s in spans if s["name"].startswith("cleanup.")]
        cleanup_start = min((s["offset_sec"] for s in cleanup), default=None)
        cleanup_end = max((s["offset_sec"] + s["duration_sec"] for s in cleanup), default=None)
        add(
            "finish",
            "system",
            "清理原容器与连接",
            "关闭沙箱、Store 和 Docker 客户端。",
            start=cleanup_start - origin if cleanup else None,
            duration=cleanup_end - cleanup_start if cleanup else None,
            data={
                "cleanup_confirmed": report["cleanup_confirmed"],
                "errors": report["cleanup_errors"],
            },
            source="record/run.py:finally / sandbox.close",
        )
        verify = next((s for s in spans if s["name"] == "record.verify"), None)
        add(
            "finish",
            "system",
            "关闭后读回校验",
            "检查摘要、引用和轨迹完整性；这一步不执行容器恢复。",
            span=verify,
            data=report["verification"],
            source="record/run.py / record/inspect.py:inspect_record(check=True)",
        )
        add(
            "finish",
            "system",
            "写入报告，返回终端",
            "录制结束；report.json 与 record/ 留在本地。",
            data={
                "status": report["status"],
                "exit_code": report["exit_code"],
                "storage": report["storage"],
            },
            source="record/run.py:run_record / cli/record.py",
        )

    return {
        "schema_version": "palinreplay.record-visualization.v1",
        "run_name": run_dir.name,
        "task_id": run.task_id,
        "model": report["model"],
        "run_id": run.run_id,
        "report_path": str(run_dir / "report.json"),
        "date": report["started_at"],
        "total_sec": total_sec,
        "timing_source": "record.total" if total else "report.duration_sec",
        "timeline_complete": bool(timing.get("timeline_complete")),
        "status": report["status"],
        "run_end_status": report["run_end_status"],
        "agent_exit_status": report["agent_exit_status"],
        "task_success": report["task_success"],
        "verification": report["verification"],
        "storage": report["storage"],
        "totals": timing.get("totals", {}),
        "counts": {"model_calls": sum(c["operation"]["kind"] == "model" for c in calls.values()),
                   "tool_calls": sum(c["operation"]["kind"] == "tool" for c in calls.values())},
        "preload": report.get("preload"),
        "capture_total_sec": sum(n["duration"] for n in node_views)
        if all(n["duration"] is not None for n in node_views) else None,
        "groups": groups,
        "events": rows,
        "nodes": node_views,
        "phases": [
            {
                "name": "启动准备", "start": 0 if total else None,
                "duration": c0_start, "event": prep["id"], "groups": ["prepare"],
            },
            {
                "name": "复用 C0",
                "start": c0_start,
                "duration": node_views[0]["duration"],
                "event": node_views[0]["snapshot_event"],
                "groups": ["C0"],
            },
            {
                "name": f"{len(steps)} 个 Agent Step",
                "start": c0_start + node_views[0]["duration"] if c0_start is not None else None,
                "duration": finish_start - c0_start - node_views[0]["duration"]
                if c0_start is not None and finish_start is not None else None,
                "event": next((e["id"] for e in rows if e["kind"] == "model"),
                              node_views[0]["commit_event"]),
                "groups": [node["label"] for node in node_views[1:]],
            },
            {
                "name": "结束与校验",
                "start": finish_start,
                "duration": total_sec - finish_start if finish_start is not None else None,
                "event": next(e["id"] for e in rows if e["group"] == "finish"),
                "groups": ["finish"],
            },
        ],
    }


def render_record(run_dir: str | Path, output: str | Path) -> dict:
    """Render saved evidence without mounting Btrfs or changing source material."""
    run_dir, output = Path(run_dir), Path(output)
    outside_store(run_dir / "record", output)
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"visualization output already exists: {output}")
    data = build_data(run_dir)
    serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    template = Path(__file__).with_name("record_flow.template.html").read_text()
    if template.count("__RECORD_DATA__") != 1:
        raise ValueError("Expected one embedded data placeholder")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        handle.write(template.replace("__RECORD_DATA__", serialized))
    return data
