import { Card, Space, Spin, Tag, Typography } from 'antd'
import { useEffect, useState } from 'react'
import ReportView from './ReportView'
import type { ApiEnvelope, ResearchSnapshot } from './types'

const statusColor: Record<string, string> = {
  completed: 'success', failed: 'error', archived: 'default', running: 'processing',
}

// FE-1：报告页独立路由 /researches/:id/report。此前只有 /researches/:id 一条，
// 带 /report 的链接会落到 App 兜底渲染成首页（任意端口皆然，含 8721）。
export default function ReportPage({ researchId }: { researchId: string }) {
  const [snapshot, setSnapshot] = useState<ResearchSnapshot | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let disposed = false
    void (async () => {
      try {
        const r = await fetch(`/api/researches/${encodeURIComponent(researchId)}`)
        if (!r.ok) throw new Error(`调研不存在或读取失败（HTTP ${r.status}）`)
        const body = await r.json() as ApiEnvelope<ResearchSnapshot>
        if (!disposed) setSnapshot(body.data)
      } catch (e) { if (!disposed) setError(e instanceof Error ? e.message : String(e)) }
    })()
    return () => { disposed = true }
  }, [researchId])

  // D-030：快照接口只用来取标题，拿不到不该拖垮整页——8721 上实测
  // /api/researches/<id> 对历史调研 404、而 /report 同时是 200。报告页的活
  // 是把报告显示出来，读不到标题就退成显示 id，报告本身能不能读由 ReportView 自己报。
  if (!snapshot && !error) return <main className="board-page report-page" data-testid="report-page">
    <Spin tip="读取调研…"><div style={{ minHeight: 160 }} /></Spin>
  </main>

  return <main className="board-page report-page" data-testid="report-page">
    <Card className="board-top">
      <Space wrap>
        <Typography.Title level={3} style={{ margin: 0 }}>{snapshot?.title ?? researchId}</Typography.Title>
        {snapshot
          ? <Tag color={statusColor[snapshot.status] ?? 'default'}>{snapshot.status_label}</Tag>
          : <Tag color="default" data-testid="snapshot-unavailable">调研状态不可用</Tag>}
      </Space>
      <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>
        <a href={`/researches/${encodeURIComponent(researchId)}`}>← 回工作板</a>
        {' · '}{researchId}
      </Typography.Paragraph>
    </Card>
    <Card title="报告产物" className="history-report" data-testid="report-page-body">
      <ReportView researchId={researchId} />
    </Card>
  </main>
}
