"""§D-051：片失败了节不许判 done。

真机 `r-7e498778d36b`（8956 补 goal-3/ch-6/sec-1）：一节切 4 片，
第 1 片写出坏 JSON（6 388 B，第 224 字符引号未转义）、第 3 片断连没落文件，
事件如实报了两条 `write_shard_finished succeeded=false` 与
`write_shards_merged {"shards":4,"done":2}`，可这一节仍被记成
`status=done attempts=1`，拿第 2、4 片合成正文交差——池 30 条只有 15 条进了报告。

真因：`_run_section_shards` 里「这片成没成」是**局部变量**
（`result.succeeded and _shard_envelope(...) is not None`），而 `first_failure`
存的是**引擎原始 result**；坏 JSON 片的引擎自认跑成（写了文件、engine_error 空），
那个对象的 `.succeeded` 仍是 True，交回节循环就判本次尝试成功。
"""

from __future__ import annotations

import json

from tests.test_d031_write_sharding import _shard_run


def _bad_json_shard(monkeypatch, marker: str, *, times: int | None = None):
    """让指定片的前 times 次尝试写出**坏 JSON** 并让引擎自认跑成。

    times=None 表示每次都坏。拦在 `_run_before_section_deadline` 上，
    夹具那一层不动（`_shard_run` 的假引擎与断言禁改）。
    坏法照真机现场：正文里有未转义的引号，`json.loads` 解不开。
    """
    import app.orchestrator.sectioning as sectioning
    from app.adapters import validation
    from app.adapters.contracts import EngineRunResult, OwliResult

    original = sectioning._run_before_section_deadline
    seen = {"n": 0}

    async def spy(adapter, task, ctx, on_event, deadline):
        if marker in task.output_path.name:
            seen["n"] += 1
            if times is None or seen["n"] <= times:
                task.output_path.parent.mkdir(parents=True, exist_ok=True)
                task.output_path.write_text(
                    '{"markdown": "## 结论\\n\\n- 他说"很好用"就走了 [S01]",'
                    ' "claims": []}',
                    encoding="utf-8",
                )
                return EngineRunResult(
                    conclusion=OwliResult(
                        "done", str(task.output_path), "完成", [], [], [], None,
                    ),
                    conclusion_error=None,
                    validation=validation.validate(ctx, task.validators),
                    events=[], permission_denials=[],
                )
        return await original(adapter, task, ctx, on_event, deadline)

    monkeypatch.setattr(sectioning, "_run_before_section_deadline", spy)
    return seen


def test_d051_坏片与缺片让节不判done(tmp_path, monkeypatch):
    """货 1 单元 a：一片坏 JSON + 一片没落文件 → 这一节不许判 done。

    旧码在这里给 `status=done attempts=1`（真机现场），拿 2/4 片交差。
    """
    # 夹具切得出 3 片（池按 §D-025 截到 30 条 ÷ 每片 10 条）；真机是 4 片
    # （真实证据条更胖，先撞 6 000 B 那道封顶切成 7/7/8/8）。坏片形态一样：
    # 一片引擎自认跑成但产物不可解析、一片没落文件。
    _bad_json_shard(monkeypatch, ".part.1.")
    result, store, _, events, output = _shard_run(
        tmp_path, evidence=30, fail_shards=(3,), wall_clock=330.0,
    )

    merged = [e["data"] for e in events if e["type"] == "write_shards_merged"]
    assert (merged[0]["shards"], merged[0]["done"]) == (3, 1)
    incomplete = [
        e["data"] for e in events if e["type"] == "section_shards_incomplete"
    ]
    assert incomplete and incomplete[0]["failed"] == [1, 3]
    assert (incomplete[0]["shards"], incomplete[0]["done"]) == (3, 1)
    # 节循环拿到的是失败结果：节级重试事件非空，最终不是 done。
    assert [e["data"]["attempt"] for e in events if e["type"] == "section_retry"]
    row = store.list_chapters("r-ledger")[0]
    assert row["status"] != "done"
    # 乙口径（调度 09-05 晚批）：闭集不动，reason 沿 retry_exhausted，
    # 「几片没写成、哪几片」在事件与 conclusion_error 文字里。
    assert row["reason"] == "retry_exhausted"
    assert "节分片未写全" in (row["conclusion_error"] or "")
    assert result.succeeded is False


