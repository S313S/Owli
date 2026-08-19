import { Alert, Button, Card, Input, Space, Typography } from 'antd'
import { useRef, useState } from 'react'
import type { ApiEnvelope } from './types'

const { TextArea } = Input

const examples = [
  '调研茶叶领域社媒竞品账号',
  '帮我挖一下飞书主要竞品的优缺点，我们要决定下个季度补哪些能力',
]

export default function ResearchInputPage() {
  const [query, setQuery] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(false)
  const requestId = useRef('')

  async function submit() {
    if (!query.trim()) return
    setSubmitting(true)
    setError(false)
    try {
      requestId.current ||= `research-${crypto.randomUUID()}`
      const response = await fetch('/api/researches', {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Request-ID': requestId.current },
        body: JSON.stringify({ query }),
      })
      if (!response.ok) throw new Error('提交失败')
      const result = await response.json() as ApiEnvelope<{ research_id: string }>
      window.location.assign(`/researches/${encodeURIComponent(result.data.research_id)}`)
    } catch {
      setError(true)
      setSubmitting(false)
    }
  }

  return <main className="input-page">
    <section className="hero">
      <Typography.Title level={1}>今天想调研点什么？</Typography.Title>
      <Typography.Paragraph className="hero-lead">
        写清楚你要拿这份调研做什么决定，计划会更贴。提交后先出计划，你审核通过才开始跑。
      </Typography.Paragraph>
      <Card className="query-card">
        <TextArea value={query} onChange={(event) => setQuery(event.target.value)} autoSize={{ minRows: 6 }}
          placeholder={'例：帮我挖一下飞书主要竞品的优缺点，我们要决定下个季度补哪些能力\n例：调研茶叶领域社媒竞品账号'} />
        <Space wrap className="samples">
          <Typography.Text type="secondary">试试：</Typography.Text>
          {examples.map((example) => <Button key={example} shape="round" size="small" onClick={() => setQuery(example)}>{example}</Button>)}
        </Space>
        <div className="query-actions">
          <Typography.Text type="secondary">
            提交后会先做一次历史匹配，再生成调研计划；<b>计划需你审核通过才开始执行</b>，不会立刻消耗引擎额度。
          </Typography.Text>
          <Button type="primary" size="large" loading={submitting} disabled={!query.trim()} onClick={submit}>生成调研计划</Button>
        </div>
        {submitting && <Alert className="inline-state" type="info" showIcon message="正在匹配历史调研…（最多 3 秒，超时按无命中处理，不阻塞主流程）" />}
        {error && <Alert className="inline-state" type="error" showIcon
          message="没能提交需求：本地服务没有响应（127.0.0.1:8721）。你输入的内容已保留，确认 Owli 仍在运行后可直接重试"
          action={<Button size="small" onClick={submit}>重试</Button>} />}
      </Card>
    </section>
  </main>
}
