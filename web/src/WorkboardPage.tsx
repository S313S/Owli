import { Alert, Badge, Button, Card, Collapse, Empty, Progress, Skeleton, Space, Tag, Timeline, Typography } from 'antd'
import { useEffect } from 'react'
import ActionButtons from './ActionButtons'
import ActionCardView from './ActionCardView'
import HistoricalResearchView from './HistoricalResearchView'
import { useResearchStream } from './useResearchStream'

const statusColor: Record<string, string> = {
  done: 'success', running: 'processing', retrying: 'warning', failed: 'error',
  paused: 'warning', queued: 'default', stopped: 'error',
}

const agentPriority: Record<string, number> = {
  failed: 0, retrying: 1, running: 2, queued: 3, skipped: 4, done: 5,
}

export default function WorkboardPage({ researchId }: { researchId: string }) {
  const { snapshot, connection, loadError, retry } = useResearchStream(researchId)
  const pending = snapshot?.cards.filter((card) => card.status === 'pending').length ?? 0

  useEffect(() => {
    document.title = snapshot?.snapshot_source === 'store'
      ? 'Owli · 历史只读'
      : `${pending ? `(${pending}) ` : ''}Owli · 实时工作板`
  }, [pending, snapshot?.snapshot_source])

  if (!snapshot) return <main className="board-page">
    {loadError
      ? <Alert type="error" showIcon message="工作板加载失败：本地快照不可用。确认 Owli 仍在运行后重试。" action={<Button onClick={() => void retry()}>重试</Button>} />
      : <Card><Skeleton active paragraph={{ rows: 8 }} /><Typography.Text type="secondary">正在连接实时工作板…</Typography.Text></Card>}
  </main>

  if (snapshot.snapshot_source === 'store') {
    return <HistoricalResearchView snapshot={snapshot} />
  }

  const percent = snapshot.progress.total ? Math.round(snapshot.progress.done / snapshot.progress.total * 100) : 0
  const activeGoals = snapshot.goals.filter((goal) => goal.status === 'running').map((goal) => goal.id)
  const sortedCards = snapshot.cards.slice().sort((left, right) => Number(right.status === 'pending') - Number(left.status === 'pending'))

  return <main className="board-page">
    {connection !== 'connected' && <Alert className="connection-banner" type="info" showIcon
      message={connection === 'connecting' ? '连接中…' : '连接已断开，正在重连…已渲染的内容保持不变；调研任务照常在后台跑，重连后会把断开期间的事件补上'} />}
    <Card className="board-top">
      <div className="board-title">
        <Typography.Title level={3}>{snapshot.title}</Typography.Title>
        <Tag color={statusColor[snapshot.status]}>{snapshot.status_label}</Tag>
        <span className="board-spacer" />
        {snapshot.actions.map((action) => <ActionButtons key={action.id} actions={[action]} />)}
      </div>
      <div className="overall-progress">
        <b>{snapshot.progress.done} / {snapshot.progress.total} 个子目标完成</b>
        <Progress percent={percent} showInfo={false} />
        <Typography.Text type="secondary">{snapshot.progress.summary}</Typography.Text>
      </div>
      <div className="goal-pips">
        {snapshot.goals.map((goal, index) => <Tag key={goal.id} color={statusColor[goal.status]}>{index + 1} {goal.title}</Tag>)}
      </div>
    </Card>

    <div className="board-layout">
      <section className="goal-lanes">
        <Collapse defaultActiveKey={activeGoals} items={snapshot.goals.map((goal, index) => ({
          key: goal.id,
          label: <div className="lane-label"><b>{index + 1}. {goal.title}</b><span>{goal.summary}</span><Tag color={statusColor[goal.status]}>{goal.status}</Tag></div>,
          children: goal.agents.length ? <div className="agent-grid">{goal.agents.slice().sort((left, right) => (agentPriority[left.status] ?? 9) - (agentPriority[right.status] ?? 9)).map((agent) => <Card key={agent.id} size="small" className={`agent-card agent-${agent.status}`}>
            <div className="agent-title"><b>{agent.name}</b><Tag>{agent.engine}</Tag></div>
            <Typography.Paragraph type="secondary">{agent.activity}</Typography.Paragraph>
            {agent.status === 'retrying' && <Typography.Text type="warning">重跑第 {agent.retry_attempt ?? '—'} / {agent.retry_max ?? 10} 次</Typography.Text>}
            <Badge status={statusColor[agent.status] as 'success' | 'processing' | 'warning' | 'error' | 'default'} text={agent.status} />
          </Card>)}</div> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={goal.summary} />,
        }))} />
      </section>

      <aside className="board-rail">
        <Card title={<Space>需要你处理 <Badge count={pending} showZero /></Space>} className="todo-panel">
          {sortedCards.length
            ? sortedCards.map((card) => <ActionCardView key={card.card_id} card={card} />)
            : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前没有需要你处理的事项" />}
        </Card>
        <Card title="事件流" extra={<Typography.Text type="secondary">默认仅显示关键事件</Typography.Text>} className="event-panel">
          {snapshot.events.length ? <Timeline items={snapshot.events.map((event) => ({
            color: event.type === 'error' ? 'red' : event.type === 'artifact' ? 'green' : 'blue',
            children: <><Typography.Text>{String(event.data?.summary ?? event.data?.message ?? event.type)}</Typography.Text><br /><Typography.Text type="secondary">#{event.sequence}</Typography.Text></>,
          }))} /> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="等待新的关键事件" />}
        </Card>
      </aside>
    </div>
  </main>
}
