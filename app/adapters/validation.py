"""Owli 产物校验器：封闭注册表、三态结论与一次性失败汇总。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNS_ROOT = PROJECT_ROOT / "runs"


class Verdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Result:
    verdict: Verdict
    name: str
    message: str
    offenders: list[str]
    detail: dict[str, Any] | None = None


@dataclass
class Ctx:
    output_path: Path
    output_format: str
    research_id: str
    goal_id: str
    agent_id: str
    read_text: Callable[[], str]
    read_json: Callable[[], Any]
    store: Any
    source_domains: frozenset[str]


@dataclass(frozen=True)
class ValidationReport:
    verdict: Verdict
    results: list[Result]

    @property
    def failures(self) -> list[Result]:
        return [result for result in self.results if result.verdict is Verdict.FAIL]

    @property
    def unavailable(self) -> list[Result]:
        return [
            result for result in self.results
            if result.verdict is Verdict.UNAVAILABLE
        ]


Validator = Callable[[Ctx, list[str]], Result]
REGISTRY: dict[str, Validator] = {}


def validator(name: str):
    def deco(function: Validator) -> Validator:
        assert name not in REGISTRY, f"校验器重名：{name}"
        REGISTRY[name] = function
        return function

    return deco


def _result(
    verdict: Verdict,
    name: str,
    message: str,
    offenders: list[str] | None = None,
    detail: dict[str, Any] | None = None,
) -> Result:
    values = list(offenders or [])
    if len(values) > 10:
        hidden = len(values) - 9
        values = values[:9] + [f"... 另 {hidden} 条"]
    return Result(verdict, name, message, values, detail)


def _parse_spec(specification: str) -> tuple[str, list[str]]:
    name, separator, raw_arguments = specification.partition(":")
    if not re.fullmatch(r"[a-z0-9_]+", name):
        return name, []
    if not separator:
        return name, []
    return name, raw_arguments.split(",")


def _invoke(ctx: Ctx, specification: str) -> Result:
    name, arguments = _parse_spec(specification)
    function = REGISTRY.get(name)
    if function is None:
        return _result(
            Verdict.UNAVAILABLE,
            specification,
            f"校验器 {name or specification} 不在封闭注册表中",
        )
    try:
        result = function(ctx, arguments)
    except Exception as exc:  # 校验器自身异常不得冒充 agent 失败。
        return _result(
            Verdict.UNAVAILABLE,
            specification,
            f"校验器 {name} 运行失败：{type(exc).__name__}: {exc}",
            detail={"exception": type(exc).__name__},
        )
    if result.name != specification:
        return Result(
            result.verdict,
            specification,
            result.message,
            result.offenders,
            result.detail,
        )
    return result


def validate(ctx: Ctx, specifications: list[str]) -> ValidationReport:
    """执行校验；文件前置失败时短路，其余条件全部跑完。"""
    explicit_file = next(
        (item for item in specifications if _parse_spec(item)[0] == "file_exists"),
        "file_exists",
    )
    file_result = _invoke(ctx, explicit_file)
    if file_result.verdict is not Verdict.PASS:
        return ValidationReport(file_result.verdict, [file_result])

    results = [file_result]
    for specification in specifications:
        if _parse_spec(specification)[0] != "file_exists":
            results.append(_invoke(ctx, specification))

    if any(result.verdict is Verdict.UNAVAILABLE for result in results):
        verdict = Verdict.UNAVAILABLE
    elif any(result.verdict is Verdict.FAIL for result in results):
        verdict = Verdict.FAIL
    else:
        verdict = Verdict.PASS
    return ValidationReport(verdict, results)


run_validators = validate


def _resolved_output_path(path: Path) -> Path:
    if path.is_absolute():
        return path.resolve(strict=False)
    return (PROJECT_ROOT / path).resolve(strict=False)


@validator("file_exists")
def file_exists(ctx: Ctx, arguments: list[str]) -> Result:
    if arguments:
        return _result(
            Verdict.UNAVAILABLE, "file_exists", "file_exists 不接受参数"
        )
    actual = _resolved_output_path(ctx.output_path)
    expected_root = (
        RUNS_ROOT / ctx.research_id / "goals" / ctx.goal_id
    ).resolve(strict=False)
    try:
        actual.relative_to(expected_root)
    except ValueError:
        return _result(
            Verdict.FAIL,
            "file_exists",
            f"产物路径越界：{actual}",
            [str(actual)],
            {"expected_root": str(expected_root), "actual_path": str(actual)},
        )
    if not actual.is_file():
        return _result(
            Verdict.FAIL,
            "file_exists",
            f"产物文件不存在：{actual}",
            [str(actual)],
            {"exists": False},
        )
    try:
        size = actual.stat().st_size
    except OSError as exc:
        return _result(
            Verdict.UNAVAILABLE,
            "file_exists",
            f"无法读取产物文件状态：{type(exc).__name__}: {exc}",
        )
    if size == 0:
        return _result(
            Verdict.FAIL,
            "file_exists",
            f"产物是空文件：{actual}",
            [str(actual)],
            {"size": 0},
        )
    return _result(
        Verdict.PASS,
        "file_exists",
        f"产物文件存在且非空：{actual}",
        detail={"size": size},
    )


def _read_json_array(ctx: Ctx, name: str) -> tuple[list[Any] | None, Result | None]:
    try:
        value = ctx.read_json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError) as exc:
        return None, _result(
            Verdict.FAIL,
            name,
            f"产物不是合法 JSON：{type(exc).__name__}: {exc}",
        )
    except OSError as exc:
        return None, _result(
            Verdict.UNAVAILABLE,
            name,
            f"无法读取 JSON 产物：{type(exc).__name__}: {exc}",
        )
    if not isinstance(value, list):
        return None, _result(
            Verdict.FAIL,
            name,
            f"JSON 顶层必须是数组，实际是 {type(value).__name__}",
        )
    return value, None


@validator("json_array_min_items")
def json_array_min_items(ctx: Ctx, arguments: list[str]) -> Result:
    name = "json_array_min_items"
    if len(arguments) != 1:
        return _result(Verdict.UNAVAILABLE, name, f"{name} 必须有一个整数参数")
    try:
        minimum = int(arguments[0])
    except ValueError:
        return _result(Verdict.UNAVAILABLE, name, f"非法整数参数：{arguments[0]}")
    items, error = _read_json_array(ctx, name)
    if error:
        return error
    actual = len(items)
    if actual < minimum:
        return _result(
            Verdict.FAIL,
            name,
            f"JSON 数组条目不足：期望至少 {minimum}，实际 {actual}",
            detail={"expected_minimum": minimum, "actual": actual},
        )
    return _result(
        Verdict.PASS,
        name,
        f"JSON 数组条目数合格：期望至少 {minimum}，实际 {actual}",
        detail={"expected_minimum": minimum, "actual": actual},
    )


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _value_at_path(item: Any, path: str) -> tuple[bool, Any]:
    current = item
    for key in path.split("."):
        if not isinstance(current, dict) or key not in current:
            return False, None
        current = current[key]
    return True, current


@validator("each_item_has")
def each_item_has(ctx: Ctx, arguments: list[str]) -> Result:
    name = "each_item_has"
    if not arguments or any(not argument for argument in arguments):
        return _result(Verdict.UNAVAILABLE, name, f"{name} 至少需要一个键路径")
    items, error = _read_json_array(ctx, name)
    if error:
        return error
    offenders: list[str] = []
    detail: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        missing = []
        for path in arguments:
            exists, value = _value_at_path(item, path)
            if not exists or _is_empty(value):
                missing.append(path)
        if missing:
            offenders.append(f"下标 {index}：缺少或为空 {','.join(missing)}")
            detail.append({"index": index, "keys": missing})
    if offenders:
        return _result(
            Verdict.FAIL,
            name,
            f"{len(offenders)} 个元素存在缺失或空字段",
            offenders,
            {"items": detail},
        )
    return _result(Verdict.PASS, name, f"全部 {len(items)} 个元素字段齐全且非空")


_HEADING = re.compile(r"(?m)^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")


def _markdown_sections(text: str) -> list[tuple[str, str, int, int]]:
    matches = list(_HEADING.finditer(text))
    sections = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections.append((match.group(2).strip(), text[match.end():end], match.start(), end))
    return sections


@validator("sections_exist")
def sections_exist(ctx: Ctx, arguments: list[str]) -> Result:
    name = "sections_exist"
    if not arguments or any(not argument for argument in arguments):
        return _result(Verdict.UNAVAILABLE, name, f"{name} 至少需要一个章节名")
    try:
        sections = _markdown_sections(ctx.read_text())
    except (OSError, UnicodeDecodeError) as exc:
        return _result(
            Verdict.UNAVAILABLE, name, f"无法读取 Markdown：{type(exc).__name__}: {exc}"
        )
    by_title = {title: body for title, body, _, _ in sections}
    missing = [title for title in arguments if not by_title.get(title, "").strip()]
    actual = [title for title, _, _, _ in sections]
    if missing:
        return _result(
            Verdict.FAIL,
            name,
            f"章节缺失或正文为空：{','.join(missing)}",
            missing,
            {"actual_headings": actual},
        )
    return _result(
        Verdict.PASS,
        name,
        f"要求的 {len(arguments)} 个章节均存在且正文非空",
        detail={"actual_headings": actual},
    )


@validator("section_exists")
def section_exists(ctx: Ctx, arguments: list[str]) -> Result:
    result = sections_exist(ctx, arguments)
    detail = dict(result.detail or {})
    detail["alias_of"] = "sections_exist"
    return Result(result.verdict, "section_exists", result.message, result.offenders, detail)


_CITATION = re.compile(r"\[S(?:0[1-9]|[1-9][0-9])\]")


def _citation_sets(ctx: Ctx, name: str) -> tuple[set[str], set[str], Result | None]:
    try:
        text = ctx.read_text()
    except (OSError, UnicodeDecodeError) as exc:
        return set(), set(), _result(
            Verdict.UNAVAILABLE, name, f"无法读取 Markdown：{type(exc).__name__}: {exc}"
        )
    source_spans = [
        (start, end, body)
        for title, body, start, end in _markdown_sections(text)
        if "信息源清单" in title
    ]
    if source_spans:
        source_text = "\n".join(body for _, _, body in source_spans)
        body_parts = []
        cursor = 0
        for start, end, _ in source_spans:
            body_parts.append(text[cursor:start])
            cursor = end
        body_parts.append(text[cursor:])
        body_text = "".join(body_parts)
    else:
        definition_lines = []
        body_lines = []
        for line in text.splitlines():
            if _CITATION.search(line) and re.search(r"https?://", line):
                definition_lines.append(line)
            else:
                body_lines.append(line)
        source_text = "\n".join(definition_lines)
        body_text = "\n".join(body_lines)
    return set(_CITATION.findall(body_text)), set(_CITATION.findall(source_text)), None


@validator("citation_marks_resolvable")
def citation_marks_resolvable(ctx: Ctx, arguments: list[str]) -> Result:
    name = "citation_marks_resolvable"
    if arguments:
        return _result(Verdict.UNAVAILABLE, name, f"{name} 不接受参数")
    body, sources, error = _citation_sets(ctx, name)
    if error:
        return error
    unresolved = sorted(body - sources)
    if unresolved:
        return _result(
            Verdict.FAIL,
            name,
            f"{len(unresolved)} 个正文角标无法在信息源清单中解析",
            unresolved,
            {"body_marks": sorted(body), "source_marks": sorted(sources)},
        )
    return _result(Verdict.PASS, name, f"正文 {len(body)} 个角标均可解析")


@validator("no_orphan_citation")
def no_orphan_citation(ctx: Ctx, arguments: list[str]) -> Result:
    name = "no_orphan_citation"
    if arguments:
        return _result(Verdict.UNAVAILABLE, name, f"{name} 不接受参数")
    body, sources, error = _citation_sets(ctx, name)
    if error:
        return error
    orphaned = sorted(sources - body)
    if orphaned:
        return _result(
            Verdict.FAIL,
            name,
            f"信息源清单中有 {len(orphaned)} 个孤立角标",
            orphaned,
            {"body_marks": sorted(body), "source_marks": sorted(sources)},
        )
    return _result(Verdict.PASS, name, f"信息源清单 {len(sources)} 个条目均被正文引用")


@validator("db_row_exists")
def db_row_exists(ctx: Ctx, arguments: list[str]) -> Result:
    name = "db_row_exists"
    if len(arguments) != 1 or not arguments[0]:
        return _result(Verdict.UNAVAILABLE, name, f"{name} 必须有一个表或列路径参数")
    path = arguments[0]
    reader = getattr(ctx.store, "read_validation_path", None)
    if reader is None:
        return _result(
            Verdict.UNAVAILABLE,
            name,
            "沉淀层未提供固定读接口 read_validation_path",
            [path],
        )
    try:
        value = reader(path, ctx.research_id)
    except Exception as exc:
        return _result(
            Verdict.UNAVAILABLE,
            name,
            f"读取数据库失败：{type(exc).__name__}: {exc}",
            [path],
            {"exception": type(exc).__name__},
        )
    if _is_empty(value):
        return _result(
            Verdict.FAIL,
            name,
            f"数据库路径 {path} 不存在或值为空",
            [path],
            {"value": value},
        )
    return _result(
        Verdict.PASS,
        name,
        f"数据库路径 {path} 存在且值非空",
        detail={"value": value},
    )


_UNIMPLEMENTED = (
    "zip_entry_glob_exists",
    "openpyxl_reload_ok",
    "json_array_between",
    "field_domain_whitelist",
    "list_items_min",
    "each_insight_has_citation",
    "table_rows_min",
    "table_rows_between",
    "table_no_empty_cells",
    "each_row_urls_reachable",
    "db_field_non_empty",
    "claims_backfilled",
    "no_item_missing_rating",
    "rating_notes_matches_regex",
    "rating_notes_scores_match_columns",
    "no_baseline_prefix_left",
    "norm_method_in_enum",
    "norm_context_required_keys",
    "xlsx_sheets_exact",
)


def _unimplemented(name: str) -> Validator:
    def stub(ctx: Ctx, arguments: list[str]) -> Result:
        del ctx, arguments
        return _result(
            Verdict.UNAVAILABLE,
            name,
            f"校验器 {name} 尚未实现（登记于 validator-registry.md §2）",
        )

    return stub


for _name in _UNIMPLEMENTED:
    validator(_name)(_unimplemented(_name))


assert len(REGISTRY) == 27, f"校验器注册表应为 27 项，实际 {len(REGISTRY)} 项"
