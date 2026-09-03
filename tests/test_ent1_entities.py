"""§ENT-1 货 1：规划期实体卡解析——上限、降级与网页线索。"""

from __future__ import annotations

import asyncio
from typing import Any


class FakeWorkspace:
    """只记提示词、按预置回答吐 JSON 的规划段替身。"""

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.prompts: dict[str, str] = {}

    async def generate(self, name: str, prompt: str, adapter: Any) -> Any:
        del adapter
        self.prompts[name] = prompt
        answer = self.answers.get(name, {})
        if isinstance(answer, Exception):
            raise answer
        return answer


def _card(name: str, en: str, **extra: Any) -> dict[str, Any]:
    return {
        "canonical": name,
        "names": {"zh": name, "en": en, "aliases": extra.pop("aliases", [])},
        "official_handles": {},
        "same_product": extra.pop("same_product", True),
        "note": extra.pop("note", f"{name} 是一个产品"),
    }


def test_实体卡上限五个且多出来的实体不查不问() -> None:
    from app.plan.entities import MAX_ENTITIES, resolve_entities

    subjects = [f"产品{index}" for index in range(1, 9)]
    workspace = FakeWorkspace({
        f"entity-{index}": _card(f"产品{index}", f"P{index}")
        for index in range(1, 9)
    })
    searched: list[str] = []

    def search(query: str, window: str, *, max_results: int) -> list:
        del window, max_results
        searched.append(query)
        return []

    cards = asyncio.run(resolve_entities(
        "八个产品的口碑对比", subjects, workspace, None, search=search,
    ))
    assert len(cards) == MAX_ENTITIES == 5
    assert [card["id"] for card in cards] == subjects[:5]
    assert len(searched) == 5 and len(workspace.prompts) == 5


def test_网页查不动照样出卡且线索进提示词() -> None:
    from app.plan.entities import resolve_entities

    workspace = FakeWorkspace({"entity-1": _card("豆包", "Doubao")})

    def broken(query: str, window: str, *, max_results: int) -> list:
        del query, window, max_results
        raise RuntimeError("EXA_API_KEY 缺失")

    cards = asyncio.run(resolve_entities(
        "国内大家对豆包的看法", ["豆包"], workspace, None, search=broken,
    ))
    assert cards[0]["names"] == {"zh": "豆包", "en": "Doubao", "aliases": []}
    assert "这次没查到可用的网页线索" in workspace.prompts["entity-1"]

    workspace = FakeWorkspace({"entity-1": _card("豆包", "Doubao")})
    hit = [{
        "title": "豆包官网", "permalink": "https://www.doubao.com/",
        "content_excerpt": "豆包（Doubao）是字节跳动推出的 AI 助手",
    }]
    asyncio.run(resolve_entities(
        "国内大家对豆包的看法", ["豆包"], workspace, None,
        search=lambda *args, **kwargs: hit,
    ))
    assert "https://www.doubao.com/" in workspace.prompts["entity-1"]


def test_单张卡失败只丢这一张且整步降级不阻塞规划() -> None:
    from app.plan.entities import resolve_entities

    workspace = FakeWorkspace({
        "entity-1": RuntimeError("规划段预算耗尽"),
        "entity-2": {"canonical": ""},          # 模型自认「不是真实产品」
        "entity-3": _card("飞书", "Feishu"),
    })
    progress: list[str] = []
    cards = asyncio.run(resolve_entities(
        "三个产品", ["钉钉", "国内", "飞书"], workspace, None,
        on_progress=progress.append, search=None,
    ))
    assert [card["id"] for card in cards] == ["飞书"]
    assert any("钉钉" in text and "跳过" in text for text in progress)
    assert any("国内" in text and "跳过" in text for text in progress)


def test_实体卡随计划落盘且规划期不因它变慢地打网络() -> None:
    """整条规划链：实体卡进 plan JSON；没注入 search 就一次网都不打。"""
    from tests.test_plan_generate import _agent, _generate, _valid_skeleton
    import tempfile
    from pathlib import Path

    skeleton = _valid_skeleton()
    # §ENT-2：夹具的实体卡默认只有中文名，本用例要验的正是 en 原样落盘，所以显式
    # 打开双语——而带中文叫法的实体在 global_product 下会被分配表再排一张国内源卡，
    # 骨架得把那张卡也写上，否则规则 31 打回（分配表按叫法定有无是本包的新语义）。
    skeleton["goals"][1]["agents"].insert(
        0, _agent("小红书数据抓取·飞书", "采集研究主体的国内讨论"),
    )
    with tempfile.TemporaryDirectory() as raw:
        plan, _, engine = _generate(
            Path(raw), [skeleton], bilingual_entities=True,
        )
    entities = plan.to_dict()["entities"]
    assert [item["id"] for item in entities] == plan.subjects
    assert entities[0]["names"]["en"].endswith("-en")
    assert len(engine.entity_tasks) == len(plan.subjects)


