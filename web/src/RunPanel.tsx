import { Button, Empty, Tabs, Tag, Typography } from 'antd'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type {
  ApiEnvelope, ProgressLine, ResearchSnapshot, SectionHeartbeat, TranscriptLine,
} from './types'

/** §OBS-2 货 4 + §OBS-3：底部运行面板——一张卡片一个 tab，tab 里并排两栏。
 *
 * 面板头一行两个**互斥标签**（像终端软件那样）：「进程」= 后端 `/progress` 译好的
 * 人话行（默认显示）、「日志」= 引擎原始流原样倒出（OBS-2 的行为一字未改）。
 * 同一时刻只显示一个；选哪个记在 localStorage。下方节标签条不动。
 */

const MIN_HEIGHT = 120
const COLLAPSED_HEIGHT = 32
/** §OBS-4 货 4：拉全量而不是只拉尾巴。2000 是接口上限（`tail` 的 `le=2000`，
 *  也是 `read_transcript` 的 MAX_READ_LINES）；夜跑库最长的一章 348 行，够用有余。
 *  真撞到上限时栏顶出一行提示，让人知道上面还有没显示的行。 */
const MAX_LINES = 2000
const POLL_MS = 3000
const HEIGHT_KEY = 'owli:run-panel:height'
const COLLAPSED_KEY = 'owli:run-panel:collapsed'
const VIEW_KEY = 'owli:run-panel:view'

/** localStorage 在无痕/禁站点数据下会直接抛，读写都得兜住。 */
function readStored(key: string, fallback: string): string {
  try {
    return window.localStorage.getItem(key) ?? fallback
  } catch {
    return fallback
  }
}

function writeStored(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value)
  } catch {
    /* 存不下就只在本次会话里生效，不影响面板本身 */
  }
}

function maxHeight(): number {
  return Math.max(MIN_HEIGHT, Math.round(window.innerHeight * 0.7))
}

export function formatElapsed(seconds: number): string {
  const total = Math.max(0, Math.round(seconds))
  const minutes = Math.floor(total / 60)
  return minutes ? `${minutes}m${String(total % 60).padStart(2, '0')}s` : `${total}s`
}

/** 一行原始事件压成一行可读文本：时间 + seq + 事件本体。 */
export function stampOf(ts: number | undefined): string {
  return new Date((ts ?? 0) * 1000).toLocaleTimeString('zh-CN', { hour12: false })
}

export function renderLine(line: TranscriptLine): string {
  const stamp = stampOf(line.ts)
  const body = typeof line.event === 'string' ? line.event : JSON.stringify(line.event)
  return `${stamp} #${line.seq} ${body}`
}

type PanelTab = {
  key: string
  goalIndex: number
  goalId: string
  name: string
  engine: string
  status: string
}

/** 只有跑起来过的卡片才有原始流可看：queued 的还没产生任何事件。
 *
 * 心跳（货 3）也算「跑起来了」：快照里的 agent 状态要等下一次全量刷新才变，
 * 心跳是 SSE 实时来的，认它就不必刷新页面才看得到新 tab。
 */
export function panelTabs(snapshot: ResearchSnapshot): PanelTab[] {
  const beating = new Set(Object.values(snapshot.heartbeats ?? {}).map((beat) => beat.agent))
  const tabs: PanelTab[] = []
  snapshot.goals.forEach((goal, index) => {
    goal.agents.forEach((agent) => {
      const idle = agent.status === 'queued' || agent.status === 'skipped'
      if (idle && !beating.has(agent.id)) return
      tabs.push({
        key: agent.id, goalIndex: index + 1, goalId: goal.id,
        name: agent.name, engine: agent.engine,
        status: idle ? 'running' : agent.status,
      })
    })
  })
  return tabs
}

/** 默认选最近有心跳的那张卡；一条心跳都没有就选第一张运行中的。 */
export function defaultTabKey(
  tabs: PanelTab[], heartbeats: Record<string, SectionHeartbeat> | undefined,
): string {
  let best: { key: string; at: number } | null = null
  tabs.forEach((tab) => {
    const beat = heartbeats?.[tab.key]
    if (beat && (!best || beat.received_at > best.at)) best = { key: tab.key, at: beat.received_at }
  })
  if (best) return (best as { key: string }).key
  return (tabs.find((tab) => tab.status === 'running') ?? tabs[0])?.key ?? ''
}

