import { Alert, Button, Collapse, Empty, Popover, Segmented, Select, Space, Spin, Table, Tag, Typography, message } from 'antd'
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { ApiEnvelope, EvidenceItem, EvidenceView, PolishedReport, PolishedTable,
  ReportSection, ReportTemplate, ReportView as ReportData } from './types'

// 五维顺序 = evidence 列序 = rating_notes 段序 = Excel G–K（四处同序，spec §5）
export const SCORE_DIMS: Array<[keyof EvidenceItem, string]> = [
  ['score_authority', '权威'], ['score_freshness', '时效'], ['score_crossref', '交叉'],
  ['score_completeness', '完整'], ['score_independence', '无关'],
]
const MARK = /\[S(\d{2})\]/g
// §RPT-2 货 5 ②：原因码的人话词表。历史只读页那张缺失清单卡也要用同一份——
// 两处各写一份，改了一处另一处照样把 conclusion_invalid 印给读者看。
export const REASON_LABEL: Record<string, string> = {
  timeout: '超时', tool_unavailable: '工具不可用', retry_exhausted: '重试耗尽',
  conclusion_invalid: '结论不合规', empty_result: '空结果', quota_exhausted: '额度耗尽',
}
const gradeColor: Record<string, string> = { A: 'green', B: 'blue', C: 'orange', D: 'red' }
// §CMT-1 货 5：kind=comment 是读者反应，不是帖子作者的说法——列表里要一眼分得开。
const KIND_LABEL: Record<string, string> = { post: '帖', comment: '评论' }
const kindOf = (item: { kind?: string | null }) => (item.kind === 'comment' ? 'comment' : 'post')

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
      {full && kindOf(full) === 'comment' && <Tag color="purple">评论</Tag>}
      {full && <Tag color={gradeColor[full.grade ?? ''] ?? 'default'}>等级 {full.grade ?? '?'}</Tag>}
    </Space>
    <div><a href={item.permalink} target="_blank" rel="noreferrer">{item.title || item.permalink}</a></div>
    {full?.parent_permalink && <div className="dims">
      父帖 <a href={full.parent_permalink} target="_blank" rel="noreferrer">{full.parent_permalink}</a>
    </div>}
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
  const [kindFilter, setKindFilter] = useState<'all' | 'post' | 'comment'>('all')
  const listedTitle = new Map(report.sources.map((s) => [s.citation_no, s.title]))
  const keep = (i: EvidenceItem) => kindFilter === 'all' || kindOf(i) === kindFilter
  const cited = evidence
    ? evidence.items.filter((i) => i.citation_no != null).filter(keep)
        .map((i) => (i.title ? i : { ...i, title: listedTitle.get(i.citation_no!) ?? '' }))
    : []
  const uncited = evidence ? evidence.items.filter((i) => i.citation_no == null).filter(keep) : []
  const commentCount = evidence ? evidence.items.filter((i) => kindOf(i) === 'comment').length : 0
  // 筛到「评论」时不能回落成稿信息源段——那份没有 kind，会把帖子当评论显示。
  const rows: EvidenceItem[] = cited.length || kindFilter !== 'all' ? cited : report.sources.map((s) => ({
    id: `src-${s.citation_no}`, citation_no: s.citation_no, permalink: s.permalink, title: s.title, platform: '—',
    fetched_at: '', score_authority: null, score_freshness: null, score_crossref: null,
    score_completeness: null, score_independence: null, score_total: null, grade: null,
  }))
  const columns = [
    { title: '角标', dataIndex: 'citation_no', width: 64, render: (n: number) => `S${String(n).padStart(2, '0')}` },
    { title: '平台', dataIndex: 'platform', width: 90 },
    { title: '类型', dataIndex: 'kind', width: 72, render: (_: unknown, r: EvidenceItem) =>
      <Tag color={kindOf(r) === 'comment' ? 'purple' : 'default'} data-testid="evidence-kind">{KIND_LABEL[kindOf(r)]}</Tag> },
    { title: '标题', dataIndex: 'title', render: (t: string, r: EvidenceItem) => <a href={r.permalink} target="_blank" rel="noreferrer">{t || r.permalink}</a> },
    { title: '等级', dataIndex: 'grade', width: 64, render: (g: string | null) => <Tag color={gradeColor[g ?? ''] ?? 'default'}>{g ?? '?'}</Tag> },
    { title: '五维（权威/时效/交叉/完整/无关）', key: 'dims', width: 220, render: (_: unknown, r: EvidenceItem) => scoreText(r) },
    { title: '抓取时间', dataIndex: 'fetched_at', width: 170 },
  ]
  return <section data-testid="report-references">
    <Space align="center" wrap style={{ marginBottom: 8 }}>
      <Typography.Title level={4} style={{ margin: 0 }}>参考文献（{rows.length}）</Typography.Title>
      {commentCount > 0 && <Segmented
        size="small"
        data-testid="evidence-kind-filter"
        value={kindFilter}
        onChange={(value) => setKindFilter(value as 'all' | 'post' | 'comment')}
        options={[
          { label: '全部', value: 'all' },
          { label: '帖', value: 'post' },
          { label: `评论 ${commentCount}`, value: 'comment' },
        ]} />}
    </Space>
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
      {/* §RPT-2 货 5 ②：原因码只当分组键用，不许印到页面上——读者不认识 conclusion_invalid。 */}
      <Space wrap><Tag color="orange">{REASON_LABEL[reason] ?? '未写出'}</Tag><Typography.Text type="secondary">{items.length} 项</Typography.Text></Space>
      <ul>{items.map((m, i) => <li key={i}>{m.goal_id}{m.chapter_id ? ` / ${m.chapter_id}` : ''}</li>)}</ul>
    </div>)}
  </section>
}

