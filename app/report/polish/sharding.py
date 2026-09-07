"""正式稿的「一条发现一片」分片：片数从已写成的执行摘要里读，不写死。

**为什么要切**：撰写已经拆到「节」，没拆到「片」；「关键发现」实测单节 2–22 分钟
（`run.py` 的 `SECTION_TIMEOUT_SECONDS` 注释），而本机代理把单次流式响应掐在 5–6 分钟
——09-07 那一格两轮独立复现（4 分 59 秒 / 5 分 47 秒），墙钟从 900 放宽到 1800 照样死。
问题不是时间不够，是**单次长流本身撑不住**，所以往下切。

**为什么不复用工作稿那套**（`orchestrator/sectioning.py` 的 `write_shard_sizes` 等，
且那是本包禁区）：它切的是**证据池条目列表**，前提是「入参是一串可并列的条目」。
正式稿各片**入参完全相同**（同一份 63.7 KB 提示词），变的是**写哪一段输出**。
前提不成立，硬套等于误用。片命名抄它的形态（`02-关键发现.shard-1.md`）但不 import。

**为什么合并就是拼接**：正式稿的信息源清单由代码确定性生成（`run.sources_table`），
片正文是纯散文 + `## 小标题`，没有工作稿那套信封（信息源去重 / 结论行归并 / 缺失清单），
所以不用 `markdown.merge_section_shards`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

#: 摘要里的发现行：行首编号 + 正文。SKILL 硬性规定「3–5 条关键发现，每条一行，
#: 格式固定 `1. 【A】结论句[S12][S18]`」。只认顶格的编号行——续行与引用块不算。
_FINDING_LINE = re.compile(r"^(\d+)\.[ \t]+(\S.*)$", re.MULTILINE)
_MARK = re.compile(r"\[S(\d{2,})\]")
#: 少于这么多条就不切：一条发现单独成节本来就不长，切了徒增合并面。
#: 也是解析失灵时的兜底——读不出编号列表就退回整节写一次（老行为）。
MIN_FINDINGS_TO_SHARD = 2
#: 多于这么多条不再加片：SKILL 上限是 5 条，留一点余量给写手多写一条的情况。
#: 超出的条数并进最后一片，**不丢**——丢一条发现是内容错误，比片大一点坏得多。
MAX_SHARDS = 6


@dataclass(frozen=True)
class Finding:
    """摘要里的一条关键发现。`index` 从 1 起，与片号一致。"""

    index: int
    text: str                       # 编号后的原话，原样带角标
    marks: tuple[str, ...]          # 这条发现自带的角标，升序去重

    @property
    def title_line(self) -> str:
        return f"{self.index}. {self.text}"


def parse_findings(summary_markdown: str) -> list[Finding]:
    """从写成的执行摘要里抽出关键发现编号列表。抽不出就返回空表，由调用方兜底。

    只在摘要这一节里找，所以不会撞上「建议」节那份同形状的编号列表。
    编号不连续（写手漏号）也照收，按出现次序重排——片号必须连续，否则片路径打架。
    """
    findings = []
    for order, match in enumerate(_FINDING_LINE.finditer(summary_markdown or ""), 1):
        text = match.group(2).strip()
        marks = tuple(f"S{int(n):02d}" for n in sorted({int(m) for m in _MARK.findall(text)}))
        findings.append(Finding(index=order, text=text, marks=marks))
    if len(findings) <= MAX_SHARDS:
        return findings
    # 超出上限的并进最后一条，不丢：丢一条发现是内容错误。
    tail = findings[MAX_SHARDS - 1:]
    merged = Finding(index=MAX_SHARDS,
                     text="；".join(f.text for f in tail),
                     marks=tuple(sorted({m for f in tail for m in f.marks})))
    return findings[:MAX_SHARDS - 1] + [merged]


def should_shard(findings: Sequence[Finding]) -> bool:
    """够不够条数值得切。不够就退回「整节写一次」——老行为，不是新失败。"""
    return len(findings) >= MIN_FINDINGS_TO_SHARD


def shard_path(section_path: Path, index: int) -> Path:
    """`02-关键发现.md` → `02-关键发现.shard-1.md`。

    抄工作稿 `write_shard_path` 的形态，不 import（它在禁区文件里）。
    与分节文件同一个目录，所以 `clear_stale_parts` 的 `[0-9][0-9]-*.md` 一并清得掉。
    """
    return section_path.with_name(f"{section_path.stem}.shard-{index}{section_path.suffix}")


def shard_paths(section_path: Path, count: int) -> list[Path]:
    return [shard_path(section_path, i) for i in range(1, count + 1)]


def merge_shards(paths: Sequence[Path]) -> str:
    """按片序拼接片正文。片间一个空行，与写手在整节里写出来的形状一致。

    真稿验过逐字节往返：把 09-06 那份 8 432 B 的「关键发现」按二级标题切成 4 份再合，
    与原文一模一样（`tests/test_shard1_polish_shards.py`）。
    """
    bodies = [path.read_text(encoding="utf-8").strip() for path in paths]
    return "\n\n".join(body for body in bodies if body)