/** 增量合并：老行在前、新行在后，同一个 seq 只留一份（后到的覆盖先到的）。 */
export function mergeBySeq<T extends { seq: number }>(current: T[], fresh: T[]): T[] {
  if (!fresh.length) return current
  const seen = new Map<number, T>()
  current.forEach((line) => seen.set(line.seq, line))
  fresh.forEach((line) => seen.set(line.seq, line))
  return [...seen.values()].sort((left, right) => left.seq - right.seq)
}

/** 一个 tab 两条流：`transcript`（日志栏）与 `progress`（进程栏），同参同源。 */
function useTail<T extends { seq: number }>(
  researchId: string, tab: PanelTab | undefined, live: boolean, view: 'transcript' | 'progress',
) {
  const [lines, setLines] = useState<T[]>([])
  const seqRef = useRef(0)

  const pull = useCallback(async (incremental: boolean) => {
    if (!tab) return
    const query = incremental && seqRef.current
      ? `tail=${MAX_LINES}&after_seq=${seqRef.current}`
      : `tail=${MAX_LINES}`
    const section = `${encodeURIComponent(tab.goalId)}/${encodeURIComponent(tab.key)}`
    const response = await fetch(
      `/api/researches/${encodeURIComponent(researchId)}/sections/${section}/${view}?${query}`,
    )
    if (!response.ok) return
    const body = await response.json() as ApiEnvelope<{ lines: T[]; last_seq: number }>
    const fresh = body.data?.lines ?? []
    seqRef.current = body.data?.last_seq ?? seqRef.current
    // 全量保留、按 seq 去重：终态行（seq 在 2e9 号段）不计入 last_seq，
    // 每次增量都会再回一遍，去重了才不会一轮一轮堆重复。
    setLines((current) => (incremental ? mergeBySeq(current, fresh) : fresh))
  }, [researchId, tab, view])

  useEffect(() => {
    seqRef.current = 0
    setLines([])
    void pull(false).catch(() => undefined)
  }, [pull])

  useEffect(() => {
    if (!live || !tab) return
    const timer = window.setInterval(() => void pull(true).catch(() => undefined), POLL_MS)
    return () => window.clearInterval(timer)
  }, [live, pull, tab])

  return lines
}

/** 撞到接口上限时栏顶挂一条，让人知道上面还有没显示的行（货 4）。 */
function TruncationHint({ count }: { count: number }) {
  if (count < MAX_LINES) return null
  return <div className="run-panel-truncated" data-testid="run-panel-truncated">
    只显示最近 {MAX_LINES} 行，更早的行未加载
  </div>
}

/** 进程视图：一行「时间 · 阶段 · 一句人话」。文本已由后端译好，这里只排版。 */
function ProgressView({ tabKey, lines }: { tabKey: string; lines: ProgressLine[] }) {
  return <div className="run-panel-col" data-testid={`run-panel-progress-${tabKey}`}>
    <TruncationHint count={lines.length} />
    {lines.length ? <ol className="run-panel-progress">
      {lines.map((line) => <li key={`${line.seq}-${line.stage}-${line.text.slice(0, 12)}`}>
        <span className="progress-time">{stampOf(line.ts)}</span>
        <span className={`progress-stage progress-${line.kind}`}>{line.stage}</span>
        <span className="progress-text">{line.text}</span>
      </li>)}
    </ol> : <div className="run-panel-blank">这一节还没有可读的进展</div>}
  </div>
}