/** §RPT-1 正式稿：只读一份已整理好的稿；没整理过就是 null，不是错。 */
function usePolishedReport(researchId: string, template: string, reload: number) {
  const [polished, setPolished] = useState<PolishedReport | null>(null)
  const [loading, setLoading] = useState(true)
  useEffect(() => {
    let disposed = false
    setLoading(true)
    void (async () => {
      try {
        const r = await fetch(`/api/researches/${encodeURIComponent(researchId)}/polished`
          + `?template=${encodeURIComponent(template)}`)
        const body = r.ok ? await r.json() as ApiEnvelope<PolishedReport> : null
        if (!disposed) setPolished(body?.ok ? body.data : null)
      } catch { if (!disposed) setPolished(null) } finally { if (!disposed) setLoading(false) }
    })()
    return () => { disposed = true }
  }, [researchId, template, reload])
  return { polished, loading }
}

/** 确定性数据表：写手不许改这里的数，所以原样折叠在正文下面，随时可核。 */
function DeterministicTables({ tables }: { tables: Record<string, PolishedTable> }) {
  const items = Object.values(tables).filter((t) => t && t.rows?.length)
  if (!items.length) return null
  return <Collapse ghost data-testid="polished-tables" items={items.map((t) => ({
    key: t.name,
    label: `${t.title}（n=${t.n}）`,
    children: <>
      <Table size="small" pagination={false} rowKey={(_, i) => String(i)}
        dataSource={t.rows} columns={(t.columns ?? []).map((c) => ({
          title: c, dataIndex: c, render: (v: unknown) => String(v ?? ''),
        }))} />
      <Typography.Paragraph type="secondary" style={{ marginTop: 6, marginBottom: 0 }}>
        口径：{t.basis}
        {Object.keys(t.coverage ?? {}).length > 0 && <> · 覆盖：{Object.entries(t.coverage)
          .map(([k, v]) => `${k} ${v}`).join(' / ')}</>}
      </Typography.Paragraph>
    </>,
  }))} />
}