def test_d051_有片失败不产done级合并稿(tmp_path, monkeypatch):
    """货 3 单元 c：`done < shards` 的合并稿不许当成节产物留在盘上。"""
    _bad_json_shard(monkeypatch, ".part.1.")
    _, store, _, events, output = _shard_run(
        tmp_path, evidence=30, fail_shards=(3,), wall_clock=330.0,
    )

    import app.orchestrator.sectioning as sectioning

    section_dir = output.parent / output.stem
    merged = [e["data"] for e in events if e["type"] == "write_shards_merged"]
    # 不变量：有片没写成，合并稿就一定不满片——合得出 done 级稿的只有全片都在。
    assert merged and all(item["done"] < item["shards"] for item in merged)
    # 半份合并稿被挪进 .rejected.md（第 2 片的字确实在里面），节产物位上留的是
    # 占位；conclusion 与证据池两道闸再读 sec-1.md 读到的不是那半份稿。
    rejected = (section_dir / "sec-1.rejected.md").read_text(encoding="utf-8")
    assert "第 2 片判断" in rejected
    placeholder = (section_dir / "sec-1.md").read_text(encoding="utf-8")
    assert "第 2 片判断" not in placeholder
    assert sectioning._shard_envelope(section_dir / "sec-1.md") is None
    assert store.list_chapters("r-ledger")[0]["status"] != "done"


def test_d051_四片全成行为不变(tmp_path):
    """对照：没有坏片时，事件与账本与本卡之前逐字相同。"""
    result, store, _, events, _ = _shard_run(
        tmp_path, evidence=30, wall_clock=330.0,
    )

    types = [e["type"] for e in events]
    assert "section_shards_incomplete" not in types
    assert "write_shard_unparseable" not in types
    merged = [e["data"] for e in events if e["type"] == "write_shards_merged"]
    assert merged[0] == {
        "goal_id": "goal-1", "chapter_id": "ch-report/sec-1",
        "shards": 3, "done": 3,
    }
    assert result.succeeded is True
    row = store.list_chapters("r-ledger")[0]
    assert (row["status"], row["reason"]) == ("done", None)


def test_d051_坏片当场重写一次就写成(tmp_path, monkeypatch):
    """货 2 单元 b 前半：第一次坏 JSON、第二次好 → 片成，事件 1 条。

    旧码在这里一次都不补（`_is_transport_failure` 认不出「产物不可解析」），
    直接跳下一片，真机 attempts=1 即证。
    """
    _bad_json_shard(monkeypatch, ".part.1.", times=1)
    result, store, bodies, events, _ = _shard_run(
        tmp_path, evidence=30, wall_clock=330.0,
    )

    unparseable = [
        e["data"] for e in events if e["type"] == "write_shard_unparseable"
    ]
    assert [(i["shard"], i["attempt"]) for i in unparseable] == [(1, 1)]
    assert unparseable[0]["bytes"] > 0 and unparseable[0]["error"]
    # 重写用的是同一份片 prompt，只多一句定向要求。
    assert "【产物不可解析重写】" in bodies["sec-1.part.1.md"]
    assert "你现在写第 1/3 片" in bodies["sec-1.part.1.md"]
    finished = [e["data"] for e in events if e["type"] == "write_shard_finished"]
    assert [(i["shard"], i["succeeded"]) for i in finished] == [
        (1, True), (2, True), (3, True),
    ]
    # 补的是这一片，不是整节：没惊动节级重试。
    assert [e for e in events if e["type"] == "section_retry"] == []
    assert [e for e in events if e["type"] == "section_shards_incomplete"] == []
    merged = [e["data"] for e in events if e["type"] == "write_shards_merged"]
    assert merged[0]["done"] == 3
    assert result.succeeded is True
    assert store.list_chapters("r-ledger")[0]["status"] == "done"


def test_d051_坏片重写还坏就记失败(tmp_path, monkeypatch):
    """货 2 单元 b 后半：两次都坏 → 这片记失败，交给货 1 那条路。"""
    _bad_json_shard(monkeypatch, ".part.1.")
    result, store, _, events, _ = _shard_run(
        tmp_path, evidence=30, wall_clock=330.0,
    )

    unparseable = [
        e["data"] for e in events if e["type"] == "write_shard_unparseable"
    ]
    # 上限 1 次：每次节尝试只多付一次引擎，不是无限重写。
    assert unparseable and all(
        item["shard"] == 1 and item["attempt"] == 1 for item in unparseable
    )
    finished = [e["data"] for e in events if e["type"] == "write_shard_finished"]
    first = [item for item in finished if item["shard"] == 1]
    assert first and all(item["succeeded"] is False for item in first)
    assert first[0]["attempts"] == 2
    # 货 1 接手：这一节不判 done。
    assert [e for e in events if e["type"] == "section_shards_incomplete"]
    assert result.succeeded is False
    assert store.list_chapters("r-ledger")[0]["status"] != "done"
