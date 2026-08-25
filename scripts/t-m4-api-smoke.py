#!/usr/bin/env python3
"""M4 阶段 API 冒烟：结构化断言优先，不以退出码代替证据。

覆盖「新研究全流程不退化」这条跨包复用的验收判据：
创建 → 等计划 → 答决策天平追问 → PUT 计划 → approve → 立即 stop → 复核。

**不进整跑**：approve 之后立刻 stop，只验通路，不烧执行期。

顺带复核 M4-b 引入的历史只读快照未抢占活研究——活研究详情的
``snapshot_source`` 必须为空（历史研究才是 ``"store"``）。

用法::

    ../Owli/.venv/bin/python scripts/t-m4-api-smoke.py --port 8723
    ../Owli/.venv/bin/python scripts/t-m4-api-smoke.py --port 8723 --history-id r-7dd51507c784

注意：
- 每个 POST 都带**全新** X-Request-ID（项目验收硬要求，接口按它做幂等缓存）；
  PUT /plan 的签名不要求该头。
- 一律绕开本机代理——它会劫持 localhost（见 urllib 的 ProxyHandler({})）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 绕本机代理
FAILURES: list[str] = []


def call(base: str, method: str, path: str, body: Any = None,
         request_id: bool = False, timeout: int = 120) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if request_id:
        req.add_header("X-Request-ID", str(uuid.uuid4()))  # 每次都换新的
    try:
        with OPENER.open(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def check(label: str, ok: bool, detail: str) -> None:
    print(f"  {'PASS' if ok else 'FAIL'} | {label} | {detail}", flush=True)
    if not ok:
        FAILURES.append(label)


def main() -> int:
    ap = argparse.ArgumentParser(description="M4 阶段 API 冒烟")
    ap.add_argument("--port", type=int, required=True,
                    help="验收服务端口（8721 常被在跑服务占用，起前先 lsof 查）")
    ap.add_argument("--query", default="豆包语音输入法的竞品分析",
                    help="固定快速档题目，见 decision-log 2026-08-22")
    ap.add_argument("--scale", default="fast")
    ap.add_argument("--answer", default="服务产品经理的功能取舍决策",
                    help="决策天平追问的答案；不答则 approve 必被 422 挡下")
    ap.add_argument("--plan-timeout", type=int, default=900,
                    help="等计划生成的上限秒数")
    ap.add_argument("--history-id", default=None,
                    help="可选：一条历史研究 id，用于复核只读快照仍为 store")
    args = ap.parse_args()
    base = f"http://127.0.0.1:{args.port}"

    print(f"[1/6] 创建研究 · {args.query} · scale={args.scale}", flush=True)
    status, env = call(base, "POST", "/api/researches",
                       {"query": args.query, "scale": args.scale}, request_id=True)
    if status != 200 or not env.get("ok"):
        check("创建研究", False, f"HTTP {status} {env.get('error')}")
        return 1
    rid = env["data"]["research_id"]
    check("创建研究", True, f"HTTP 200 research_id={rid}")

    print(f"[2/6] 等计划生成（上限 {args.plan_timeout}s）", flush=True)
    deadline, last, detail = time.time() + args.plan_timeout, None, {}
    while time.time() < deadline:
        time.sleep(15)
        try:
            _, e = call(base, "GET", f"/api/researches/{rid}", timeout=30)
        except Exception as exc:                       # 服务抖动不算失败，继续等
            print(f"       轮询异常（继续等）: {exc}", flush=True)
            continue
        detail = e.get("data") or {}
        st = detail.get("status")
        if st != last:
            print(f"       status={st}", flush=True)
            last = st
        if st != "planning":
            break
    check("计划生成", last == "awaiting_review", f"status={last}（期望 awaiting_review）")
    # 活研究必须走内存路径；若这里是 store，说明历史快照抢占了活研究
    check("活研究未被历史快照抢占", detail.get("snapshot_source") is None,
          f"snapshot_source={detail.get('snapshot_source')!r}（期望 None）")
    if last != "awaiting_review":
        return 1

    print("[3/6] 答决策天平追问并提交计划", flush=True)
    _, env = call(base, "GET", f"/api/researches/{rid}/plan")
    plan = env["data"]
    balance = plan.get("decision_balance") or []
    if not balance:
        check("决策天平存在", False, "plan.decision_balance 为空")
        return 1
    print(f"       追问：{balance[0].get('question')}", flush=True)
    balance[0]["answer"] = args.answer
    rev_before = plan.get("plan_rev")
    # PUT 收整份计划，签名不要求 X-Request-ID
    status, env = call(base, "PUT", f"/api/researches/{rid}/plan", plan)
    rev_after = (env.get("data") or {}).get("plan_rev")
    check("PUT 计划", status == 200 and env.get("ok"),
          f"HTTP {status} plan_rev {rev_before}→{rev_after} err={env.get('error')}")

    print("[4/6] approve", flush=True)
    status, env = call(base, "POST", f"/api/researches/{rid}/plan/approve", request_id=True)
    d = env.get("data") or {}
    check("approve", status == 200 and env.get("ok"),
          f"HTTP {status} status={d.get('status')} plan_rev={d.get('plan_rev')} "
          f"approved_at={d.get('approved_at')} err={env.get('error')}")

    print("[5/6] 立即 stop（不进整跑）", flush=True)
    status, env = call(base, "POST", f"/api/researches/{rid}/stop", request_id=True)
    check("stop", status == 200 and env.get("ok"), f"HTTP {status} err={env.get('error')}")
    _, env = call(base, "GET", f"/api/researches/{rid}")
    d = env.get("data") or {}
    check("stop 后仍走内存路径", d.get("snapshot_source") is None,
          f"status={d.get('status')} snapshot_source={d.get('snapshot_source')!r}")

    print("[6/6] 历史只读快照复核", flush=True)
    if args.history_id:
        status, env = call(base, "GET", f"/api/researches/{args.history_id}")
        d = env.get("data") or {}
        check("历史研究 200 只读快照", status == 200 and d.get("snapshot_source") == "store",
              f"HTTP {status} snapshot_source={d.get('snapshot_source')!r} "
              f"status={d.get('status')}")
        check("历史研究无操作按钮", d.get("actions") == [],
              f"actions={d.get('actions')!r}（期望 []）")
    else:
        print("       跳过（未给 --history-id）", flush=True)

    print(f"\n{'=' * 60}")
    if FAILURES:
        print(f"冒烟未通过，失败断言：{', '.join(FAILURES)}")
        return 1
    print(f"冒烟全部通过。本次研究 id={rid}（已 stop，未进整跑）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