export default function ReportView({ researchId, fallback }: { researchId: string; fallback?: string | null }) {
  const { report, evidence, error, refresh } = useReportData(researchId)
  // 正式稿是给「拿结论去用的人」看的，有就默认停在它；工作稿随时能切回来。
  const [template, setTemplate] = useState('consulting')
  // §RPT-2 货 1 ①：后端按题面算推荐模板，下拉默认选中它——但用户一旦手动改过，
  // 推荐就不许再回来抢（清单是异步到的，晚于用户第一次点击也可能发生）。
  const templatePicked = useRef(false)
  const [polishTick, setPolishTick] = useState(0)
  const [draft, setDraft] = useState<'work' | 'polished' | null>(null)
  const { polished, loading: polishing } = usePolishedReport(researchId, template, polishTick)
  useEffect(() => {
    if (draft === null && !polishing) setDraft(polished ? 'polished' : 'work')
  }, [draft, polishing, polished])
  const lookup = useMemo<Lookup>(() => {
    const byNo = new Map<number, EvidenceItem | { permalink: string; title: string }>()
    for (const s of report?.sources ?? []) byNo.set(s.citation_no, { permalink: s.permalink, title: s.title })
    for (const e of evidence?.items ?? []) if (e.citation_no != null) {
      const listed = byNo.get(e.citation_no)
      byNo.set(e.citation_no, e.title ? e : { ...e, title: listed?.title ?? '' })
    }
    return { byNo, listed: new Set(byNo.keys()) }
  }, [report, evidence])

  if (error) return <>
    <Alert type="warning" showIcon message="结构化报告不可用，显示原始快照" description={error} style={{ marginBottom: 8 }} />
    {fallback && <pre className="history-report-body">{fallback}</pre>}
  </>
  if (!report) return <Spin tip="读取报告…"><div style={{ minHeight: 120 }} /></Spin>

  const dangling = report.citations.dangling
  const showPolished = draft === 'polished' && polished !== null
  return <div className="report-view" data-testid="report-view" data-format={report.format}>
    <div className="report-toolbar" data-testid="report-toolbar">
      <ExportButtons researchId={researchId} report={report} onDone={refresh}
        template={template}
        onTemplateChange={(name) => { templatePicked.current = true; setTemplate(name) }}
        onRecommended={(name) => { if (!templatePicked.current) setTemplate(name) }}
        onPolished={() => { setPolishTick((n) => n + 1); setDraft('polished') }} />
    </div>
    {polished !== null && <Segmented data-testid="draft-tabs" style={{ marginBottom: 12 }}
      value={showPolished ? 'polished' : 'work'} onChange={(v) => setDraft(v as 'work' | 'polished')}
      options={[{ label: '正式稿', value: 'polished' }, { label: '工作稿', value: 'work' }]} />}
    {showPolished && <div className="polished-view" data-testid="polished-view"
      data-template={polished.template}>
      <Markdown text={polished.markdown} lookup={lookup} />
      <Typography.Title level={5} style={{ marginTop: 16 }}>确定性数据表</Typography.Title>
      <DeterministicTables tables={polished.tables} />
      <References report={report} evidence={evidence} />
    </div>}
    {!showPolished && dangling.length > 0 && <Alert type="error" showIcon style={{ marginBottom: 8 }}
      message={`正文引用了 ${dangling.length} 个清单里没有的角标：${dangling.map((n) => `S${String(n).padStart(2, '0')}`).join('、')}`} />}
    {!showPolished && report.conclusions.length > 0 && <section data-testid="report-conclusions">
      <Typography.Title level={4}>结论</Typography.Title>
      <ul>{report.conclusions.map((c, i) => <li key={i}>{withMarks(c, lookup)}</li>)}</ul>
    </section>}
    {!showPolished && report.sections.map((section, i) => <section key={section.section_id ?? i} data-testid="report-section" data-placeholder={section.placeholder}>
      <ReplaySectionButton researchId={researchId} section={section} />
      {section.placeholder
        ? <div className="report-section-placeholder">
            <Typography.Text strong>{section.title ?? section.section_id}</Typography.Text>
            {/* 人话由后端给（render.missing_text），前端不再拼原因码。 */}
            <div>{section.missing_text ?? '本节未成稿。'}</div>
          </div>
        : <Markdown text={section.markdown} lookup={lookup} />}
    </section>)}
    {!showPolished && <References report={report} evidence={evidence} />}
    {!showPolished && <MissingList report={report} />}
  </div>
}

/** §OBS-2 货 6：拿这一节的旧证据旧产物重跑它自己，跑在一个新 research 上。 */
function ReplaySectionButton({ researchId, section }: {
  researchId: string
  section: ReportSection
}) {
  const [busy, setBusy] = useState(false)
  if (!section.section_id || !section.goal_id) return null
  const replay = async () => {
    setBusy(true)
    try {
      const response = await fetch('/api/researches/replay', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Request-ID': `replay-${crypto.randomUUID()}` },
        body: JSON.stringify({
          source_research_id: researchId,
          from_goal: section.goal_id,
          only_chapters: [section.section_id],
          reset_done: true,
        }),
      })
      const body = await response.json() as ApiEnvelope<{ research_id: string }>
      if (!response.ok || !body.ok) throw new Error(body.error?.message ?? `HTTP ${response.status}`)
      window.location.assign(`/researches/${encodeURIComponent(body.data.research_id)}`)
    } catch (error) {
      void message.error(error instanceof Error ? error.message : String(error))
    } finally { setBusy(false) }
  }
  return <Button size="small" type="link" loading={busy} className="replay-section"
    data-testid={`replay-section-${section.section_id}`} onClick={() => void replay()}>
    用旧数据重跑这节
  </Button>
}

