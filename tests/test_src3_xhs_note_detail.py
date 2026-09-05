"""§SRC-3：小红书笔记详情二跳（全文 + 绝对发布时间）。

搜索端点只回一段短 `desc` 且不给绝对时间，评级的「完整性」「时效」两维等于在
给标题打分。这里锁住二跳的四条语义：按互动量排、串号不认、失败不阻塞、计费如实。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

from app.sources import xhs


class ImmediateGate:
    def wait(self, **_: Any) -> None:
        return None


def _note(index: int, *, liked: int) -> dict[str, Any]:
    return {
        "model_type": "note",
        "note": {
            "id": f"note-{index}", "title": f"标题 {index}", "desc": "短摘要",
            "type": "normal", "xsec_token": f"signed-{index}=",
            "liked_count": liked, "comments_count": 0,
            "collected_count": 0, "share_count": 0,
            "user": {"nickname": "作者", "userid": "user-1"},
        },
    }


def _detail_payload(note_id: str, *, desc: str, time_value: Any) -> dict[str, Any]:
    return {
        "code": 200,
        "data": {"code": 0, "success": True, "data": [{"note_list": [{
            "id": note_id, "type": "normal", "desc": desc,
            "time": time_value, "ip_location": "Beijing",
        }]}]},
    }


def _search_payload(notes: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "code": 200,
        "data": {"code": 200, "success": True,
                 "data": {"items": notes}, "next_page": None},
    }


def _run(http_get, **kwargs: Any) -> list[dict[str, Any]]:
    return xhs.search(
        "飞书", "7d", limit=3, token="runtime-secret", http_get=http_get,
        rate_gate=ImmediateGate(),
        now=lambda: datetime(2026, 9, 5, tzinfo=timezone.utc), **kwargs,
    )


def test_详情二跳按互动量取前N条_回填全文与绝对时间() -> None:
    notes = [_note(1, liked=5), _note(2, liked=900), _note(3, liked=50)]
    seen: list[str] = []

    def http_get(url, headers, timeout):
        del headers, timeout
        if "get_image_note_detail" in url:
            note_id = parse_qs(urlparse(url).query)["note_id"][0]
            seen.append(note_id)
            return xhs.HttpResponse(200, _detail_payload(
                note_id, desc="全文" * 1200 + "#飞书[话题]#", time_value=1788401161,
            ))
        return xhs.HttpResponse(200, _search_payload(notes))

    events: list[dict[str, Any]] = []
    result = _run(http_get, detail_top_n=2, on_event=events.append)

    # 只补前 2 条，且是互动量最高的两条（note-2 900 赞、note-3 50 赞）。
    assert seen == ["note-2", "note-3"]
    by_id = {item["platform_item_id"]: item for item in result}
    assert by_id["note-2"]["published_at"] == "2026-09-03T02:06:01+00:00"
    # 全文截 2000 字，话题的 `[话题]#` 尾巴被抹平。
    assert len(by_id["note-2"]["content_excerpt"]) == 2000
    assert "[话题]" not in by_id["note-2"]["content_excerpt"]
    assert by_id["note-2"]["extra"]["detail_hop"] is True
    assert by_id["note-2"]["extra"]["ip_location"] == "Beijing"
    # 没轮到二跳的那条：保留搜索行形态，不伪造时间。
    assert by_id["note-1"]["content_excerpt"] == "短摘要"
    assert by_id["note-1"]["published_at"] is None
    assert by_id["note-1"]["extra"]["detail_hop"] is False
    usage = next(e for e in events if e["type"] == "source_usage_reconciled")
    assert usage["data"]["calls"] == {"search_notes": 1, "get_image_note_detail": 2}
    assert (usage["data"]["detail_filled"], usage["data"]["detail_failed"]) == (2, 0)


def test_详情串号不认_失败不阻塞整轮搜索() -> None:
    notes = [_note(1, liked=900), _note(2, liked=5)]

    def http_get(url, headers, timeout):
        del headers, timeout
        if "get_image_note_detail" in url:
            note_id = parse_qs(urlparse(url).query)["note_id"][0]
            if note_id == "note-1":
                # 真机实测过的坑：HTTP 200 + code 200，回来的却是另一条笔记。
                return xhs.HttpResponse(200, _detail_payload(
                    "note-别人家的", desc="别人家的全文", time_value=1788401161,
                ))
            return xhs.HttpResponse(200, _detail_payload(
                note_id, desc="自己的全文", time_value=1788401161,
            ))
        return xhs.HttpResponse(200, _search_payload(notes))

    events: list[dict[str, Any]] = []
    result = _run(http_get, detail_top_n=2, on_event=events.append)

    assert len(result) == 2
    by_id = {item["platform_item_id"]: item for item in result}
    # 串号那条既不落别人家的正文，也不落别人家的时间——退回搜索行。
    assert by_id["note-1"]["content_excerpt"] == "短摘要"
    assert by_id["note-1"]["published_at"] is None
    assert by_id["note-2"]["content_excerpt"] == "自己的全文"
    failure = next(e for e in events if e["type"] == "source_partial_failure")
    assert failure["data"]["stage"] == "note_detail"
    assert failure["data"]["task_continues"] is True
    usage = next(e for e in events if e["type"] == "source_usage_reconciled")
    # 计费如实：两次详情请求都发出去了，失败那次照样算钱。
    assert usage["data"]["calls"]["get_image_note_detail"] == 2
    assert (usage["data"]["detail_filled"], usage["data"]["detail_failed"]) == (1, 1)