export default function RunPanel({ researchId, snapshot }: {
  researchId: string
  snapshot: ResearchSnapshot
}) {
  const [height, setHeight] = useState(() => Number(readStored(HEIGHT_KEY, '240')) || 240)
  const [collapsed, setCollapsed] = useState(() => readStored(COLLAPSED_KEY, '0') === '1')
  const [active, setActive] = useState('')
  const dragRef = useRef<{ y: number; height: number } | null>(null)

  const tabs = useMemo(() => panelTabs(snapshot), [snapshot])
  const preferred = useMemo(() => defaultTabKey(tabs, snapshot.heartbeats), [tabs, snapshot.heartbeats])
  const current = tabs.find((tab) => tab.key === active) ?? tabs.find((tab) => tab.key === preferred)
  const live = current?.status === 'running' || current?.status === 'retrying'
  const [view, setView] = useState<'progress' | 'transcript'>(
    () => (readStored(VIEW_KEY, 'progress') === 'transcript' ? 'transcript' : 'progress'),
  )
  const target = collapsed ? undefined : current
  const polling = live && !collapsed
  // 两个视图互斥：只有正在显示的那个才拉流，另一个连请求都不发
  const lines = useTail<TranscriptLine>(
    researchId, view === 'transcript' ? target : undefined, polling, 'transcript',
  )
  const progress = useTail<ProgressLine>(
    researchId, view === 'progress' ? target : undefined, polling, 'progress',
  )

  function pickView(next: 'progress' | 'transcript') {
    writeStored(VIEW_KEY, next)
    setView(next)
  }

  useEffect(() => {
    const move = (event: MouseEvent) => {
      if (!dragRef.current) return
      const next = dragRef.current.height + (dragRef.current.y - event.clientY)
      setHeight(Math.min(maxHeight(), Math.max(MIN_HEIGHT, Math.round(next))))
    }
    const stop = () => {
      if (!dragRef.current) return
      dragRef.current = null
      setHeight((value) => { writeStored(HEIGHT_KEY, String(value)); return value })
    }
    window.addEventListener('mousemove', move)
    window.addEventListener('mouseup', stop)
    return () => {
      window.removeEventListener('mousemove', move)
      window.removeEventListener('mouseup', stop)
    }
  }, [])

  function toggle() {
    setCollapsed((value) => {
      writeStored(COLLAPSED_KEY, value ? '0' : '1')
      return !value
    })
  }

  const beat = current ? snapshot.heartbeats?.[current.key] : undefined
  const body = collapsed ? null : <div className="run-panel-body" style={{ height: height - COLLAPSED_HEIGHT }}>
    {tabs.length ? <Tabs size="small" activeKey={current?.key} onChange={setActive}
      items={tabs.map((tab) => ({
        key: tab.key,
        label: `${tab.goalIndex} · ${tab.name} · ${tab.engine}`,
        children: view === 'transcript'
          // 「日志」：OBS-2 的原样倒出，行为一字未改
          ? <div className="run-panel-col" data-testid={`run-panel-log-${tab.key}`}>
            <TruncationHint count={lines.length} />
            <pre className="run-panel-lines" data-testid={`transcript-${tab.key}`}>
              {lines.length ? lines.map(renderLine).join('\n') : '这一节还没有引擎原始事件落盘'}
            </pre>
          </div>
          : <ProgressView tabKey={tab.key} lines={progress} />,
      }))} />
      : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有卡片跑起来" />}
  </div>

  return <section className="run-panel" data-testid="run-panel" data-collapsed={collapsed ? '1' : '0'}
    style={{ height: collapsed ? COLLAPSED_HEIGHT : height }}>
    <div className="run-panel-grip" data-testid="run-panel-grip"
      onMouseDown={(event) => {
        if (collapsed) return
        dragRef.current = { y: event.clientY, height }
      }} />
    <div className="run-panel-head">
      <b>运行面板</b>
      {/* 货 10：两个互斥标签，像终端软件的 tab；默认「进程」 */}
      {/* 用原生 button 不用 antd Button：后者会在两个汉字之间自动塞一个空格（「进 程」） */}
      <span className="run-panel-views" role="tablist">
        <button type="button" role="tab" data-testid="run-panel-progress"
          className={`run-panel-view${view === 'progress' ? ' is-on' : ''}`}
          aria-selected={view === 'progress'} onClick={() => pickView('progress')}>进程</button>
        <button type="button" role="tab" data-testid="run-panel-log"
          className={`run-panel-view${view === 'transcript' ? ' is-on' : ''}`}
          aria-selected={view === 'transcript'} onClick={() => pickView('transcript')}>日志</button>
      </span>
      {current ? <Tag>{current.name}</Tag> : null}
      {beat ? <Typography.Text type="secondary" data-testid="run-panel-beat">
        已用 {formatElapsed(beat.elapsed_s)}{beat.step_hint ? ` · 最近：${beat.step_hint}` : ''}
      </Typography.Text> : null}
      <span className="run-panel-spacer" />
      <Button size="small" type="text" data-testid="run-panel-toggle" onClick={toggle}>
        {collapsed ? '展开 ▲' : '折叠 ▼'}
      </Button>
    </div>
    {body}
  </section>
}