function ExportButtons({ researchId, report, onDone, template, onTemplateChange, onRecommended, onPolished }: {
  researchId: string; report: ReportData; onDone: () => void
  template: string; onTemplateChange: (name: string) => void
  onRecommended: (name: string) => void; onPolished: () => void
}) {
  const [busy, setBusy] = useState<string | null>(null)
  const [templates, setTemplates] = useState<ReportTemplate[]>([])
  const [waited, setWaited] = useState<string | null>(null)
  useEffect(() => {
    void (async () => {
      try {
        const r = await fetch(`/api/report-templates?research_id=${encodeURIComponent(researchId)}`)
        if (!r.ok) return
        const body = await r.json() as ApiEnvelope<{ templates: ReportTemplate[]; recommended?: string }>
        if (!body.ok) return
        setTemplates(body.data.templates)
        if (body.data.recommended) onRecommended(body.data.recommended)
      } catch { /* 拿不到清单就只留默认模板，按钮照样能按 */ }
    })()
    // 只在挂载时取一次：onRecommended 每次渲染都是新函数，进依赖会让它反复抢回推荐值。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [researchId])
  // 整理是后台活（Opus 一次调用，几分钟起步）。状态一律走 SSE，不许轮询
  // （web 契约：只有 RunPanel 豁免定时器），所以这里订阅本研究的事件流，
  // 只认 progress 的 polish 阶段与 export_failed 两种。
  const polish = async () => {
    setBusy('polished'); setWaited(null)
    let source: EventSource | null = null
    const stop = () => { source?.close(); source = null; setBusy(null) }
    try {
      const r = await fetch(`/api/researches/${encodeURIComponent(researchId)}/export`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kind: 'polished', template }),
      })
      const body = await r.json() as ApiEnvelope<{ status?: string }>
      if (!r.ok || !body.ok) throw new Error(body.error?.message ?? `HTTP ${r.status}`)
      source = new EventSource(`/api/researches/${encodeURIComponent(researchId)}/events`)
      source.addEventListener('progress', (event) => {
        const data = JSON.parse((event as MessageEvent<string>).data)?.data ?? {}
        if (data.stage !== 'polish') return
        setWaited(String(data.summary ?? ''))
        if (data.status === 'done') {
          void message.success('正式稿已整理完成'); onPolished(); onDone(); stop()
        }
      })
      source.addEventListener('export_failed', (event) => {
        const data = JSON.parse((event as MessageEvent<string>).data)?.data ?? {}
        if (data.kind !== 'polished') return
        void message.error(`整理失败：${data.error ?? '未知原因'}`); onDone(); stop()
      })
    } catch (e) { void message.error(e instanceof Error ? e.message : String(e)); stop() }
  }
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
  // url=null 的那条是失败登记（货 3），只当提示不给链接。
  const lastPolished = report.exports.filter((x) => x.kind === 'polished').at(-1)
  const options = (templates.length ? templates : [{ name: 'consulting', title: '调研报告（咨询体）' }])
    .map((t) => ({ value: t.name, label: t.title }))
  return <>
    <Button size="small" type="primary" loading={busy === 'polished'} onClick={() => void polish()}
      data-testid="export-polished">整理成正式稿</Button>
    <Select size="small" style={{ minWidth: 170 }} value={template} options={options}
      onChange={onTemplateChange} data-testid="polished-template" />
    {busy === 'polished' && <Typography.Text type="secondary" data-testid="polished-progress">
      {waited ?? '正在整理（Opus 一次调用通常要几分钟）'}</Typography.Text>}
    <Button size="small" loading={busy === 'excel'} onClick={() => void run('excel')} data-testid="export-excel">导出 Excel</Button>
    <Button size="small" loading={busy === 'feishu'} onClick={() => void run('feishu')} data-testid="export-feishu">推送飞书</Button>
    {lastPolished && (lastPolished.url
      ? <a href={lastPolished.url} target="_blank" rel="noreferrer">上次整理 {lastPolished.created_at}</a>
      : <Tag color="orange" data-testid="polished-failed">上次整理失败</Tag>)}
    {excel?.url && <a href={excel.url} target="_blank" rel="noreferrer">上次导出 {excel.created_at}</a>}
    {report.feishu.doc_url && <a href={report.feishu.doc_url} target="_blank" rel="noreferrer">飞书云文档</a>}
    {report.feishu.status && report.feishu.status !== 'pending' && <Tag>飞书 {report.feishu.status}</Tag>}
  </>
}
