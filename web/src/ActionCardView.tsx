import { Badge, Button, Card, Space, Tag, Typography } from 'antd'
import type { ActionCard } from './types'

const typeNames: Record<string, string> = {
  LOGIN_REPAIR: '🔑 补登录', AUTHORIZE: '🛡️ 授权', ENGINE_SWITCH_CONFIRM: '🔀 确认换引擎',
  EXTRA_QUOTA_CONFIRM: '💳 确认额外额度', QUESTION: '❓ 决策天平追问',
  ARTIFACT_OPEN: '📄 产物文件', INTERVENE: '🚦 干预点确认',
}

export default function ActionCardView({ card }: { card: ActionCard }) {
  async function respond(href: string, method: string) {
    const response = await fetch(href, { method })
    if (!response.ok) return
  }

  return <Card size="small" className={`action-card card-${card.card_type.toLowerCase()}`}>
    <div className="card-type"><b>{typeNames[card.card_type] ?? card.card_type}</b><Tag>{card.blocking}</Tag></div>
    <Typography.Title level={5}>{card.title}</Typography.Title>
    {card.body && <Typography.Paragraph type="secondary">{card.body}</Typography.Paragraph>}
    <Space wrap className="card-actions">
      {card.actions.map((action) => <Button key={action.id} size="small" type={action.danger ? 'default' : 'primary'} danger={action.danger}
        onClick={() => void respond(action.href, action.method)}>{action.label}</Button>)}
    </Space>
    {card.status !== 'pending' && <Badge status="default" text="已处理 · 已归档到事件流" />}
  </Card>
}
