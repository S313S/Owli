"""§SEL-4：海外源放行——可进关键发现与建议，但必须标明是海外的声音。

用户 2026-09-18 拍乙（decision-log 同日 13:1x）。⚠️ 这是**口径翻案**，不是修 bug：
§RPT-4 货 3「与题面对不上的证据不进摘要关键发现」的结论在**海外平台**这一支上退场，
**对照实体那一支原封不动**。本文件里凡是与旧口径相反的断言，锁的都是被替换的语义。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _ruler():
    spec = importlib.util.spec_from_file_location(
        "check_polished_sel4", ROOT / "scripts" / "acceptance" / "rpt1" / "check_polished.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- 货 1 两支拆开

def test_货1_两支各问各的_判定一字未改_变的只是待遇() -> None:
    from app.report.polish.tables import is_contrast_source, is_overseas_source

    # 海外：题面带「只看国内」的词 × 平台在名单里。两个条件缺一不可。
    for platform in ("reddit", "x", "hacker_news", "product_hunt"):
        assert is_overseas_source({"platform": platform},
                                  question="国内大家对豆包的看法") is True
    assert is_overseas_source({"platform": "reddit"}, question="大家怎么看豆包") is False
    assert is_overseas_source({"platform": "xhs"}, question="国内大家对豆包的看法") is False
    for marker in ("国内", "中国用户", "国人"):
        assert is_overseas_source({"platform": "reddit"}, question=f"{marker}怎么看") is True
    # 对照实体：只看实体，⛔ 不看平台、不看题面地域。
    assert is_contrast_source(contrast=True) is True
    assert is_contrast_source(contrast=False) is False
    assert is_contrast_source(contrast=None) is False, "没登记实体 ≠ 是别家"


def test_货1_信息源池里两栏字面分得开_海外那栏不含旁证二字() -> None:
    """⚠️ 写手**只读得到池里那一行字**。规则说「海外可以进关键发现」而栏名写着
    「旁证·海外平台」，写手会按栏名办——栏名离他更近。⇒ 栏名必须先跟口径对齐。
    """
    from app.report.polish.tables import CONTRAST_COLUMN, OVERSEAS_COLUMN

    assert OVERSEAS_COLUMN == "海外社区"
    assert "旁证" not in OVERSEAS_COLUMN
    assert CONTRAST_COLUMN.startswith("旁证·"), "对照实体那一支的说法照旧"


def test_货1_出稿数据里海外走布尔位_offtopic字段只剩对照实体() -> None:
    """⛔ 两支不许再揉回一个字符串：揉在一起，下游只问得出「它旁证吗」。"""
    from app.report.polish.tables import build_tables

    plan = {"research_question": "国内大家对豆包的看法（对比 DeepSeek）",
            "goals": [], "entities": ["豆包"], "subjects": ["豆包"]}
    report = {"id": "r-sel4", "title": "T"}
    evidence = [
        {"id": "ev-1", "citation_no": 1, "platform": "hacker_news", "agent_name": "a-1"},
        {"id": "ev-2", "citation_no": 2, "platform": "xhs", "agent_name": "a-2"},
    ]
    view = {"title": "T", "sources": [{"citation_no": 1, "title": "HN", "url": "u1"},
                                      {"citation_no": 2, "title": "小红书", "url": "u2"}]}
    data = build_tables(report=report, plan=plan, evidence=evidence, claims=[], view=view)
    by_mark = {s["mark"]: s for s in data["sources"]}
    assert by_mark["S01"]["overseas"] is True and by_mark["S01"]["offtopic"] is None
    assert by_mark["S02"]["overseas"] is False and by_mark["S02"]["offtopic"] is None


# ---------------------------------------------------------------- 货 2 线索块

def test_货2_线索块只排除对照实体_海外照收() -> None:
    """⚠️ 锁的正是被替换的旧语义：改前第 3 条筛法排除「对照实体 + 海外平台」。

    另外三条筛法（SINGLE / firsthand / _CLUE_NOISE）⛔ 一字未动，本条一并复核——
    它们都不读 platform、不读题面，判域与地域没有交集。
    """
    from app.report.polish.tables import _clues

    rows = [{"id": "ev-hn", "citation_no": 11, "platform": "hacker_news"},
            {"id": "ev-ds", "citation_no": 12, "platform": "xhs"},
            {"id": "ev-cn", "citation_no": 13, "platform": "weibo"}]
    claims = [
        {"verdict": "SINGLE", "firsthand": ["ev-hn"], "evidence_ids": ["ev-hn"],
         "text": "海外开发者抱怨素材里遗留了豆包水印。"},
        {"verdict": "SINGLE", "firsthand": ["ev-ds"], "evidence_ids": ["ev-ds"],
         "text": "有用户反馈 DeepSeek 的联网检索更稳。"},
        {"verdict": "SINGLE", "firsthand": ["ev-cn"], "evidence_ids": ["ev-cn"],
         "text": "微博用户自述每天和豆包打视频电话。"},
    ]
    texts = [c["text"] for c in _clues(claims, rows, contrast_ids={"ev-ds"})]
    assert any("海外开发者" in t for t in texts), "海外 09-18 起放行，线索块要收得进来"
    assert any("微博用户" in t for t in texts)
    assert not any("DeepSeek" in t for t in texts), "对照实体那一支⛔ 一字未动"
    # 另外三条筛法各自仍然拦得住（判域与地域无关，这里逐条复核）。
    assert _clues([{"verdict": "PASS", "firsthand": ["ev-hn"], "evidence_ids": ["ev-hn"],
                   "text": "多源互证的说法。"}], rows, contrast_ids=set()) == []
    assert _clues([{"verdict": "SINGLE", "firsthand": [], "evidence_ids": ["ev-hn"],
                   "text": "转述来的说法。"}], rows, contrast_ids=set()) == []
    assert _clues([{"verdict": "SINGLE", "firsthand": ["ev-hn"], "evidence_ids": ["ev-hn"],
                   "text": "与豆包无直接关联，仅登记为长尾噪声。"}],
                  rows, contrast_ids=set()) == []


# ---------------------------------------------------------------- 货 3 规则文档

def test_货3_三份规则互相指得回去_且两支各说各的待遇() -> None:
    """§D-083 的教训：规则与闸各说各话，写手按规则写、闸按自己那套判，两边都「对」
    而稿子被打回。⇒ 共用规则要点到模板，模板要指回共用规则。
    """
    from app.report.polish.skills import get_template, shared_rules

    rules = shared_rules()
    assert "5.7.1" in rules and "5.7.2" in rules, "两支必须各有一节，⛔ 不许再合称旁证"
    # 5.7.1 对照实体：照旧不进关键发现。
    head, tail = rules.split("5.7.2", 1)
    contrast = head.split("5.7.1", 1)[1]
    assert "旁证·对照实体" in contrast and "不进" in contrast
    # 5.7.2 海外：放行 + 必须标明 + 给得出照抄的写法 + 全海外的加码。
    assert "海外社区" in tail and "放行" in tail
    assert "Hacker News" in tail, "⛔ 别只说「要标明」，要给写手可照抄的写法"
    assert "只反映海外社区" in tail, "整条全海外时要写死适用范围"
    assert "国内总体倾向" in tail or "合并统计" in tail
    # 模板指得回共用规则，共用规则也点得到模板。
    assert "consulting/SKILL.md" in tail and "sentiment-brief/SKILL.md" in tail
    for name, anchor in (("consulting", "5.7.2"), ("sentiment-brief", "5.7.2")):
        body = get_template(name).body
        assert anchor in body, f"{name} 要指回共用规则 §5.7.2"
        assert "海外社区" in body
    # ⛔ 竞品对比稿本来就豁免这一条，不许凭空给它加规矩。
    assert "5.7" not in get_template("competitor-matrix").body


# ---------------------------------------------------------------- 货 4 硬判据 ㉑

_TABLES = {"sources": [
    {"mark": "S01", "platform": "hacker_news", "overseas": True},
    {"mark": "S02", "platform": "reddit", "overseas": True},
    {"mark": "S03", "platform": "x", "overseas": True},
    {"mark": "S04", "platform": "xhs", "overseas": False},
]}


def _judge(tmp_path: Path, body: str, tables: dict | None = None) -> list[str]:
    ruler = _ruler()
    md = tmp_path / "r.polished.consulting.md"
    md.write_text(body, encoding="utf-8")
    path = md.with_name("r.polished.consulting.tables.json")
    path.write_text(json.dumps(tables or _TABLES, ensure_ascii=False), encoding="utf-8")
    data = json.loads(path.read_text(encoding="utf-8"))
    template = ruler._template_for(body, path)
    return ruler.check_overseas_attributed(body, template, data["sources"])


def _doc(finding: str, advice: str = "") -> str:
    return ("# 执行摘要\n\n开场[S04]。\n\n"
            f"{finding}\n\n> 本报告结论的把握度为**低**。\n\n"
            "# 关键发现\n\n## 一\n\n正文[S04]。\n\n"
            "# 论据与数据\n\n无。\n\n"
            + (f"# 建议\n\n{advice}\n\n" if advice else "")
            + "# 附录\n\n无。\n")


def test_货4_掺了海外没标出身判红_标了就放行(tmp_path: Path) -> None:
    red = _judge(tmp_path, _doc("1. 【A】有用户抱怨素材里遗留水印[S01][S04]"))
    assert len(red) == 1 and "没标明出身" in red[0] and "S01" in red[0]
    # 认法一：点名平台。
    assert _judge(tmp_path, _doc("1. 【A】Hacker News 上有人抱怨遗留水印[S01][S04]")) == []
    # 认法二：地域说法。两条认法是「或」，⛔ 不是「且」。
    assert _judge(tmp_path, _doc("1. 【A】海外社区有人抱怨遗留水印[S01][S04]")) == []
    assert _judge(tmp_path, _doc("1. 【A】境外开发者抱怨遗留水印[S01][S04]")) == []


def test_货4_X平台光点名不算标明_必须给地域说法(tmp_path: Path) -> None:
    """⛔ 有意为之：X 上大量是中文帖（真稿池里 S23/S28 都是中文），写「X 上有用户说」
    读者看不出这是海外；而且「X」是单字母，拿它做词面匹配会把「X 轴」认成命中。
    """
    from app.report.polish.tables import OVERSEAS_PLATFORMS

    ruler = _ruler()
    assert ruler.OVERSEAS_PLATFORM_WORDS["x"] == ()
    assert set(ruler.OVERSEAS_PLATFORM_WORDS) == set(OVERSEAS_PLATFORMS), \
        "平台名表要与 OVERSEAS_PLATFORMS 一一对上，加源时漏一个就是静默放行"
    assert len(_judge(tmp_path, _doc("1. 【A】X 上有用户说豆包好用[S03][S04]"))) == 1
    assert _judge(tmp_path, _doc("1. 【A】海外的 X 上有用户说豆包好用[S03][S04]")) == []


def test_货4_护栏_整条全海外还要写死适用范围(tmp_path: Path) -> None:
    """09-14 事故的防线：一条关键发现全靠 Reddit 撑着，读者会以为那是国内的声音。"""
    only = _judge(tmp_path, _doc("1. 【A】Reddit 上的开发者抱怨遗留水印[S02][S01]"))
    assert len(only) == 1 and "全部" in only[0] and "只反映海外社区" in only[0], only
    assert _judge(tmp_path, _doc(
        "1. 【A】Reddit 上的开发者抱怨遗留水印，这一条只反映海外社区[S02][S01]")) == []
    assert _judge(tmp_path, _doc(
        "1. 【A】Reddit 开发者抱怨遗留水印，国内语料没有对应说法[S02][S01]")) == []
    # 掺了一条国内的就不加码——那一条读者已经有国内参照了。
    assert _judge(tmp_path, _doc("1. 【A】Reddit 上的开发者抱怨遗留水印[S02][S04]")) == []


def test_货4_点名了别的平台不算标明_平台名要对得上引的那条(tmp_path: Path) -> None:
    """⚠️ 认法二认的是**它引的那条证据所在的平台**，⛔ 不是「文里出现任一海外平台名」。

    写「Reddit 上……」却引的是 Hacker News 的角标，读者按平台名去找原文会找错地方，
    这跟没标一样。踩点：本包写用例时自己先写错了一次，闸判红判得对。
    """
    red = _judge(tmp_path, _doc("1. 【A】Reddit 上的开发者抱怨遗留水印[S01][S04]"))
    assert len(red) == 1 and "hacker_news" in red[0], red
    # 换成它真正的平台名就过。
    assert _judge(tmp_path, _doc("1. 【A】Hacker News 上的开发者抱怨遗留水印[S01][S04]")) == []
    # 说不清平台时用地域说法兜底，同样算标明。
    assert _judge(tmp_path, _doc("1. 【A】海外开发者抱怨遗留水印[S01][S04]")) == []


def test_货4_判域含建议节的降级区_否则整道闸量不到东西(tmp_path: Path) -> None:
    """⚠️ 实测：第三轮真稿里**唯一**一条海外引用就落在降级区「值得进一步验证的方向」。
    按建议门禁那个判域（降级区免检）整道闸一条都量不到 ⇒ 那是假绿（坑 23′）。
    标明出身与证据强不强无关，所以这里**故意**比门禁多管一段。
    """
    body = _doc("1. 【A】国内用户夸陪伴[S04]",
                "1. **建议一**\n   依据：国内样本[S04]。\n\n"
                "### 值得进一步验证的方向\n\n"
                "1. **验证办公定位**\n   依据：标题信号[S01][S04]。\n")
    red = _judge(tmp_path, body)
    assert len(red) == 1 and "值得进一步验证" not in red[0]
    assert "第 2 条" in red[0] and "S01" in red[0], red
    # 生产侧那道门禁的判域**没被改动**：降级区照旧免检。
    from app.report.polish.run import advice_entries

    lines = ["1. **建议一**", "   依据：国内样本[S04]。", "",
             "### 值得进一步验证的方向", "", "1. **验证办公定位**", "   依据：标题信号[S01]。"]
    assert len(advice_entries(lines)) == 1
    assert len(advice_entries(lines, include_downgraded=True)) == 2


def test_货4_老产物也量得出海外_按字面拆两支(tmp_path: Path) -> None:
    """⛔ 不许因为老稿没有 `overseas` 键就静默放行（假绿的经典形状）。

    ⚠️ 也不许按 `offtopic` 的**真值**一把抓：那会把老稿里的对照实体也当成海外，
    对着 DeepSeek 的角标要求写「这是海外社区的声音」（假红）。
    """
    ruler = _ruler()
    old = {"sources": [{"mark": "S01", "platform": "hacker_news", "offtopic": "海外平台"},
                       {"mark": "S05", "platform": "xhs", "offtopic": "对照实体"},
                       {"mark": "S04", "platform": "xhs"}]}
    assert ruler._overseas_marks(old["sources"]) == {1: "hacker_news"}
    red = _judge(tmp_path, _doc("1. 【A】有人抱怨遗留水印[S01][S04]"), old)
    assert len(red) == 1 and "S01" in red[0]
    # 对照实体的角标 ⛔ 不进 ㉑ 的判域。
    assert _judge(tmp_path, _doc("1. 【A】DeepSeek 联网更稳[S05][S04]"), old) == []


def test_货4_软检丙只按对照实体算占比_海外不再判跑题(tmp_path: Path) -> None:
    """⚠️ 锁的正是被替换的旧语义：改前 ⒞ 把海外也算进「跑题」占比。"""
    ruler = _ruler()
    md = tmp_path / "r.polished.consulting.md"
    md.write_text(_doc("1. 【A】Reddit 开发者抱怨水印，这一条只反映海外社区[S01][S02]\n"
                       "2. 【B】DeepSeek 联网更稳[S05][S06]"), encoding="utf-8")
    tables = md.with_name("r.polished.consulting.tables.json")
    tables.write_text(json.dumps({"sources": [
        {"mark": "S01", "platform": "hacker_news", "overseas": True},
        {"mark": "S02", "platform": "reddit", "overseas": True},
        {"mark": "S05", "platform": "xhs", "offtopic": "对照实体"},
        {"mark": "S06", "platform": "xhs", "offtopic": "对照实体"}]},
        ensure_ascii=False), encoding="utf-8")
    warns = ruler.warnings_of(md, tables)["⒞ 摘要关键发现跑题"]
    assert len(warns) == 1 and "第 2 条" in warns[0] and "对照实体" in warns[0]
    assert "第 1 条" not in " ".join(warns), "海外 09-18 起放行，⛔ 不再按跑题判黄"


def test_货4_是硬判据不是软检() -> None:
    ruler = _ruler()
    assert "㉑ 引了海外就得标明" in ruler.CHECKS
    assert not any("海外" in name for name in ruler.WARNINGS)
