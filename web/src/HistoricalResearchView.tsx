import { Alert, Card, Empty, List, Space, Tag, Typography } from 'antd'
import type { ResearchSnapshot } from './types'
import ReportView from './ReportView'

const statusColor: Record<string, string> = {
  completed: 'success', failed: 'error', archived: 'default',
  done: 'success', missing: 'error', deferred: 'warning',
  running: 'processing', pending: 'default',
}

function readableReport(content: string | null | undefined): string | null {
  if (!content) return null
  try {
    const parsed = JSON.parse(content) as {
      sections?: Array<{ title?: unknown; markdown?: unknown }>
      '报告正文'?: unknown
    }
    if (typeof parsed['报告正文'] === 'string') return parsed['报告正文']
    if (Array.isArray(parsed.sections)) {
      const sections = parsed.sections.flatMap((section) => {
        if (typeof section.markdown !== 'string') return []
        const title = typeof section.title === 'string' ? `## ${section.title}\n\n` : ''
        return [`${title}${section.markdown}`]
      })
      if (sections.length) return sections.join('\n\n')
    }
    return JSON.stringify(parsed, null, 2)
  } catch {
    return content
  }
}

export default function HistoricalResearchView({ snapshot }: { snapshot: ResearchSnapshot }) {
  const chapters = snapshot.chapters ?? []
  const missing = snapshot.missing ?? []
  const reportBody = readableReport(snapshot.report_content)

  return <main className="board-page historical-research" data-testid="historical-research-view">
    <Alert
      className="history-readonly-banner"
      type="info"
      showIcon
      message="历史只读"
      description="这是服务重启后从报告库与章账本恢复的快照，仅供查看，不提供暂停、停止或恢复操作。"
    />

    <Card className="board-top history-heading">
      <Space wrap>
        <Typography.Title level={3}>{snapshot.title}</Typography.Title>
        <Tag color={statusColor[snapshot.status]}>{snapshot.status_label}</Tag>
      </Space>
      <Typography.Paragraph>{snapshot.summary ?? snapshot.summary_line ?? snapshot.progress.summary}</Typography.Paragraph>
      <Typography.Text type="secondary">
        账本终态 {snapshot.progress.done} / {snapshot.progress.total} 个 goal
      </Typography.Text>
      <br />
      <Typography.Text type="secondary">
        LLM 实测用量：调用 {snapshot.usage.calls} 次 · 输入 {snapshot.usage.input_tokens.toLocaleString()} ·
        缓存命中 {snapshot.usage.cached_input_tokens.toLocaleString()} ·
        输出 {snapshot.usage.output_tokens.toLocaleString()} ·
        已知成本 ${snapshot.usage.cost_usd.toFixed(6)}（{snapshot.usage.costed_calls}/{snapshot.usage.calls} 次有成本）
      </Typography.Text>
    </Card>

    <section className="history-grid">
      <Card
        title="报告产物"
        className="history-report"
        data-testid="history-report"
        // FE-1 货 4：报告页 /researches/<id>/report 有了路由也得有人指过去，
        // 否则用户只能手敲 URL。只读视图不放 antd Button（历史页无操作组件契约）。
        extra={reportBody
          ? <a data-testid="open-report-page"
               href={`/researches/${encodeURIComponent(snapshot.research_id)}/report`}>
              打开报告页 →
            </a>
          : null}
      >
        {reportBody
          ? <ReportView researchId={snapshot.research_id} fallback={reportBody} />
          : <Alert
              type="warning"
              showIcon
              message="报告正文不可用"
              description={snapshot.report_path ? `账本记录路径：${snapshot.report_path}` : '报告库没有记录产物路径。'}
            />}
      </Card>

      <Card title={`章账本（${chapters.length}）`} className="history-ledger" data-testid="history-ledger">
        {chapters.length
          ? <List
              dataSource={chapters}
              renderItem={(chapter) => <List.Item className="history-ledger-row">
                <List.Item.Meta
                  title={<Space wrap>
                    <Typography.Text strong>{chapter.goal_id} / {chapter.chapter_id}</Typography.Text>
                    <Tag color={statusColor[chapter.status]}>{chapter.status}</Tag>
                  </Space>}
                  description={
                    <Space direction="vertical" size={0}>
                      <span>尝试 {chapter.attempts} 次{chapter.engine ? ` · ${chapter.engine}` : ''}</span>
                      {chapter.actual_output_path && <span>产物：{chapter.actual_output_path}</span>}
                      <span>更新：{chapter.updated_at}</span>
                    </Space>
                  }
                />
              </List.Item>}
            />
          : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有章账本记录" />}
      </Card>

      <Card title={`缺失清单（${missing.length}）`} className="history-missing" data-testid="history-missing">
        {missing.length
          ? <List
              dataSource={missing}
              renderItem={(item) => <List.Item className="history-missing-row">
                <List.Item.Meta
                  title={<Space wrap>
                    <Typography.Text strong>{item.goal_id} / {item.chapter_id}</Typography.Text>
                    <Tag color={statusColor[item.status]}>{item.status}</Tag>
                  </Space>}
                  description={<>
                    <div>原因：{item.reason ?? '未记录'}</div>
                    {item.error && <div>错误摘要：{item.error}</div>}
                  </>}
                />
              </List.Item>}
            />
          : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="无缺失项" />}
      </Card>
    </section>
  </main>
}
