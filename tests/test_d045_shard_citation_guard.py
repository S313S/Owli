"""§D-045：新写分片角标越池时只重写该片，合并后仍越池则走节级重试。"""

from __future__ import annotations

import json
from collections.abc import Callable

from tests.test_d031_write_sharding import _shard_run


def _corrupt_new_shard(
    monkeypatch,
    *,
    shard: int,
    times: int,
) -> tuple[list[str], list[str]]:
    """把指定新写片的前 ``times`` 次产物角标改成当前节池外的 S49。"""
    import app.orchestrator.sectioning as sectioning

    original: Callable = sectioning._run_before_section_deadline
    calls: list[str] = []
    prompts: list[str] = []
    seen = 0

    async def spy(adapter, task, ctx, on_event, deadline):
        nonlocal seen
        result = await original(adapter, task, ctx, on_event, deadline)
        name = task.output_path.name
        calls.append(name)
        if f".part.{shard}." not in name:
            return result
        seen += 1
        prompts.append(task.body)
        if seen <= times:
            payload = json.loads(task.output_path.read_text(encoding="utf-8"))
            payload["markdown"] = payload["markdown"].replace(
                f"[S{(shard - 1) * 10 + 1:02d}]", "[S49]", 1,
            )
            task.output_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8",
            )
        return result

    monkeypatch.setattr(sectioning, "_run_before_section_deadline", spy)
    return calls, prompts


def test_d045_新写片角标越池只重写该片并发事件(tmp_path, monkeypatch):
    calls, prompts = _corrupt_new_shard(monkeypatch, shard=2, times=1)

    result, store, _, events, _ = _shard_run(tmp_path, evidence=30)

    assert result.succeeded is True
    assert calls == [
        "sec-1.part.1.md", "sec-1.part.2.md",
        "sec-1.part.2.md", "sec-1.part.3.md",
    ]
    offpool = [event for event in events if event["type"] == "write_shard_offpool"]
    assert len(offpool) == 1
    assert offpool[0]["is_error"] is True
    assert offpool[0]["data"]["shard"] == 2
    assert offpool[0]["data"]["citations"] == ["[S49]"]
    assert offpool[0]["data"]["attempt"] == 1
    assert "只能引用池内角标 S01–S30" in prompts[1]
    assert store.list_chapters("r-ledger")[0]["status"] == "done"


def test_d045_片内重写两次仍越池则节级重试_第二次通过(tmp_path, monkeypatch):
    calls, _ = _corrupt_new_shard(monkeypatch, shard=2, times=3)

    result, store, _, events, _ = _shard_run(tmp_path, evidence=30)

    assert result.succeeded is True
    offpool = [event["data"] for event in events if event["type"] == "write_shard_offpool"]
    assert [(item["shard"], item["attempt"]) for item in offpool] == [(2, 1), (2, 2)]
    retries = [event["data"] for event in events if event["type"] == "section_retry"]
    assert [(item["attempt"], item["resume"]) for item in retries] == [(2, True)]
    assert calls == [
        "sec-1.part.1.md", "sec-1.part.2.md", "sec-1.part.2.md",
        "sec-1.part.2.md", "sec-1.part.3.md", "sec-1.part.2.md",
    ]
    row = store.list_chapters("r-ledger")[0]
    assert (row["status"], row["attempts"]) == ("done", 2)
