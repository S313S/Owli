import { Alert, Button, Collapse, Empty, Popover, Space, Spin, Table, Tag, Typography, message } from 'antd'
import { useEffect, useMemo, useState, type ReactNode } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { ApiEnvelope, EvidenceItem, EvidenceView, ReportView as ReportData } from './types'

// 五维顺序 = evidence 列序 = rating_notes 段序 = Excel G–K（四处同序，spec §5）
export const SCORE_DIMS: Array<[keyof EvidenceItem, string]> = [
  ['score_authority', '权威'], ['score_freshness', '时效'], ['score_crossref', '交叉'],
  ['score_completeness', '完整'], ['score_independence', '无关'],
]
const MARK = /\[S(\d{2})\]/g
const REASON_LABEL: Record<string, string> = {
  timeout: '超时', tool_unavailable: '工具不可用', retry_exhausted: '重试耗尽',
  conclusion_invalid: '结论不合规', empty_result: '空结果', quota_exhausted: '额度耗尽',
}
const gradeColor: Record<string, string> = { A: 'green', B: 'blue', C: 'orange', D: 'red' }

export function useReportData(researchId: string) {
  const [report, setReport] = useState<ReportData | null>(null)
  const [evidence, setEvidence] = useState<EvidenceView | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [reload, setReload] = useState(0)
  useEffect(() => {
    let disposed = false
    const base = `/api/researches/${encodeURIComponent(researchId)}`
    void (async () => {
      try {
        const r = await fetch(`${base}/report`)
        if (!r.ok) throw new Error(`报告读取失败（HTTP ${r.status}）`)
        const body = await r.json() as ApiEnvelope<ReportData>
        if (!disposed) setReport(body.data)
      } catch (e) { if (!disposed) setError(e instanceof Error ? e.message : String(e)) }
      try {
        const r = await fetch(`${base}/evidence`)
        if (r.ok) { const body = await r.json() as ApiEnvelope<EvidenceView>; if (!disposed) setEvidence(body.data) }
      } catch { /* 拿不到 evidence 时按信息源段就地降级 */ }
    })()
    return () => { disposed = true }
  }, [researchId, reload])
  return { report, evidence, error, refresh: () => setReload((n) => n + 1) }
}

export function scoreText(item: Partial<EvidenceItem>): string {
  return SCORE_DIMS.map(([field, label]) => `${label}${item[field] ?? '?'}`).join('/')
}

type Lookup = { byNo: Map<number, EvidenceItem | { permalink: string; title: string }>; listed: Set<number> }

function CitationCard({ item, no }: { item: EvidenceItem | { permalink: string; title: string }; no: number }) {
  const full = 'platform' in item ? item : null
  return <div className="citation-card" data-testid="citation-card">
    <Space wrap>
      <Typography.Text strong>S{String(no).padStart(2, '0')}</Typography.Text>
      {full && <Tag>{full.platform}</Tag>}
      {full && <Tag color={gradeColor[full.grade ?? ''] ?? 'default'}>等级 {full.grade ?? '?'}</Tag>}
    </Space>
    <div><a href={item.permalink} target="_blank" rel="noreferrer">{item.title || item.permalink}</a></div>
    {full && <div className="dims">五维 {scoreText(full)}{full.score_total != null ? ` · 总分 ${full.score_total}` : ''}</div>}
    {full?.rating_notes && <Typography.Paragraph type="secondary" style={{ marginBottom: 4 }}>理由：{full.rating_notes}</Typography.Paragraph>}
    {full?.content_excerpt && <Typography.Paragraph ellipsis={{ rows: 3 }} style={{ marginBottom: 0 }}>{full.content_excerpt}</Typography.Paragraph>}
    {full && <Typography.Text type="secondary">抓取 {full.fetched_at}{full.author_name ? ` · ${full.author_name}` : ''}</Typography.Text>}
  </div>
}

function CitationMark({ no, lookup }: { no: number; lookup: Lookup }) {
  const item = lookup.byNo.get(no)
  const label = `[S${String(no).padStart(2, '0')}]`
  if (!item) return <span className="citation-mark dangling" data-testid="citation-mark" data-citation={no} title="信息源清单里没有这条">{label}</span>
  return <Popover content={<CitationCard item={item} no={no} />} trigger="hover" placement="top">
    <a className="citation-mark" data-testid="citation-mark" data-citation={no}
      href={item.permalink} target="_blank" rel="noreferrer">{label}</a>
  </Popover>
}