def _plan_with_entities() -> dict:
    """一份带两张实体卡（抖音 / TikTok）与一张抖音采集卡的计划。"""
    from tests.plan_factory import make_plan_dict

    plan = make_plan_dict()
    plan["subjects"] = ["抖音", "TikTok"]
    plan["subjects_justification"] = "题面要求对比国内外两个短视频产品。"
    plan["entities"] = [
        {
            "id": "抖音", "canonical": "抖音",
            "names": {"zh": "抖音", "en": "Douyin", "aliases": ["抖音短视频"]},
            "official_handles": {}, "same_product": False,
            "note": "抖音与 TikTok 是字节面向国内与海外的两个独立产品，内容生态不互通。",
        },
        {
            "id": "TikTok", "canonical": "TikTok",
            "names": {"zh": None, "en": "TikTok", "aliases": []},
            "official_handles": {}, "same_product": False,
            "note": "TikTok 是面向海外市场的独立产品。",
        },
    ]
    agent = plan["goals"][0]["agents"][0]
    agent["entity"] = "抖音"
    agent["capability"]["profile"] = "web-collector"
    return plan


def test_规则32_采集卡实体必须有卡且错误带闭集() -> None:
    from app.plan.lint import lint

    plan = _plan_with_entities()
    assert [item for item in lint(plan)["errors"] if "[规则32]" in item] == []

    plan["goals"][0]["agents"][0]["entity"] = "快手"
    matches = [item for item in lint(plan)["errors"] if "[规则32]" in item]
    assert len(matches) == 1
    assert "entity=快手 不在实体卡闭集里" in matches[0]
    assert "可选值：TikTok、抖音" in matches[0]


def test_规则32_采集卡不许写别的实体的叫法() -> None:
    from app.plan.lint import lint

    plan = _plan_with_entities()
    agent = plan["goals"][0]["agents"][0]
    agent["task"] = "采集抖音上关于 TikTok 的用户评价，近 30 天，至少 20 条。"
    matches = [item for item in lint(plan)["errors"] if "[规则32]" in item]
    assert len(matches) == 1
    assert "实体 TikTok 的叫法：TikTok" in matches[0]
    assert "本卡只能写 抖音 的这些叫法：抖音、Douyin、抖音短视频" in matches[0]

    # 章 opening.task 是引擎真正照着跑的那段，同样要守。
    agent["task"] = "采集抖音的用户评价，近 30 天，至少 20 条。"
    agent["chapter"]["opening"]["task"] = "顺便把 TikTok 的评价一起采了。"
    assert [item for item in lint(plan)["errors"] if "[规则32]" in item]


def test_规则32_不误伤本实体别名与长名里的短名() -> None:
    from app.plan.lint import lint

    plan = _plan_with_entities()
    agent = plan["goals"][0]["agents"][0]
    agent["task"] = "采集抖音短视频（Douyin）上的用户评价，近 30 天，至少 20 条。"
    assert [item for item in lint(plan)["errors"] if "[规则32]" in item] == []

    # 「飞书妙记」里的「飞书」不算写了别人的名字。
    plan["subjects"] = ["飞书妙记", "飞书"]
    plan["entities"] = [
        {"id": "飞书妙记", "canonical": "飞书妙记",
         "names": {"zh": "飞书妙记", "en": "Feishu Minutes", "aliases": []},
         "official_handles": {}, "same_product": True, "note": "飞书旗下的会议纪要工具"},
        {"id": "飞书", "canonical": "飞书",
         "names": {"zh": "飞书", "en": "Feishu", "aliases": []},
         "official_handles": {}, "same_product": False,
         "note": "飞书与 Lark 是面向国内与海外的两套部署，数据不互通。"},
    ]
    agent["entity"] = "飞书妙记"
    agent["task"] = "采集飞书妙记的用户评价，近 30 天，至少 20 条。"
    assert [item for item in lint(plan)["errors"] if "[规则32]" in item] == []


def test_规则32_实体卡为空时整条规则不作数() -> None:
    """历史计划与解析失败的计划 entities=[]，规则 32 必须一声不吭。"""
    from app.plan.lint import lint
    from tests.plan_factory import make_plan_dict

    plan = make_plan_dict()
    plan["goals"][0]["agents"][0].update(entity="谁都没登记过的东西")
    plan["goals"][0]["agents"][0]["capability"]["profile"] = "web-collector"
    assert [item for item in lint(plan)["errors"] if "[规则32]" in item] == []


def test_实体叫法闭集写进_goal_提示词() -> None:
    """§ENT-1 货 3：规则 32 要能被遵守，模型得先在提示词里看到闭集。"""
    from app.plan.generate import _entity_rule

    text = _entity_rule([
        {"id": "抖音", "names": {"zh": "抖音", "en": "Douyin", "aliases": ["抖音短视频"]},
         "same_product": False},
        {"id": "豆包", "names": {"zh": "豆包", "en": "Doubao", "aliases": []},
         "same_product": True},
    ])
    assert "抖音→抖音、Douyin、抖音短视频（与它的中外同名产品不是同一个产品" in text
    assert "交叉验证章对它只并列不跨市场交叉" in text
    assert "豆包→豆包、Doubao；" in text or "豆包→豆包、Doubao。" in text
    assert _entity_rule([]) == "" and _entity_rule(None) == ""


