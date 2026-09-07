"""正式稿模板加载器：扫 `app/skills/report-polish/` 出模板清单与提示词。

**模板内容与代码零耦合**——代码只认 frontmatter 里的六个字段（`name` / `title` /
`description` / `tables` / `sections` / `model`），正文一个字都不解析，直接拼进提示词。
加模板 = 新建一个目录放一份 SKILL.md，不改任何代码。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.report.polish.tables import TABLE_NAMES

SKILLS_ROOT = Path(__file__).resolve().parents[2] / "skills" / "report-polish"
SHARED_DIR = "_shared"
#: `_shared/` 里按这个顺序拼进提示词；缺文件即报错，不静默跳过。
SHARED_FILES: tuple[str, ...] = ("writing-rules.md", "evidence-grades.md", "audit-checklist.md")
DEFAULT_TEMPLATE = "consulting"
_LIST_FIELDS = ("tables", "sections", "shard_sections")
_TEXT_FIELDS = ("name", "title", "description", "model")


@dataclass(frozen=True)
class Template:
    """一个正式稿模板。`body` 是 SKILL.md 去掉 frontmatter 的正文。"""

    name: str
    title: str
    description: str
    tables: tuple[str, ...]
    sections: tuple[str, ...]
    model: str
    body: str
    #: 声明哪几节要按摘要条数切片（§SHARD-1）。空 = 整节写一次，老行为。
    #: 大纲永远是第一节（三个模板的第一节都是摘要/总体倾向），所以它自己不许切。
    shard_sections: tuple[str, ...] = ()

    def as_listing(self) -> dict[str, object]:
        """给 `GET /api/report-templates` 用：前端下拉只需要这四个字段。"""
        return {"name": self.name, "title": self.title, "description": self.description,
                "sections": list(self.sections)}


def _split_frontmatter(text: str, source: Path) -> tuple[dict[str, object], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"{source} 缺 frontmatter：首行必须是 ---")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError(f"{source} 的 frontmatter 没有闭合的 ---") from exc
    meta: dict[str, object] = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, _, raw = line.partition(":")
        key, raw = key.strip(), raw.strip()
        if key in _LIST_FIELDS:
            meta[key] = [v.strip() for v in raw.strip("[]").split(",") if v.strip()]
        elif key in _TEXT_FIELDS:
            meta[key] = raw.strip("\"'")
    return meta, "\n".join(lines[end + 1:]).strip()


def _load_one(skill_file: Path) -> Template:
    meta, body = _split_frontmatter(skill_file.read_text(encoding="utf-8"), skill_file)
    name = str(meta.get("name") or "").strip()
    if name != skill_file.parent.name:
        raise ValueError(f"{skill_file} 的 name={name!r} 与目录名 {skill_file.parent.name!r} 不一致")
    tables = tuple(str(t) for t in (meta.get("tables") or []))
    unknown = [t for t in tables if t not in TABLE_NAMES]
    if unknown:
        raise ValueError(f"{skill_file} 声明了不存在的表 {unknown}；可用表见 tables.TABLE_NAMES")
    sections = tuple(str(s) for s in (meta.get("sections") or []))
    if not sections:
        raise ValueError(f"{skill_file} 缺 sections：尺子要靠它核成稿的一级标题")
    shard_sections = tuple(str(s) for s in (meta.get("shard_sections") or []))
    stray = [s for s in shard_sections if s not in sections]
    if stray:
        raise ValueError(f"{skill_file} 的 shard_sections 声明了不在 sections 里的 {stray}")
    if sections[0] in shard_sections:
        # 片数是从第一节（大纲节）读出来的，它必须先整节写成，切了就没有大纲可读。
        raise ValueError(f"{skill_file} 不能切第一节 {sections[0]!r}：片数要从它里面读")
    return Template(
        name=name, title=str(meta.get("title") or name),
        description=str(meta.get("description") or ""), tables=tables, sections=sections,
        model=str(meta.get("model") or "opus"), body=body, shard_sections=shard_sections,
    )


@lru_cache(maxsize=1)
def load_templates(root: str | None = None) -> tuple[Template, ...]:
    """扫目录出全部模板，按 `DEFAULT_TEMPLATE` 在前、其余按名排。目录坏了就抛，不静默降级。"""
    base = Path(root) if root else SKILLS_ROOT
    found = [_load_one(path) for path in sorted(base.glob("*/SKILL.md"))
             if path.parent.name != SHARED_DIR]
    if not found:
        raise FileNotFoundError(f"{base} 下没有任何 SKILL.md")
    return tuple(sorted(found, key=lambda t: (t.name != DEFAULT_TEMPLATE, t.name)))


def get_template(name: str | None, root: str | None = None) -> Template:
    """按名取模板；`None` 或空串取默认模板。名字不认识就抛 KeyError（接口层转 400）。"""
    templates = load_templates(root)
    wanted = (name or DEFAULT_TEMPLATE).strip()
    for template in templates:
        if template.name == wanted:
            return template
    raise KeyError(f"没有名为 {wanted!r} 的正式稿模板；现有：{[t.name for t in templates]}")


def shared_rules(root: str | None = None) -> str:
    """三个模板共用的硬规则正文，按 `SHARED_FILES` 顺序拼。"""
    base = (Path(root) if root else SKILLS_ROOT) / SHARED_DIR
    chunks = []
    for file_name in SHARED_FILES:
        path = base / file_name
        if not path.is_file():
            raise FileNotFoundError(f"共用规则缺文件：{path}")
        chunks.append(path.read_text(encoding="utf-8").strip())
    return "\n\n---\n\n".join(chunks)


#: 题面里出现这些词就是在问「大家怎么看」——该走舆情简报，不是咨询体。
#: §RPT-2 货 1：09-05 那份「国内大家对豆包的看法」走了默认咨询体，六张表里没有
#: 一张回答「谁在夸什么、骂什么」，读者拿不到答案（提货单 §2.2 第 3 条）。
SENTIMENT_CUES: tuple[str, ...] = ("看法", "口碑", "怎么说", "怎么看", "评价", "吐槽",
                                   "风评", "反馈", "舆情", "槽点")
#: 对比类线索。只有线索还不够——得真有两个及以上研究对象才配得上矩阵稿。
COMPARE_CUES: tuple[str, ...] = ("对比", "竞品", "vs", "VS", "哪个好", "比较")


def recommend_template(question: str, *, entity_count: int = 0,
                       decision_type: str | None = None) -> str:
    """按题面（与研究对象个数）推荐一个模板名，认不出就回默认的咨询体。

    判定顺序就是下面三条的书写顺序，先命中先返回——「豆包和 Kimi 大家怎么评价」
    这种两头都沾的题面归舆情简报，因为读者问的是「怎么评价」，对比只是范围。

    - 题面含看法/口碑/怎么说/评价/吐槽 → `sentiment-brief`
    - 研究对象 ≥2 且题面含对比/竞品/vs → `competitor-matrix`
    - 其余 → `DEFAULT_TEMPLATE`（咨询体）

    `decision_type` 是 q-1 的答案，目前只作旁证不参与判定；留着是为了让调用方
    不必在加规则时改签名。
    """
    text = (question or "").strip()
    if any(cue in text for cue in SENTIMENT_CUES):
        return "sentiment-brief"
    if entity_count >= 2 and any(cue in text for cue in COMPARE_CUES):
        return "competitor-matrix"
    return DEFAULT_TEMPLATE