/** 把文本节点里的 [Sxx] 换成可点角标；其它文本原样保留。 */
function withMarks(children: ReactNode, lookup: Lookup): ReactNode {
  if (typeof children === 'string') {
    const parts: ReactNode[] = []
    let last = 0
    for (const m of children.matchAll(MARK)) {
      if (m.index! > last) parts.push(children.slice(last, m.index))
      parts.push(<CitationMark key={`${m.index}`} no={Number(m[1])} lookup={lookup} />)
      last = m.index! + m[0].length
    }
    if (!parts.length) return children
    if (last < children.length) parts.push(children.slice(last))
    return parts
  }
  if (Array.isArray(children)) return children.map((child, i) => <span key={i}>{withMarks(child, lookup)}</span>)
  return children
}

function Markdown({ text, lookup }: { text: string; lookup: Lookup }) {
  type Tag = 'p' | 'li' | 'td' | 'th' | 'h1' | 'h2' | 'h3' | 'h4' | 'blockquote' | 'strong' | 'em'
  const wrap = (tag: Tag) =>
    ({ children, node: _n, ...rest }: { children?: ReactNode; node?: unknown }) => {
      const El = tag as 'p'
      return <El {...rest}>{withMarks(children, lookup)}</El>
    }
  return <ReactMarkdown remarkPlugins={[remarkGfm]}
    components={{ p: wrap('p'), li: wrap('li'), td: wrap('td'), th: wrap('th'), h1: wrap('h1'), h2: wrap('h2'), h3: wrap('h3'), h4: wrap('h4'), blockquote: wrap('blockquote'), strong: wrap('strong'), em: wrap('em') }}>
    {text}
  </ReactMarkdown>
}

function References({ report, evidence }: { report: ReportData; evidence: EvidenceView | null }) {
  const cited = evidence ? evidence.items.filter((i) => i.citation_no != null) : []
  const uncited = evidence ? evidence.items.filter((i) => i.citation_no == null) : []
  const rows: EvidenceItem[] = cited.length ? cited : report.sources.map((s) => ({
    id: `src-${s.citation_no}`, citation_no: s.citation_no, permalink: s.permalink, title: s.title, platform: '—',
    fetched_at: '', score_authority: null, score_freshness: null, score_crossref: null,
    score_completeness: null, score_independence: null, score_total: null, grade: null,
  }))
  const columns = [
    { title: '角标', dataIndex: 'citation_no', width: 64, render: (n: number) => `S${String(n).padStart(2, '0')}` },
    { title: '平台', dataIndex: 'platform', width: 90 },
    { title: '标题', dataIndex: 'title', render: (t: string, r: EvidenceItem) => <a href={r.permalink} target="_blank" rel="noreferrer">{t || r.permalink}</a> },
    { title: '等级', dataIndex: 'grade', width: 64, render: (g: string | null) => <Tag color={gradeColor[g ?? ''] ?? 'default'}>{g ?? '?'}</Tag> },
    { title: '五维（权威/时效/交叉/完整/无关）', key: 'dims', width: 220, render: (_: unknown, r: EvidenceItem) => scoreText(r) },
    { title: '抓取时间', dataIndex: 'fetched_at', width: 170 },
  ]
  return <section data-testid="report-references">
    <Typography.Title level={4}>参考文献（{rows.length}）</Typography.Title>
    {rows.length
      ? <Table size="small" rowKey="id" pagination={false} dataSource={rows} columns={columns} />
      : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="成稿没有引用任何信息源" />}
    {uncited.length > 0 && <Collapse size="small" style={{ marginTop: 8 }} items={[{
      key: 'uncited', label: `采到未引用（${uncited.length}）`,
      children: <Table size="small" rowKey="id" pagination={{ pageSize: 20 }} dataSource={uncited}
        columns={columns.filter((c) => c.dataIndex !== 'citation_no')} />,
    }]} />}
  </section>
}

function MissingList({ report }: { report: ReportData }) {
  const groups = useMemo(() => {
    const map = new Map<string, ReportData['missing']>()
    for (const m of report.missing) { const k = m.reason ?? 'unknown'; map.set(k, [...(map.get(k) ?? []), m]) }
    return [...map.entries()]
  }, [report.missing])
  if (!groups.length) return <Alert type="success" showIcon message="缺失清单：无" data-testid="report-missing" />
  return <section data-testid="report-missing">
    <Alert type="warning" showIcon message={`缺失清单：${report.missing.length} 项未写出，按原因分组`} style={{ marginBottom: 8 }} />
    {groups.map(([reason, items]) => <div key={reason} className="report-missing-group" data-reason={reason}>
      <Space wrap><Tag color="orange">{REASON_LABEL[reason] ?? reason}</Tag><Typography.Text type="secondary">{reason} · {items.length} 项</Typography.Text></Space>
      <ul>{items.map((m, i) => <li key={i}>{m.goal_id}{m.chapter_id ? ` / ${m.chapter_id}` : ''}</li>)}</ul>
    </div>)}
  </section>
}

