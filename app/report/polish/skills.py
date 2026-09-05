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
_LIST_FIELDS = ("tables", "sections")
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
    return Template(
        name=name, title=str(meta.get("title") or name),
        description=str(meta.get("description") or ""), tables=tables, sections=sections,
        model=str(meta.get("model") or "opus"), body=body,
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
