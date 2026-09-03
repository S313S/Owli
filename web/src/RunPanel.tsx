import { Button, Empty, Tabs, Tag, Typography } from 'antd'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ApiEnvelope, ResearchSnapshot, SectionHeartbeat, TranscriptLine } from './types'

/** §OBS-2 货 4：工作板底部运行面板——一张卡片一个 tab，内容是引擎原始流。 */

const MIN_HEIGHT = 120
const COLLAPSED_HEIGHT = 32
const TAIL_LINES = 200
const POLL_MS = 3000
const HEIGHT_KEY = 'owli:run-panel:height'
const COLLAPSED_KEY = 'owli:run-panel:collapsed'

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
export function renderLine(line: TranscriptLine): string {
  const stamp = new Date((line.ts ?? 0) * 1000).toLocaleTimeString('zh-CN', { hour12: false })
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

/** 只有跑起来过的卡片才有原始流可看：queued 的还没产生任何事件。 */
export function panelTabs(snapshot: ResearchSnapshot): PanelTab[] {
  const tabs: PanelTab[] = []
  snapshot.goals.forEach((goal, index) => {
    goal.agents.forEach((agent) => {
      if (agent.status === 'queued' || agent.status === 'skipped') return
      tabs.push({
        key: agent.id, goalIndex: index + 1, goalId: goal.id,
        name: agent.name, engine: agent.engine, status: agent.status,
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

function useTranscript(researchId: string, tab: PanelTab | undefined, live: boolean) {
  const [lines, setLines] = useState<TranscriptLine[]>([])
  const seqRef = useRef(0)

  const pull = useCallback(async (incremental: boolean) => {
    if (!tab) return
    const query = incremental && seqRef.current
      ? `tail=${TAIL_LINES}&after_seq=${seqRef.current}`
      : `tail=${TAIL_LINES}`
    const response = await fetch(
      `/api/researches/${encodeURIComponent(researchId)}/sections/`
      + `${encodeURIComponent(tab.goalId)}/${encodeURIComponent(tab.key)}/transcript?${query}`,
    )
    if (!response.ok) return
    const body = await response.json() as ApiEnvelope<{ lines: TranscriptLine[]; last_seq: number }>
    const fresh = body.data?.lines ?? []
    seqRef.current = body.data?.last_seq ?? seqRef.current
    setLines((current) => (incremental ? [...current, ...fresh].slice(-TAIL_LINES) : fresh))
  }, [researchId, tab])

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
  const lines = useTranscript(researchId, collapsed ? undefined : current, live && !collapsed)

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
        children: <pre className="run-panel-lines" data-testid={`transcript-${tab.key}`}>
          {lines.length ? lines.map(renderLine).join('\n') : '这一节还没有引擎原始事件落盘'}
        </pre>,
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