export default function ReportView({ researchId, fallback }: { researchId: string; fallback?: string | null }) {
  const { report, evidence, error, refresh } = useReportData(researchId)
  const lookup = useMemo<Lookup>(() => {
    const byNo = new Map<number, EvidenceItem | { permalink: string; title: string }>()
    for (const s of report?.sources ?? []) byNo.set(s.citation_no, { permalink: s.permalink, title: s.title })
    for (const e of evidence?.items ?? []) if (e.citation_no != null) byNo.set(e.citation_no, e)
    return { byNo, listed: new Set(byNo.keys()) }
  }, [report, evidence])

  if (error) return <>
    <Alert type="warning" showIcon message="结构化报告不可用，显示原始快照" description={error} style={{ marginBottom: 8 }} />
    {fallback && <pre className="history-report-body">{fallback}</pre>}
  </>
  if (!report) return <Spin tip="读取报告…"><div style={{ minHeight: 120 }} /></Spin>

  const dangling = report.citations.dangling
  return <div className="report-view" data-testid="report-view" data-format={report.format}>
    <div className="report-toolbar" data-testid="report-toolbar">
      <ExportButtons researchId={researchId} report={report} onDone={refresh} />
    </div>
    {dangling.length > 0 && <Alert type="error" showIcon style={{ marginBottom: 8 }}
      message={`正文引用了 ${dangling.length} 个清单里没有的角标：${dangling.map((n) => `S${String(n).padStart(2, '0')}`).join('、')}`} />}
    {report.conclusions.length > 0 && <section data-testid="report-conclusions">
      <Typography.Title level={4}>结论</Typography.Title>
      <ul>{report.conclusions.map((c, i) => <li key={i}>{withMarks(c, lookup)}</li>)}</ul>
    </section>}
    {report.sections.map((section, i) => <section key={section.section_id ?? i} data-testid="report-section" data-placeholder={section.placeholder}>
      {section.placeholder
        ? <div className="report-section-placeholder">
            <Typography.Text strong>{section.title ?? section.section_id}</Typography.Text>
            <div>此节未写出 · 原因：<Tag color="orange">{REASON_LABEL[section.missing_reason ?? ''] ?? section.missing_reason}</Tag></div>
          </div>
        : <Markdown text={section.markdown} lookup={lookup} />}
    </section>)}
    <References report={report} evidence={evidence} />
    <MissingList report={report} />
  </div>
}

function ExportButtons({ researchId, report, onDone }: { researchId: string; report: ReportData; onDone: () => void }) {
  const [busy, setBusy] = useState<string | null>(null)
  const run = async (kind: 'excel' | 'feishu') => {
    setBusy(kind)
    try {
      const r = await fetch(`/api/researches/${encodeURIComponent(researchId)}/export`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ kind }),
      })
      const body = await r.json() as ApiEnvelope<{ kind: string; url?: string | null; status?: string; message?: string }>
      if (!r.ok || !body.ok) throw new Error(body.error?.message ?? `HTTP ${r.status}`)
      if (kind === 'excel' && body.data.url) { window.open(body.data.url, '_blank'); void message.success('Excel 已生成') }
      else void message[body.data.status === 'skipped' ? 'warning' : 'success'](body.data.message ?? '已推送飞书')
      onDone()
    } catch (e) { void message.error(e instanceof Error ? e.message : String(e)) } finally { setBusy(null) }
  }
  const excel = report.exports.filter((x) => x.kind === 'excel').at(-1)
  return <>
    <Button size="small" loading={busy === 'excel'} onClick={() => void run('excel')} data-testid="export-excel">导出 Excel</Button>
    <Button size="small" loading={busy === 'feishu'} onClick={() => void run('feishu')} data-testid="export-feishu">推送飞书</Button>
    {excel?.url && <a href={excel.url} target="_blank" rel="noreferrer">上次导出 {excel.created_at}</a>}
    {report.feishu.doc_url && <a href={report.feishu.doc_url} target="_blank" rel="noreferrer">飞书云文档</a>}
    {report.feishu.status && report.feishu.status !== 'pending' && <Tag>飞书 {report.feishu.status}</Tag>}
  </>
}