def test_JSON_成稿也带研究对象节且排在最前() -> None:
    """§ENT-1 货 6：报告章多数声明 format=json，走的是 _write_object_document。"""
    from app.orchestrator.sectioning import _write_object_document
    from app.plan.model import Entity
    from app.report.render import parse_report
    import json as _json
    import tempfile
    from pathlib import Path
    from types import SimpleNamespace

    plan = SimpleNamespace(title="国内大家对 workbuddy 的看法", entities=[
        Entity.from_dict({
            "id": "飞书", "canonical": "飞书",
            "names": {"zh": "飞书", "en": "Feishu", "aliases": []},
            "official_handles": {}, "same_product": False,
            "note": "飞书与 Lark 是面向国内与海外的两套部署，数据不互通。",
        }),
    ])
    agent = SimpleNamespace(
        chapter={"chapter_id": "ch-6"}, goal_id="goal-1",
        output={"path": "goals/goal-1/x.json", "shape": "object"},
    )
    with tempfile.TemporaryDirectory() as raw:
        target = Path(raw) / "x.json"
        _write_object_document(
            plan=plan, agent=agent, goal_id="goal-1", output_path=target,
            section_items=[{
                "section_id": "ch-6/sec-1", "goal_id": "goal-1", "title": "一节",
                "markdown": "## 一节\n\n正文 [S01]", "done": True,
            }],
            missing_items=[],
        )
        document = _json.loads(target.read_text(encoding="utf-8"))
        view = parse_report(target.read_text(encoding="utf-8"))
    assert document["sections"][0]["title"] == "研究对象"
    assert document["sections"][0]["section_id"] == "ch-6/entities"
    # goal_id 非空是 sectioned_document_valid 的硬要求（重放实证：填 None 整章作废）
    assert document["sections"][0]["goal_id"] == "goal-1"
    assert "只并列不交叉" in document["sections"][0]["markdown"]
    assert [item["name"] for item in view["entities"]] == ["飞书"]
    assert view["entities"][0]["same_product"] is False


def test_带研究对象节的_JSON_信封过得了_sectioned_document_valid() -> None:
    """§ENT-1 货 6：系统自己插的那一节也得过生产校验器——沙盒重放曾被它整章打回。

    校验器是禁区文件里的生产实现，这里直接调它，不另写一份判定。
    """
    import json as _json
    import tempfile
    from pathlib import Path
    from types import SimpleNamespace

    from app.adapters.validation import Ctx, Verdict, sectioned_document_valid
    from app.orchestrator.sectioning import _write_object_document
    from app.plan.model import Entity

    plan = SimpleNamespace(title="国内大家对 workbuddy 的看法", entities=[
        Entity.from_dict({
            "id": "钉钉", "canonical": "钉钉",
            "names": {"zh": "钉钉", "en": "DingTalk", "aliases": []},
            "official_handles": {}, "same_product": True, "note": "阿里的协作平台",
        }),
    ])
    agent = SimpleNamespace(
        chapter={"chapter_id": "ch-6"},
        output={"path": "goals/goal-1/x.json", "shape": "object"},
    )
    with tempfile.TemporaryDirectory() as raw:
        target = Path(raw) / "x.json"
        _write_object_document(
            plan=plan, agent=agent, goal_id="goal-1", output_path=target,
            section_items=[{
                "section_id": "ch-6/sec-1", "goal_id": "goal-1", "title": "一节",
                "markdown": "## 一节\n\n正文", "done": True,
            }],
            missing_items=[],
        )
        ctx = Ctx(
            output_path=target, output_format="json", research_id="r-1",
            goal_id="goal-1", agent_id="report-writing",
            read_text=lambda: target.read_text(encoding="utf-8"),
            read_json=lambda: _json.loads(target.read_text(encoding="utf-8")),
            store=None, source_domains=frozenset(),
        )
        result = sectioned_document_valid(ctx, [])
    assert result.verdict is Verdict.PASS, result.message


def test_中英同名时研究对象节不重复列名() -> None:
    """真机样本 WorkBuddy 的 zh 与 en 都是 WorkBuddy，渲染出「WorkBuddy、WorkBuddy」。"""
    from app.report.markdown import render_entity_section

    lines = render_entity_section([{
        "id": "WorkBuddy", "canonical": "WorkBuddy",
        "names": {"zh": "WorkBuddy", "en": "WorkBuddy", "aliases": ["workbuddy", "WorkBuddy"]},
        "same_product": True, "note": "腾讯云的 AI 工作助理。",
    }])
    assert "WorkBuddy、workbuddy。" in "\n".join(lines)
