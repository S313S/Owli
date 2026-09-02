"""§D-035：节级重试后全片跳过（盘上已有完整合并稿）却返回 None 被判 empty_result。

真跑证据（r-e74541583a05）：goal-2 ch-5/sec-3 片 1/片 4 的末次尝试已把 part 文件
落盘才撞片墙钟；D-033 的节级重试 resume 后 4 片全 `write_shard_skipped`、
`write_shards_merged done=4`，但 `_run_section_shards` 没跑过任何一片，返回
`(None, section_task)`，节循环把 None 判成 empty_result，22.7 KB 合并稿进
`.rejected.md`。
"""

from __future__ import annotations

import json

from tests.test_d031_write_sharding import _shard_run


def _expire_after_write(monkeypatch, marker: str) -> list[str]:
    """指定片**写完产物之后**才撞片墙钟：模拟末次尝试落盘后超时。

    返回真正起过引擎会话的片名单（按调用序），用来断言重试里零引擎调用。
    """
    import app.orchestrator.sectioning as sectioning

    original = sectioning._run_before_section_deadline
    calls: list[str] = []
    seen = {"n": 0}

    async def spy(adapter, task, ctx, on_event, deadline):
        calls.append(task.output_path.name)
        result = await original(adapter, task, ctx, on_event, deadline)
        if marker in task.output_path.name:
            seen["n"] += 1
            if seen["n"] == 1:
                raise sectioning.SectionWallClockExpired(f"{marker} 写完才跑满片墙钟")
        return result

    monkeypatch.setattr(sectioning, "_run_before_section_deadline", spy)
    return calls


def test_d035_全片跳过且合并稿在盘_节判done(tmp_path, monkeypatch):
    _expire_after_write(monkeypatch, ".part.2.")
    result, store, _, events, output = _shard_run(
        tmp_path, evidence=20, wall_clock=330.0,
    )

    # 节级重试里两片都在盘上 → 全 skipped、merged done=2。
    assert [
        e["data"]["shard"] for e in events if e["type"] == "write_shard_skipped"
    ] == [1, 2]
    merged = [e["data"] for e in events if e["type"] == "write_shards_merged"]
    assert merged[-1]["done"] == 2 and merged[-1]["shards"] == 2
    # 本次尝试就该拿盘上的合并稿收尾，而不是 None → empty_result。
    assert result.succeeded is True
    row = store.list_chapters("r-ledger")[0]
    assert (row["status"], row["reason"]) == ("done", None)
    # 正文 = 合并稿（两片都在，claims 按片序拼上），且没被挪进 .rejected.md。
    section_path = output.parent / output.stem / "sec-1.md"
    section = json.loads(section_path.read_text(encoding="utf-8"))
    assert section["markdown"].count("## 结论") == 1
    assert "第 1 片判断" in section["markdown"]
    assert "第 2 片判断" in section["markdown"]
    assert [claim["id"] for claim in section["claims"]] == ["c-0101", "c-0201"]
    assert not (section_path.parent / "sec-1.rejected.md").exists()


def test_d035_全片跳过不再调引擎(tmp_path, monkeypatch):
    """出口修正是纯判定，不新增任何引擎调用：全跳过那次尝试零会话。"""
    calls = _expire_after_write(monkeypatch, ".part.2.")
    result, _, _, events, _ = _shard_run(tmp_path, evidence=20, wall_clock=330.0)

    assert result.succeeded is True
    # 首次尝试起了 2 片；第二次节尝试一次会话都没起。
    assert calls == ["sec-1.part.1.md", "sec-1.part.2.md"]
    assert [
        e["data"]["shard"] for e in events if e["type"] == "write_shard_started"
    ] == [1, 2]


def test_d035_guard_有坏片未落盘仍重写(tmp_path, monkeypatch):
    """闸只对「全片跳过」开：有片没落盘时照旧重写那一片，不许直接判 done。"""
    from tests.test_d033_shard_wall_clock_section_retry import _expire_shard

    _expire_shard(monkeypatch, ".part.2.")
    result, store, bodies, events, _ = _shard_run(
        tmp_path, evidence=20, wall_clock=330.0,
    )

    assert [
        e["data"]["shard"] for e in events if e["type"] == "write_shard_skipped"
    ] == [1]
    started = [e["data"]["shard"] for e in events if e["type"] == "write_shard_started"]
    assert started == [1, 2, 2]
    assert list(bodies) == ["sec-1.part.1.md", "sec-1.part.2.md"]
    assert result.succeeded is True
    assert store.list_chapters("r-ledger")[0]["status"] == "done"
