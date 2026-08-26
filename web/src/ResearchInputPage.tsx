import { Alert, Button, Card, Input, Space, Typography } from 'antd'
import { useEffect, useRef, useState } from 'react'
import ActionCardView from './ActionCardView'
import type { ActionCard, ApiEnvelope, NormalizedEvent, ServerAction } from './types'

const { TextArea } = Input

const examples = [
  '调研茶叶领域社媒竞品账号',
  '帮我挖一下飞书主要竞品的优缺点，我们要决定下个季度补哪些能力',
]

type CreateResearchData = {
  research_id: string
  recall_status: 'pending' | 'complete'
}

export default function ResearchInputPage() {
  const [query, setQuery] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const [cards, setCards] = useState<ActionCard[]>([])
  const [statusMessage, setStatusMessage] = useState('')
  const requestId = useRef('')
  const stream = useRef<EventSource | null>(null)
  const activeResearchId = useRef('')
  const pendingHistoryCards = useRef(new Set<string>())

  useEffect(() => {
    const researchId = new URLSearchParams(window.location.search).get('research_id') ?? ''
    if (!researchId) return () => stream.current?.close()
    activeResearchId.current = researchId
    setSubmitting(true)
    setStatusMessage('正在恢复历史候选检查…')
    void fetch(`/api/researches/${encodeURIComponent(researchId)}`).then(async (response) => {
      if (!response.ok) throw new Error('恢复失败')
      const result = await response.json() as ApiEnvelope<{ cards?: ActionCard[], status?: string }>
      const restored = (result.data.cards ?? []).filter((card) => card.card_type === 'HISTORY_REUSE')
      pendingHistoryCards.current = new Set(
        restored.filter((card) => card.status === 'pending').map((card) => card.card_id),
      )
      setCards(restored)
      setStatusMessage(restored.length ? '已恢复历史候选，请选择下一步' : '历史匹配仍在后台继续')
      if (result.data.status === 'awaiting_review' && pendingHistoryCards.current.size === 0) {
        window.location.assign(`/researches/${encodeURIComponent(researchId)}/plan`)
        return
      }
      openRecallStream(researchId)
    }).catch(() => setError('无法恢复这次历史匹配，请重新提交需求'))
    return () => stream.current?.close()
  }, [])

  function openRecallStream(researchId: string) {
    stream.current?.close()
    const source = new EventSource(`/api/researches/${encodeURIComponent(researchId)}/events`)
    stream.current = source
    const receive = (message: MessageEvent<string>) => {
      const event = JSON.parse(message.data) as NormalizedEvent
      const data = event.data ?? {}
      if (event.type === 'card_update' && data.card) {
        const card = data.card as ActionCard
        if (card.card_type === 'HISTORY_REUSE') {
          if (card.status === 'pending') pendingHistoryCards.current.add(card.card_id)
          else pendingHistoryCards.current.delete(card.card_id)
        }
        setCards((current) => {
          const position = current.findIndex((item) => item.card_id === card.card_id)
          if (position < 0) return [...current, card]
          return current.map((item, index) => index === position ? card : item)
        })
        if (card.status === 'pending') setStatusMessage('发现可复用的历史调研，请选择下一步')
      }
      if (event.type === 'reuse_check_complete' && data.has_matches === false) {
        setStatusMessage('没有命中可复用历史，正在生成全新计划')
      }
      if (event.type === 'research_update' && data.status === 'awaiting_review') {
        if (pendingHistoryCards.current.size === 0) {
          window.location.assign(`/researches/${encodeURIComponent(researchId)}/plan`)
        } else {
          setStatusMessage('全新计划已准备好，你仍可选择复用历史或坚持新建')
        }
      }
      if (event.type === 'error') {
        setError(String(data.summary ?? '计划生成失败，请返回后重试'))
        setSubmitting(false)
      }
    }
    for (const type of ['card_update', 'reuse_check_complete', 'research_update', 'error']) {
      source.addEventListener(type, receive as EventListener)
    }
    source.onerror = () => setStatusMessage('连接已断开，正在重连；历史匹配仍在后台继续')
  }

  function markChoice(card: ActionCard, action: ServerAction) {
    pendingHistoryCards.current.clear()
    setCards((current) => current.map((item) => item.card_id === card.card_id
      ? { ...item, status: 'answered', result: { choice: action.value ?? action.id } }
      : item))
    setStatusMessage(action.value === 'reuse'
      ? '已选择复用历史计划，正在打开可编辑初稿'
      : '已选择全新开始，正在生成全新计划')
    const researchId = activeResearchId.current
    if (action.value === 'new' && researchId) {
      void fetch(`/api/researches/${encodeURIComponent(researchId)}/plan`).then((response) => {
        if (response.ok) window.location.assign(`/researches/${encodeURIComponent(researchId)}/plan`)
      })
    }
  }

  async function submit() {
    if (!query.trim()) return
    setSubmitting(true)
    setError('')
    setCards([])
    setStatusMessage('正在连接历史匹配…')
    try {
      requestId.current ||= `research-${crypto.randomUUID()}`
      const response = await fetch('/api/researches', {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Request-ID': requestId.current },
        body: JSON.stringify({ query }),
      })
      if (!response.ok) throw new Error('提交失败')
      const result = await response.json() as ApiEnvelope<CreateResearchData>
      activeResearchId.current = result.data.research_id
      window.history.replaceState(null, '', `/?research_id=${encodeURIComponent(result.data.research_id)}`)
      pendingHistoryCards.current.clear()
      openRecallStream(result.data.research_id)
      setStatusMessage('研究已创建，历史匹配在后台继续')
    } catch {
      setError('没能提交需求：本地服务没有响应（127.0.0.1:8721）。你输入的内容已保留，确认 Owli 仍在运行后可直接重试')
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
        {submitting && <Alert className="inline-state" type="info" showIcon
          message={statusMessage || '研究已创建，历史匹配在后台继续，不阻塞主流程'} />}
        {error && <Alert className="inline-state" type="error" showIcon message={error}
          action={<Button size="small" onClick={submit}>重试</Button>} />}
      </Card>
      {cards.length ? <Card className="history-candidates" title={`发现 ${cards.length} 条历史候选`}>
        <Typography.Paragraph type="secondary">先看候选，再决定复用已验证成果或生成全新计划。</Typography.Paragraph>
        <div className="history-candidate-list">
          {cards.map((card) => <ActionCardView key={card.card_id} card={card} onResolved={markChoice} />)}
        </div>
      </Card> : null}
    </section>
  </main>
}
