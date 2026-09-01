"""预采集池：本机离线采集产物与 Owli 证据行之间的唯一映射处。

§M6-b 货 1（丁形态，decision-log 2026-09-01「丁现在用、丙留产品化」）：
MediaCrawler 这类采集手法**不进任何 Owli 仓**，留在本机脚本里；公开仓这边只认
一件事——**预采集池的目录契约**。池是唯一接口：本机脚本往里写、Owli 从里读，
两边都不必知道对方怎么实现。将来若做丙·私有插件，插件边界就是这份契约，
Owli 核心不返工。

池的形状（默认根目录 `~/.owli/precollect/`）：

    <root>/<platform>/<batch_id>/manifest.json          批次自述
    <root>/<platform>/<batch_id>/**/jsonl/*contents*.jsonl   采集产物（原样）
    <root>/<platform>/<batch_id>/**/jsonl/*comments*.jsonl   评论（本包未开，读取端忽略）
    <root>/<platform>/<batch_id>/snapshots/*.html            正文快照（§M6-d，见下）

MediaCrawler 把 `SAVE_DATA_PATH` 指到批次目录即可，产物落在
`<batch>/<platform>/jsonl/` 下，无需二次搬运（`tools/async_file_writer.py:37-44`）。

**permalink 口径（§M6-d 货 2，2026-09-02 用户拍板）**：固定永久链优先；只在
补充/兜底面收临时签名链，且**必须伴一份正文 HTML 快照落盘**，快照路径写在行内
（相对批次目录），缺快照当场拒收这一行。理由见 `_require_snapshot`——临时链会
在交付之后才过期，「链接可追溯」这条判据验收当天全绿、一周后全断。

`manifest.json` 的 `status` 只有三种值：`ok` / `partial` / `failed`；失败批次带
`failure.reason`，其中 `login_required` / `qrcode_timeout` 是 §M6-c 登录卡要认的
两种——本包只负责让它们留在池里可被读到，不做卡。

**不建新表、不动 schema**：读出来的行就是 `store.upsert_evidence_batch` 吃的证据
字典，`platform` 取自池目录名；去重沿用 evidence 既有唯一键
（report_id + platform + platform_item_id），不另造 key（[[upsert-covers-one-key-only]]）。

**批次保留/清理策略（§M6-c 货 5，已拍口径：定容）**：每平台成功批
（`ok`/`partial`，partial 的行照样能用）保留最近 **5** 批、失败批保留最近 **1** 批
（留 1 是给登录卡当判据输入，全删了「最新批次为何失败」就说不出口）；
清理在**导入时顺手做**（`python -m app.precollect_import`，`--no-prune` 可关），
只删池目录、**不碰库**——已导入的 evidence 行不因批次目录消失而回退。
研究期读池路径（薄源）只读不清，清理永远不落在章墙钟里。
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

__all__ = [
    "LOGIN_REQUIRED_REASON", "MANIFEST_NAME", "PLATFORM_PROFILES", "POOL_ROOT",
    "PlatformProfile", "PoolContractError", "PoolReadResult", "PrecollectBatch",
    "iter_batches", "latest_login_failure", "load_evidence", "pool_root",
    "profile_for", "prune_batches", "to_evidence",
]

#: §M6-c 货 5 定容值（已拍）：成功批留最近 5、失败批留最近 1。
KEEP_OK_BATCHES = 5
KEEP_FAILED_BATCHES = 1

MANIFEST_NAME = "manifest.json"
POOL_ROOT = Path.home() / ".owli" / "precollect"

#: §M6-c 登录卡认的失败原因；判据 = **最新批次** manifest `status=failed` 且
#: `failure.reason` 等于它（老批次的失败残骸不算——那是清理策略的事，不是登录卡的事）。
LOGIN_REQUIRED_REASON = "login_required"

#: 批次状态闭集；池里出现别的值一律当 `failed` 读（宁可报失败也不假绿）。
BATCH_STATUSES = frozenset({"ok", "partial", "failed"})

_WINDOW_DAYS = re.compile(r"^\s*(\d+)\s*d\s*$", re.IGNORECASE)


def pool_root(root: Path | str | None = None) -> Path:
    """池根目录；显式传入优先，便于夹具与多机部署各自指路。"""

    return Path(root).expanduser() if root is not None else POOL_ROOT


@dataclass(frozen=True)
class PrecollectBatch:
    """池里的一个批次目录。"""

    platform: str
    batch_id: str
    directory: Path
    manifest: Mapping[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        value = str(self.manifest.get("status") or "").strip()
        return value if value in BATCH_STATUSES else "failed"

    @property
    def failure_reason(self) -> str | None:
        failure = self.manifest.get("failure")
        if not isinstance(failure, Mapping):
            return None
        reason = str(failure.get("reason") or "").strip()
        return reason or None

    @property
    def keywords(self) -> tuple[str, ...]:
        raw = self.manifest.get("keywords")
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple)):
            return ()
        return tuple(str(item).strip() for item in raw if str(item).strip())

    @property
    def collected_at(self) -> str | None:
        value = str(self.manifest.get("collected_at") or "").strip()
        return value or None

    def content_files(self) -> list[Path]:
        """按文件名认内容行；评论文件本包不读，但也不当成错误。"""

        return sorted(
            path for path in self.directory.rglob("*.jsonl")
            if "contents" in path.name
        )

    def iter_rows(self) -> Iterator[dict[str, Any]]:
        """逐行读；坏行跳过并计数，不让一行脏数据吞掉整批。"""

        for path in self.content_files():
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(row, dict):
                        yield row


def iter_batches(
    platform: str, *, root: Path | str | None = None
) -> list[PrecollectBatch]:
    """列出某平台的批次，新的在前（按 batch_id 倒序，约定为时间前缀）。"""

    base = pool_root(root) / platform
    if not base.is_dir():
        return []
    batches: list[PrecollectBatch] = []
    for directory in sorted(base.iterdir(), reverse=True):
        if not directory.is_dir():
            continue
        manifest: dict[str, Any] = {}
        manifest_path = directory / MANIFEST_NAME
        if manifest_path.is_file():
            try:
                loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                loaded = None
            if isinstance(loaded, dict):
                manifest = loaded
        batches.append(PrecollectBatch(
            platform=platform, batch_id=directory.name,
            directory=directory, manifest=manifest,
        ))
    return batches


def _int(value: Any) -> int | None:
    """MediaCrawler 的计数是字符串（`"1.2万"` 之类也可能出现）。"""

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _iso(timestamp: Any, *, unit: str = "s") -> str | None:
    """unix 时间戳 → UTC ISO8601；拿不到就返回 None，不编时间。"""

    number = _int(timestamp)
    if number is None or number <= 0:
        return None
    seconds = number / 1000 if unit == "ms" else float(number)
    try:
        moment = datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class PlatformProfile:
    """「一条通道 + 平台参数」里的**平台参数**：只写字段名，不写抓取手法。"""

    source_type: str
    item_id_key: str
    content_key: str
    permalink_key: str
    permalink_template: str
    author_key: str
    author_id_key: str
    metric_keys: Mapping[str, str]
    created_key: str
    created_unit: str = "s"
    baseline_tag: str = ""
    #: 五段式 rating_notes 的五个理由，顺序同 scoring.SCORE_FIELDS，各 ≤14 字。
    rating_reasons: tuple[str, ...] = ()
    #: 证据的内容类型（`dao._CONTENT_KINDS` 闭集）。微博是网友短讯，公众号是
    #: 署名长文，两者在交叉维里不是一回事，所以不该共用一个硬编码值。
    content_kind: str = "user_opinion"
    #: 采集分层（`schema.sql` 的 `fetch_method` CHECK）；行内同名字段可覆盖
    #: ——同一平台可能有两条发现路（如公众号的客户端搜一搜 vs 搜狗 curl），
    #: 让本机脚本照实写，比在这里替它猜一个诚实。
    fetch_method: str = "media_crawler"
    #: 供货方（写进 evidence.extra，与事件里的 provider 同名同义）：谁把这批行
    #: 采回来的。微博是 MediaCrawler，公众号是本机自己的发现+抓正文脚本。
    provider: str = "media_crawler"
    #: 固定永久链的正则。非空即启用**「临时链必附正文快照」闸门**（§M6-d 货 2
    #: 口径 2）：permalink 不匹配 = 临时签名链，几小时到一天就过期，那时报告里
    #: 的角标点开是空的——所以临时链只在**同时落盘了正文快照**时才收。
    permanent_permalink_pattern: str = ""
    #: 行内正文快照的相对路径字段名（相对批次目录）。
    snapshot_key: str = ""


#: `schema.sql:82-84` 的 `fetch_method` CHECK 闭集。在池契约这层先挡一道，
#: 是为了让「本机脚本写错采集分层」当场报错、而不是等 upsert 时被 sqlite
#: 的 CHECK 拒收**整批**证据（[[upsert-covers-one-key-only]] 同类教训）。
FETCH_METHODS = frozenset({
    "official_api", "search_index", "third_party_api",
    "media_crawler", "browser_agent", "manual",
})


PLATFORM_PROFILES: dict[str, PlatformProfile] = {
    # 字段名照 MediaCrawler `store/weibo/__init__.py:86-105` 的 save_content_item。
    "weibo": PlatformProfile(
        source_type="post",
        item_id_key="note_id",
        content_key="content",
        permalink_key="note_url",
        permalink_template="https://m.weibo.cn/detail/{item_id}",
        author_key="nickname",
        author_id_key="user_id",
        metric_keys={
            "liked_count": "liked_count",
            "comments_count": "comments_count",
            "shared_count": "shared_count",
        },
        created_key="create_time",
        baseline_tag="baseline:weibo@v1",
        rating_reasons=(
            "平台社区基线", "搜索近期博文", "缺断言血缘簇",
            "正文可取评论未开", "普通用户短讯",
        ),
    ),
    # §M6-d 货 2。字段名由本机发现脚本产出（`owli-precollect/`，不进仓），
    # 不是某个第三方库的产物格式——所以这张表就是这些字段名的唯一定义处。
    # 公众号没有可比的互动量（阅读数/在看数未登录抓不到），`platforms.py` 里
    # 它的 primary_metric 因此是 None：`raw_metrics` 照收原始值，归一化整批
    # 走 `norm_method=none / reason=no_metric_available`，不编分数。
    "wechat_mp": PlatformProfile(
        source_type="article",
        item_id_key="article_id",
        content_key="content",
        permalink_key="url",
        permalink_template="https://mp.weixin.qq.com/s/{item_id}",
        author_key="account_name",
        author_id_key="account_biz",
        metric_keys={"read_count": "read_count", "like_count": "like_count"},
        created_key="publish_time",
        baseline_tag="baseline:wechat_mp@v1",
        rating_reasons=(
            "认证主体待核", "发布时间可取", "缺断言血缘簇",
            "正文全无评论", "账号多为自营",
        ),
        content_kind="industry_view",
        fetch_method="browser_agent",
        provider="owli_precollect",
        # 固定链 = `/s/` 加一段 base64url 串，**不带查询参数**；带
        # `?src=11&timestamp=&signature=` 的是临时签名链（勘察 §一实证）。
        permanent_permalink_pattern=r"^https://mp\.weixin\.qq\.com/s/[A-Za-z0-9_-]+/?$",
        snapshot_key="snapshot_path",
    ),
}


class PoolContractError(ValueError):
    """池里的行不符合契约——宁可当场报错，也不静默丢一半字段。"""


def profile_for(platform: str) -> PlatformProfile:
    try:
        return PLATFORM_PROFILES[platform]
    except KeyError as exc:
        raise PoolContractError(
            f"预采集池未登记平台参数：{platform}；"
            f"已登记 {sorted(PLATFORM_PROFILES)}"
        ) from exc


def to_evidence(
    row: Mapping[str, Any], *, platform: str, batch: PrecollectBatch | None = None,
    fetched_at: str | None = None,
) -> dict[str, Any]:
    """池内一行 → 一条证据字典（`upsert_evidence_batch` 直接可吃）。

    五维分只写基线占位，真值由评级链回填（§RATE-1 之后写作前评级）；
    基线数值不在这里手抄，统一问 `reliability.scoring.PLATFORM_BASELINES`
    ——第七张表的收编见 §M6-b 货 6。
    """

    from app.reliability.scoring import PLATFORM_BASELINES

    profile = profile_for(platform)
    item_id = str(row.get(profile.item_id_key) or "").strip()
    if not item_id:
        raise PoolContractError(
            f"{platform} 池内行缺 {profile.item_id_key}，无法去重：{dict(row)!r:.120}"
        )
    permalink = str(row.get(profile.permalink_key) or "").strip()
    if not permalink:
        permalink = profile.permalink_template.format(item_id=item_id)
    snapshot = _require_snapshot(row, profile, batch, permalink=permalink)
    fetch_method = str(row.get("fetch_method") or "").strip() or profile.fetch_method
    if fetch_method not in FETCH_METHODS:
        raise PoolContractError(
            f"{platform} 池内行的 fetch_method={fetch_method!r} 不在采集分层闭集"
            f"（schema.sql）：{sorted(FETCH_METHODS)}"
        )
    content = str(row.get(profile.content_key) or "").strip()
    baseline = PLATFORM_BASELINES.get(platform)
    if baseline is None:
        raise PoolContractError(
            f"{platform} 没有可靠度基线（scoring.PLATFORM_BASELINES）；"
            "缺键会静默回落 web_search，见 source-reliability.md §2 注 1"
        )
    metrics: dict[str, Any] = {
        name: _int(row.get(key)) for name, key in profile.metric_keys.items()
    }
    metrics["_raw"] = {
        key: row.get(key) for key in profile.metric_keys.values() if key in row
    }
    collected = (batch.collected_at if batch is not None else None)
    return {
        "platform": platform,
        "source_type": profile.source_type,
        "platform_item_id": item_id,
        "permalink": permalink,
        "title": content[:80] or f"{platform} {item_id}",
        "content_excerpt": content[:8000] or None,
        "author_name": str(row.get(profile.author_key) or "") or None,
        "author_meta": {
            "user_id": str(row.get(profile.author_id_key) or ""),
            "ip_location": str(row.get("ip_location") or ""),
            "profile_url": str(row.get("profile_url") or ""),
        },
        "source_keyword": str(row.get("source_keyword") or "") or None,
        # 采集分层第三档：本机自备爬虫/客户端，不是官方 API 也不是第三方付费 API。
        "fetch_method": fetch_method,
        "published_at": _iso(row.get(profile.created_key), unit=profile.created_unit),
        "fetched_at": (
            fetched_at or collected or _iso(row.get("last_modify_ts"), unit="ms")
            or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        ),
        "raw_metrics": metrics,
        **baseline,
        "rating_notes": _rating_notes(baseline, profile),
        "rated_by": profile.baseline_tag,
        "extra": {
            "content_kind": profile.content_kind,
            "provider": profile.provider,
            "precollect_batch": batch.batch_id if batch is not None else None,
            # 快照路径**相对批次目录**存：池目录会随定容清理搬走/删掉，存绝对
            # 路径将来只会指向一个不存在的地方。批次没了快照也没了，那是清理
            # 策略的既定后果，不在这里假装还在。
            "content_snapshot": snapshot or None,
            "permalink_kind": (
                "permanent" if not profile.permanent_permalink_pattern
                or re.match(profile.permanent_permalink_pattern, permalink)
                else "temporary"
            ),
        },
    }


def _require_snapshot(
    row: Mapping[str, Any], profile: PlatformProfile,
    batch: PrecollectBatch | None, *, permalink: str,
) -> str:
    """临时签名链必须伴正文快照落盘（§M6-d 口径 2），缺一即拒收这一行。

    为什么这道闸非有不可：临时链几小时到一天就过期，过期后报告角标点开是
    「参数错误」页——**「链接可追溯」这条核心判据会在交付之后才失效**，
    验收当天全绿、一周后全断。快照是这类链唯一的兜底证据。
    """

    if not profile.permanent_permalink_pattern:
        return ""
    snapshot = str(row.get(profile.snapshot_key) or "").strip()
    if re.match(profile.permanent_permalink_pattern, permalink):
        return snapshot
    if not snapshot:
        raise PoolContractError(
            f"临时签名链必须附正文快照（{profile.snapshot_key} 为空）：{permalink}"
        )
    if batch is not None and not (batch.directory / snapshot).is_file():
        raise PoolContractError(
            f"临时签名链的正文快照不在批次目录里：{snapshot}（批次 {batch.batch_id}）"
        )
    return snapshot


@dataclass(frozen=True)
class PoolReadResult:
    """读池的**全部**读数：行、批次账、失败原因——判据落在这上面。"""

    items: list[dict[str, Any]]
    batches_scanned: int = 0
    rows_seen: int = 0
    dropped_by_query: int = 0
    dropped_by_window: int = 0
    failure_reasons: tuple[str, ...] = ()
    #: §M6-c：最新批次的读数。`latest_failure_reason` 只在**最新批次** `failed`
    #: 时非空——登录卡的判据钉在这上面，老批次的失败残骸不触发发卡。
    latest_batch_id: str | None = None
    latest_failure_reason: str | None = None

    @property
    def closed_reason(self) -> str:
        """池里没货时，「为什么没有」必须说得出口，不许静默假绿。"""

        if self.failure_reasons:
            return self.failure_reasons[0]
        if self.batches_scanned == 0:
            return "precollect_pool_empty"
        if self.rows_seen == 0:
            return "precollect_batch_empty"
        return "precollect_no_match"


def _window_cutoff(window: str | None, now: datetime) -> datetime | None:
    if not window:
        return None
    matched = _WINDOW_DAYS.match(str(window))
    if matched is None:
        return None
    return now - timedelta(days=int(matched.group(1)))


def load_evidence(
    platform: str, *, query: str | None = None, window: str | None = None,
    limit: int | None = None, root: Path | str | None = None,
    now: datetime | None = None,
) -> PoolReadResult:
    """按平台读池；query 命中「关键词或正文」，window 只筛拿得到发布时间的行。"""

    moment = now or datetime.now(timezone.utc)
    cutoff = _window_cutoff(window, moment)
    needle = (query or "").strip()
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    rows_seen = dropped_query = dropped_window = 0
    reasons: list[str] = []
    batches = iter_batches(platform, root=root)
    for batch in batches:
        reason = batch.failure_reason
        if reason and reason not in reasons:
            reasons.append(reason)
        for row in batch.iter_rows():
            rows_seen += 1
            item = to_evidence(row, platform=platform, batch=batch)
            if item["platform_item_id"] in seen:
                continue
            if needle and not _matches(item, needle):
                dropped_query += 1
                continue
            if cutoff is not None and not _within(item, cutoff):
                dropped_window += 1
                continue
            seen.add(item["platform_item_id"])
            items.append(item)
    items.sort(key=lambda item: item.get("published_at") or "", reverse=True)
    latest = batches[0] if batches else None
    return PoolReadResult(
        items=items[:limit] if limit else items,
        batches_scanned=len(batches), rows_seen=rows_seen,
        dropped_by_query=dropped_query, dropped_by_window=dropped_window,
        failure_reasons=tuple(reasons),
        latest_batch_id=latest.batch_id if latest is not None else None,
        latest_failure_reason=(
            latest.failure_reason
            if latest is not None and latest.status == "failed"
            else None
        ),
    )


def prune_batches(
    platform: str, *, root: Path | str | None = None,
    keep_ok: int = KEEP_OK_BATCHES, keep_failed: int = KEEP_FAILED_BATCHES,
) -> list[str]:
    """定容清理（§M6-c 货 5）：成功批留最近 `keep_ok`、失败批留最近 `keep_failed`。

    只删池目录、不碰库；返回删掉的 batch_id（新旧序照 `iter_batches` 倒序）。
    单个目录删不动（权限/占用）就跳过继续，不让清理失败拖垮导入。
    """

    removed: list[str] = []
    ok_seen = failed_seen = 0
    for batch in iter_batches(platform, root=root):
        if batch.status == "failed":
            failed_seen += 1
            keep = failed_seen <= keep_failed
        else:
            ok_seen += 1
            keep = ok_seen <= keep_ok
        if keep:
            continue
        try:
            shutil.rmtree(batch.directory)
        except OSError:
            continue
        removed.append(batch.batch_id)
    return removed


def latest_login_failure(
    platform: str, *, root: Path | str | None = None
) -> str | None:
    """最新批次因未登录而失败时返回其 batch_id，否则 None（§M6-c）。

    这是登录卡「重试」后的复核尺子：用户答「已补登录」→ 本机重跑预采集 →
    池里应出现**更新的成功批次**顶掉失败批次；判据落在池状态上，
    不落在「重试函数退没退出异常」上（[[verdict-is-data-not-http200]]）。
    """

    batches = iter_batches(platform, root=root)
    if not batches:
        return None
    latest = batches[0]
    if latest.status == "failed" and latest.failure_reason == LOGIN_REQUIRED_REASON:
        return latest.batch_id
    return None


def _matches(item: Mapping[str, Any], needle: str) -> bool:
    keyword = str(item.get("source_keyword") or "")
    if needle in keyword or keyword in needle:
        return True
    return needle in str(item.get("content_excerpt") or "")


def _within(item: Mapping[str, Any], cutoff: datetime) -> bool:
    published = item.get("published_at")
    if not published:
        # 拿不到发布时间的行不因时间窗被丢——丢了就等于假装它不存在。
        return True
    try:
        moment = datetime.fromisoformat(str(published).replace("Z", "+00:00"))
    except ValueError:
        return True
    return moment >= cutoff


def _rating_notes(baseline: Mapping[str, int], profile: PlatformProfile) -> str:
    """五段式评分理由（§4.2 正则由 `dao._prepare_evidence` 当场校验）。

    这里写的是**基线占位**，真值由评级链回填；分数从 baseline 取，
    绝不另手抄一份，否则「表里写 0、库里存 1」的老毛病会原地复发。
    """

    from app.reliability.scoring import SCORE_FIELDS

    labels = ("权威", "时效", "交叉", "完整", "无关")
    if len(profile.rating_reasons) != len(SCORE_FIELDS):
        raise PoolContractError(
            f"{profile.baseline_tag} 的 rating_reasons 必须给满五段"
        )
    return " · ".join(
        f"{label}{baseline[field]}:{reason}"
        for label, field, reason in zip(labels, SCORE_FIELDS, profile.rating_reasons)
    )
