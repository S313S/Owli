import { Alert, Badge, Button, Card, Space, Tag, Typography } from 'antd'
import { useEffect, useMemo, useState } from 'react'
import type { ActionCard, ServerAction } from './types'

const typeNames: Record<string, string> = {
  LOGIN_REPAIR: '🔑 补登录', AUTHORIZE: '🛡️ 授权', ENGINE_SWITCH_CONFIRM: '🔀 确认换引擎',
  EXTRA_QUOTA_CONFIRM: '💳 确认额外额度', QUESTION: '❓ 决策天平追问',
  ARTIFACT_OPEN: '📄 产物文件', INTERVENE: '🚦 干预点确认',
}

function useDeadline(deadline?: string | null) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!deadline) return
    const timer = window.setTimeout(() => setNow(Date.now()), 1000)
    return () => window.clearTimeout(timer)
  }, [deadline, now])
  if (!deadline) return null
  const seconds = Math.max(0, Math.floor((new Date(deadline).getTime() - now) / 1000))
  const minutes = Math.floor(seconds / 60)
  return `${String(minutes).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`
}

export default function ActionCardView({ card }: { card: ActionCard }) {
  const [sending, setSending] = useState(false)
  const [failure, setFailure] = useState('')
  const countdown = useDeadline(card.deadline)
  const defaultAction = useMemo(() => card.actions.find((action) => action.default), [card.actions])

  async function respond(action: ServerAction) {
    if (action.type === 'OPEN_URL' && typeof card.target?.url === 'string') {
      window.open(card.target.url, '_blank', 'noopener,noreferrer')
      return
    }
    setSending(true)
    setFailure('')
    try {
      const response = await fetch(`/api/cards/${encodeURIComponent(card.card_id)}/respond`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Request-ID': `card-${crypto.randomUUID()}` },
        body: JSON.stringify({ action: action.id || action.type, payload: { choice: action.value ?? action.id } }),
      })
      if (!response.ok) {
        const body = await response.json()
        throw new Error(body.error?.message ?? '卡片回答没有生效')
      }
      if (action.value === 'adjust' && card.research_id) {
        window.location.assign(`/researches/${encodeURIComponent(card.research_id)}/plan?runtime=1`)
      }
    } catch (error) {
      setFailure(`${String(error)}。卡片仍保留，可直接重试`)
    } finally {
      setSending(false)
    }
  }

  return <Card size="small" className={`action-card card-${card.card_type.toLowerCase()} ${card.status !== 'pending' ? 'card-resolved' : ''}`}>
    <div className="card-type"><b>{typeNames[card.card_type] ?? card.card_type}</b><Tag>{card.blocking}</Tag></div>
    <Typography.Title level={5}>{card.title}</Typography.Title>
    {card.body && <Typography.Paragraph type="secondary">{card.body}</Typography.Paragraph>}
    {card.target && Object.keys(card.target).length > 0 && <div className="card-target">{String(card.target.display_name ?? card.target.url ?? card.target.path ?? '动作对象')}</div>}
    {failure && <Alert type="error" showIcon message={failure} />}
    {card.status === 'pending' && <Space wrap className="card-actions">
      {card.actions.map((action) => <Button key={action.id || action.type} size="small" loading={sending}
        type={action.danger ? 'default' : 'primary'} danger={action.danger}
        onClick={() => void respond(action)}>{action.label || action.type}</Button>)}
    </Space>}
    {countdown !== null && card.status === 'pending' && <div className="card-deadline">倒计时 {countdown} · 超时后默认「{defaultAction?.label ?? '按正文说明处理'}」</div>}
    {card.status !== 'pending' && <Badge status="default" text="已处理 · 已归档到事件流" />}
  </Card>
}
