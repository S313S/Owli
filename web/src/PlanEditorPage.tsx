import {
  Alert, Button, Card, Checkbox, Collapse, Empty, Input, Modal, Radio,
  Select, Skeleton, Space, Tag, Typography,
} from 'antd'
import { useCallback, useEffect, useMemo, useState } from 'react'
import type {
  ApiEnvelope, DecisionQuestion, PlanAgent, PlanEntity, PlanGoal, ResearchPlan,
} from './types'

const { TextArea } = Input
const PENDING_ENTITY_PATTERN = /待定实体\d+/g

type EditFailure = { message: string; details: unknown; conflict?: boolean }

function requestId(prefix: string) {
  return `${prefix}-${crypto.randomUUID()}`
}

function planFromResponse(value: ResearchPlan & { lint?: unknown }): ResearchPlan {
  const { lint: _lint, ...plan } = value
  return plan
}

function collectPendingEntityPlaceholders(plan: ResearchPlan) {
  const activePlan = JSON.stringify({
    decision_balance: plan.decision_balance,
    goals: plan.goals,
  })
  return [...new Set(activePlan.match(PENDING_ENTITY_PATTERN) ?? [])]
    .sort((left, right) => Number(left.slice(4)) - Number(right.slice(4)))
}

function replacePendingEntityText(value: string, mapping: Record<string, string>) {
  let result = value
  const longestPlaceholderFirst = Object.entries(mapping)
    .sort(([left], [right]) => right.length - left.length)
  for (const [placeholder, entity] of longestPlaceholderFirst) {
    result = result.replaceAll(placeholder, entity)
  }
  return result
}

function applyPendingEntityMapping(
  plan: ResearchPlan,
  mapping: Record<string, string>,
) {
  const next = structuredClone(plan)
  for (const question of next.decision_balance) {
    question.question = replacePendingEntityText(question.question, mapping)
    question.options = question.options.map((value) => replacePendingEntityText(value, mapping))
    if (typeof question.answer === 'string') {
      question.answer = replacePendingEntityText(question.answer, mapping)
    } else if (Array.isArray(question.answer)) {
      question.answer = question.answer.map((value) => replacePendingEntityText(value, mapping))
    }
  }
  for (const goal of next.goals) {
    goal.title = replacePendingEntityText(goal.title, mapping)
    goal.objective = replacePendingEntityText(goal.objective, mapping)
    goal.deliverable.description = replacePendingEntityText(goal.deliverable.description, mapping)
    goal.acceptance = goal.acceptance.map((value) => replacePendingEntityText(value, mapping))
    goal.intervention.prompt = replacePendingEntityText(goal.intervention.prompt, mapping)
    for (const agent of goal.agents) {
      if (agent.entity !== null) agent.entity = replacePendingEntityText(agent.entity, mapping)
      agent.task = replacePendingEntityText(agent.task, mapping)
      agent.prompt.body = replacePendingEntityText(agent.prompt.body, mapping)
      if (agent.chapter) {
        agent.chapter.opening.task = replacePendingEntityText(agent.chapter.opening.task, mapping)
        agent.chapter.opening.acceptance = agent.chapter.opening.acceptance
          .map((value) => replacePendingEntityText(value, mapping))
        agent.chapter.closing.entities = agent.chapter.closing.entities
          .map((value) => replacePendingEntityText(value, mapping))
      }
    }
  }
  return next
}

function changedCount(agent: PlanAgent) {
  return Object.entries(agent.origin).filter(([key, value]) => key !== '_node' && value === 'user').length
}

function canReset(agent: PlanAgent) {
  return Object.values(agent.origin).some((value) => value !== 'generated')
}

function OriginTag({ agent, field }: { agent: PlanAgent; field: string }) {
  return agent.origin[field] === 'user' ? <Tag color="gold">已自定义</Tag> : null
}

function ReadonlyReason({ children }: { children: string }) {
  return <Tag className="readonly-reason">{children}</Tag>
}

function fieldDetails(details: unknown) {
  if (!details) return ''
  if (Array.isArray(details)) {
    return details.map((item) => typeof item === 'string' ? item : JSON.stringify(item)).join('；')
  }
  return String(details)
}

export default function PlanEditorPage({ researchId }: { researchId: string }) {
  const [plan, setPlan] = useState<ResearchPlan | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [failure, setFailure] = useState<EditFailure | null>(null)
  const [warnings, setWarnings] = useState<string[]>([])
  const [pendingEntityValues, setPendingEntityValues] = useState<Record<string, string>>({})

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const response = await fetch(`/api/researches/${encodeURIComponent(researchId)}/plan`)
      const body = await response.json() as ApiEnvelope<ResearchPlan>
      if (!response.ok || !body.ok) throw new Error(body.error?.message ?? '计划读取失败')
      setPlan(body.data)
      setPendingEntityValues({})
      setFailure(null)
    } catch (error) {
      setFailure({ message: `计划加载失败：${String(error)}。确认 Owli 仍在运行后重试`, details: null })
    } finally {
      setLoading(false)
    }
  }, [researchId])

  useEffect(() => { void load() }, [load])

  const save = useCallback(async (next: ResearchPlan) => {
    setSaving(true)
    setFailure(null)
    try {
      const response = await fetch(`/api/researches/${encodeURIComponent(researchId)}/plan`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(next),
      })
      const body = await response.json() as ApiEnvelope<ResearchPlan & { lint?: { warnings: string[] } }>
      if (!response.ok || !body.ok) {
        setFailure({ message: body.error?.message ?? '计划保存失败', details: body.error?.details, conflict: response.status === 409 })
        return
      }
      setPlan(planFromResponse(body.data))
      setWarnings(body.data.lint?.warnings ?? [])
    } catch (error) {
      setFailure({ message: `计划保存失败：${String(error)}。修改尚未生效，请重试`, details: null })
    } finally {
      setSaving(false)
    }
  }, [researchId])

  const resetScope = useCallback(async (scope: 'plan' | 'agent', targetId?: string) => {
    const endpoint = scope === 'agent' ? 'reset-agent' : 'reset'
    const payload = scope === 'agent' ? { agent_id: targetId } : { scope: 'plan' }
    const response = await fetch(`/api/researches/${encodeURIComponent(researchId)}/plan/${endpoint}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Request-ID': requestId('reset') },
      body: JSON.stringify(payload),
    })
    const body = await response.json() as ApiEnvelope<ResearchPlan>
    if (!response.ok || !body.ok) {
      setFailure({ message: body.error?.message ?? '恢复初始化失败', details: body.error?.details, conflict: response.status === 409 })
      return
    }
    setPlan(body.data)
    setWarnings([])
    setFailure(null)
  }, [researchId])

  const confirmReset = useCallback((scope: 'plan' | 'agent', targetId?: string) => {
    const name = scope === 'plan' ? '整份计划' : targetId
    Modal.confirm({
      title: `恢复「${name}」到初稿？`,
      content: '将丢弃你对这张卡片的全部修改，且不可撤销。决策天平答案不会被清除。',
      okText: '恢复初始化', cancelText: '取消',
      onOk: () => resetScope(scope, targetId),
    })
  }, [resetScope])

  const approvePlan = useCallback(async () => {
    const response = await fetch(`/api/researches/${encodeURIComponent(researchId)}/plan/approve`, {
      method: 'POST', headers: { 'X-Request-ID': requestId('approve') },
    })
    const body = await response.json() as ApiEnvelope<{ status: string; approved_at: string; plan_rev: number }>
    if (!response.ok || !body.ok) {
      setFailure({ message: body.error?.message ?? '批准失败', details: body.error?.details, conflict: response.status === 409 })
      return
    }
    setPlan((current) => current ? {
      ...current, status: body.data.status, approved_at: body.data.approved_at, plan_rev: body.data.plan_rev,
    } : current)
    setFailure(null)
  }, [researchId])

  const updateQuestion = useCallback((questionIndex: number, answer: string | string[]) => {
    if (!plan) return
    const next = structuredClone(plan)
    next.decision_balance[questionIndex].answer = answer
    next.decision_balance[questionIndex].answered_at = new Date().toISOString()
    void save(next)
  }, [plan, save])

  const unanswered = useMemo(
    () => plan?.decision_balance.filter((item) => item.answer === null || item.answer === '' || (Array.isArray(item.answer) && item.answer.length === 0)).length ?? 0,
    [plan],
  )
  const pendingEntityPlaceholders = useMemo(
    () => plan ? collectPendingEntityPlaceholders(plan) : [],
    [plan],
  )
  const entityMappingReady = pendingEntityPlaceholders.length > 0
    && pendingEntityPlaceholders.every((placeholder) => pendingEntityValues[placeholder]?.trim())
  const applyEntityMapping = () => {
    if (!plan || !entityMappingReady) return
    const mapping = Object.fromEntries(
      pendingEntityPlaceholders.map((placeholder) => [
        placeholder,
        pendingEntityValues[placeholder].trim(),
      ]),
    )
    void save(applyPendingEntityMapping(plan, mapping))
  }
  const approved = Boolean(plan?.approved_at)
  const runtimeEdit = new URLSearchParams(window.location.search).get('runtime') === '1'
  const frozen = approved && !runtimeEdit
  const reusedSource = plan?.baseline_source.startsWith('reused:')
    ? plan.baseline_source.slice('reused:'.length)
    : null
  const planModified = useMemo(() => {
    if (!plan) return false
    return plan.title !== plan.baseline.title
      || plan.use_case !== plan.baseline.use_case
      || plan.goals.some((goal) => goal.agents.some(canReset))
      || plan.goals.map((goal) => goal.goal_id).join(',') !== plan.baseline.goals.map((goal) => goal.goal_id).join(',')
  }, [plan])

  if (loading && !plan) return <main className="plan-page">
    <Card><Skeleton active paragraph={{ rows: 10 }} /><Typography.Text type="secondary">正在制订计划…goal 骨架会逐个填充</Typography.Text></Card>
  </main>

  if (!plan) return <main className="plan-page">
    <Alert type="error" showIcon message={failure?.message ?? '计划暂不可用'} action={<Button onClick={() => void load()}>重试</Button>} />
  </main>

  // §ENT-1 货 5：改实体卡走的还是 PUT /plan（editing 把 entities 记进调整日志），
  // 与 goal / agent 编辑同一条保存路径：defaultValue + onBlur，焦点离开即保存。
  const updateEntity = (entityIndex: number, mutate: (entity: PlanEntity) => void) => {
    const next = structuredClone(plan)
    if (!next.entities) return
    mutate(next.entities[entityIndex])
    void save(next)
  }

  const updateGoal = (goalIndex: number, mutate: (goal: PlanGoal) => void) => {
    const next = structuredClone(plan)
    mutate(next.goals[goalIndex])
    void save(next)
  }

  const updateAgent = (goalIndex: number, agentIndex: number, mutate: (agent: PlanAgent) => void) => {
    const next = structuredClone(plan)
    mutate(next.goals[goalIndex].agents[agentIndex])
    void save(next)
  }

  const detailText = `${failure?.message ?? ''} ${fieldDetails(failure?.details)}`

  return <main className="plan-page">
    {frozen && <Alert className="plan-frozen" type="success" showIcon message="计划已冻结"
      description={`计划已冻结为执行基线（批准于 ${plan.approved_at}）。此后的改动请走工作板上的干预点，会记入调整日志`} />}
    {runtimeEdit && approved && <Alert className="plan-frozen" type="warning" showIcon message="运行期调整"
      description="只修改后续 goal；保存会写入变更日志与 feedback，并丢弃已完成阶段的旧产物。完成后返回工作板点击“继续”。" />}
    {failure && <Alert className="plan-error" type="error" showIcon message={failure.message}
      description={fieldDetails(failure.details)}
      action={failure.conflict ? <Button onClick={() => void load()}>重新加载</Button> : undefined} />}
    {warnings.length > 0 && <Alert className="plan-error" type="warning" showIcon
      message="计划已保存，但有质量提醒" description={warnings.join('；')} />}

    <div className="plan-layout">
      <section className="plan-main">
        {reusedSource && pendingEntityPlaceholders.length > 0 && <Card
          className="question-queue"
          title={<Space>复用实体映射 <Tag color="warning">批准前必填</Tag></Space>}
        >
          <Typography.Paragraph type="secondary">
            一次填入当前题目的真实实体，系统会同步替换追问、goal 文案、Agent 任务、采集实体和章节派生字段。
          </Typography.Paragraph>
          <Space direction="vertical" style={{ width: '100%' }}>
            {pendingEntityPlaceholders.map((placeholder) => <Input
              key={placeholder}
              addonBefore={placeholder}
              placeholder="输入真实实体"
              disabled={frozen || saving}
              value={pendingEntityValues[placeholder] ?? ''}
              onChange={(event) => setPendingEntityValues((current) => ({
                ...current,
                [placeholder]: event.target.value,
              }))}
            />)}
            <Button
              type="primary"
              disabled={frozen || saving || !entityMappingReady}
              onClick={applyEntityMapping}
            >应用实体映射并保存</Button>
          </Space>
        </Card>}
        {(plan.entities?.length ?? 0) > 0 && <Card
          className="question-queue plan-entities"
          title={<Space>研究对象 <Tag color="blue">{plan.entities!.length} 个</Tag></Space>}
        >
          <Typography.Paragraph type="secondary">
            采集时国内源按中文名搜、海外源按英文名搜，每个对象每个源最多两个查询词。
            名字写错就搜不到人在讨论什么——起跑前请确认这里说的是不是你要研究的那个产品。
          </Typography.Paragraph>
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            {plan.entities!.map((entity, entityIndex) => <div
              key={entity.id} className="plan-entity-card" data-entity-id={entity.id}
            >
              <Space size={8} wrap>
                <Tag>{entity.id}</Tag>
                <Input
                  key={`canonical-${entity.canonical}`} addonBefore="正式名"
                  disabled={frozen || saving} defaultValue={entity.canonical}
                  data-testid={`entity-canonical-${entity.id}`}
                  onBlur={(event) => updateEntity(entityIndex, (item) => {
                    item.canonical = event.target.value.trim() || item.canonical
                  })}
                />
              </Space>
              <Space size={8} wrap style={{ marginTop: 8 }}>
                <Input
                  key={`zh-${entity.names.zh ?? ''}`} addonBefore="中文名"
                  disabled={frozen || saving} defaultValue={entity.names.zh ?? ''}
                  data-testid={`entity-zh-${entity.id}`}
                  onBlur={(event) => updateEntity(entityIndex, (item) => {
                    item.names.zh = event.target.value.trim() || null
                  })}
                />
                <Input
                  key={`en-${entity.names.en ?? ''}`} addonBefore="英文名"
                  disabled={frozen || saving} defaultValue={entity.names.en ?? ''}
                  data-testid={`entity-en-${entity.id}`}
                  onBlur={(event) => updateEntity(entityIndex, (item) => {
                    item.names.en = event.target.value.trim() || null
                  })}
                />
              </Space>
              <Input
                key={`aliases-${entity.names.aliases.join(',')}`}
                addonBefore="别名" style={{ marginTop: 8 }}
                placeholder="用逗号分隔，例如：豆包大模型, Doubao AI"
                disabled={frozen || saving} defaultValue={entity.names.aliases.join(', ')}
                data-testid={`entity-aliases-${entity.id}`}
                onBlur={(event) => updateEntity(entityIndex, (item) => {
                  item.names.aliases = [...new Set(event.target.value
                    .split(/[,，]/).map((value) => value.trim()).filter(Boolean))]
                })}
              />
              <Checkbox
                style={{ marginTop: 8 }} disabled={frozen || saving}
                checked={entity.same_product}
                data-testid={`entity-same-${entity.id}`}
                onChange={(event) => updateEntity(entityIndex, (item) => {
                  item.same_product = event.target.checked
                })}
              >中外名字是同一个产品（取消勾选表示像抖音／TikTok 那样是两个产品，报告里只并列不交叉）</Checkbox>
              <TextArea
                key={`note-${entity.note}`} autoSize style={{ marginTop: 8 }}
                placeholder={entity.same_product ? '一句话说清这是什么产品' : '必填：中外两个产品差在哪'}
                disabled={frozen || saving} defaultValue={entity.note}
                data-testid={`entity-note-${entity.id}`}
                onBlur={(event) => updateEntity(entityIndex, (item) => {
                  item.note = event.target.value
                })}
              />
            </div>)}
          </Space>
        </Card>}
        <Card className="question-queue" title={<Space>决策天平追问 <Tag color={unanswered ? 'warning' : 'success'}>{unanswered ? `${unanswered} 个待回答` : '已全部回答'}</Tag></Space>}>
          {plan.decision_balance.length ? plan.decision_balance.map((question, index) =>
            <QuestionView key={question.q_id} question={question} disabled={approved || saving}
              onChange={(answer) => updateQuestion(index, answer)} />)
            : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="本次计划没有补充追问" />}
        </Card>

        <div className="plan-heading">
          <div>
            <Typography.Title level={3}>{plan.title}</Typography.Title>
            <Space size={8}>
              <Typography.Text type="secondary">plan_rev {plan.plan_rev} · {plan.goals.length} 个 goal</Typography.Text>
              {reusedSource && <Tag color="blue">沿用自历史计划 · {reusedSource}</Tag>}
            </Space>
          </div>
          <Button disabled={frozen || !planModified} onClick={() => confirmReset('plan')}>全部恢复</Button>
        </div>

        <Collapse defaultActiveKey={plan.goals[0]?.goal_id} className="plan-goals" items={plan.goals.map((goal, goalIndex) => {
          const modifiedCards = goal.agents.filter((agent) => changedCount(agent) > 0).length
          return {
            key: goal.goal_id,
            label: <div className="plan-goal-label"><b>{goalIndex + 1}. {goal.title}</b><span>{goal.objective}</span>{modifiedCards > 0 && <Tag color="gold">{modifiedCards} 张卡片被改过</Tag>}</div>,
            children: <>
              <div className="deliverable-block">
                <label>产物</label>
                <Select disabled={frozen || saving} value={goal.deliverable.format} options={['table', 'markdown', 'excel', 'json'].map((value) => ({ value }))}
                  onChange={(value) => updateGoal(goalIndex, (item) => { item.deliverable.format = value })} />
                <Input key={goal.deliverable.path} disabled={frozen || saving} defaultValue={goal.deliverable.path}
                  onBlur={(event) => updateGoal(goalIndex, (item) => { item.deliverable.path = event.target.value })} />
                <Input key={goal.deliverable.description} disabled={frozen || saving} defaultValue={goal.deliverable.description}
                  onBlur={(event) => updateGoal(goalIndex, (item) => { item.deliverable.description = event.target.value })} />
                <label>验收标准</label>
                <TextArea key={goal.acceptance.join('\n')} disabled={frozen || saving} autoSize defaultValue={goal.acceptance.join('\n')}
                  onBlur={(event) => updateGoal(goalIndex, (item) => { item.acceptance = event.target.value.split('\n').filter(Boolean) })} />
              </div>
              <div className="plan-agent-list">{goal.agents.map((agent, agentIndex) =>
                <AgentEditor key={agent.agent_id} agent={agent} goal={goal} disabled={frozen || saving}
                  inlineError={detailText.includes(agent.agent_id) ? detailText : ''}
                  onChange={(mutate) => updateAgent(goalIndex, agentIndex, mutate)}
                  onReset={() => confirmReset('agent', agent.agent_id)} />)}</div>
            </>,
          }
        })} />
      </section>

      <aside className="plan-rail">
        <Card title="计划闸门">
          <Typography.Paragraph>确认追问答案、阶段产物和 Agent 卡片后再批准。批准后编辑器切为只读。</Typography.Paragraph>
          {!approved && <Button block type="primary" size="large" loading={saving} disabled={unanswered > 0} onClick={() => void approvePlan()}>批准并开始执行</Button>}
          {runtimeEdit && <Button block type="primary" size="large" href={`/researches/${encodeURIComponent(researchId)}`}>返回工作板继续</Button>}
          {unanswered > 0 && <Typography.Text type="danger">还有 {unanswered} 个追问未回答，所以按钮不可用</Typography.Text>}
          {frozen && <Typography.Text type="secondary">计划已冻结；运行期调整请从工作板干预卡进入</Typography.Text>}
        </Card>
        <Card title="交叉审计"><Typography.Paragraph type="secondary">本次为单方案直出，不渲染对比视图。</Typography.Paragraph></Card>
      </aside>
    </div>
  </main>
}

function QuestionView({
  question, disabled, onChange,
}: {
  question: DecisionQuestion
  disabled: boolean
  onChange: (answer: string | string[]) => void
}) {
  return <div className="question-item">
    <div><b>{question.question}</b><Tag color="blue">将作为报告内注释</Tag></div>
    {question.input_type === 'multi'
      ? <Checkbox.Group disabled={disabled} options={question.options} value={Array.isArray(question.answer) ? question.answer : []} onChange={(value) => onChange(value as string[])} />
      : question.input_type === 'text'
        ? <Input key={String(question.answer)} disabled={disabled} defaultValue={typeof question.answer === 'string' ? question.answer : ''} onBlur={(event) => onChange(event.target.value)} />
        : <Radio.Group disabled={disabled} options={question.options} value={question.answer} onChange={(event) => onChange(event.target.value)} />}
    <Typography.Text type="secondary">影响：{question.affects.join('、') || '整份报告'}</Typography.Text>
  </div>
}

function AgentEditor({
  agent, goal, disabled, inlineError, onChange, onReset,
}: {
  agent: PlanAgent
  goal: PlanGoal
  disabled: boolean
  inlineError: string
  onChange: (mutate: (agent: PlanAgent) => void) => void
  onReset: () => void
}) {
  const count = changedCount(agent)
  const dependencyOptions = goal.agents
    .filter((item) => item.agent_id !== agent.agent_id)
    .map((item) => ({ value: item.agent_id, label: item.display_name }))
  const fieldClass = (field: string) => agent.origin[field] === 'user' ? 'editable-field customized' : 'editable-field'

  return <Card className={`plan-agent-card ${agent.origin._node === 'user' ? 'user-node' : ''}`}
    title={<Space><b>{agent.display_name}</b>{count > 0 && <Tag color="gold">已修改 {count} 项</Tag>}{agent.origin._node === 'user' && <Tag color="cyan">用户新增</Tag>}</Space>}
    extra={<Button size="small" disabled={disabled || !canReset(agent)} onClick={onReset}>恢复初始化</Button>}>
    {inlineError && <Alert className="agent-inline-error" type="error" showIcon message="这项修改未保存" description={inlineError} />}
    <div className="agent-fields">
      <label>名称 <OriginTag agent={agent} field="display_name" /></label>
      <Input key={agent.display_name} className={fieldClass('display_name')} disabled={disabled} defaultValue={agent.display_name}
        onBlur={(event) => event.target.value !== agent.display_name && onChange((item) => { item.display_name = event.target.value })} />

      <label>任务 <OriginTag agent={agent} field="task" /></label>
      <TextArea key={agent.task} className={fieldClass('task')} disabled={disabled} autoSize defaultValue={agent.task}
        onBlur={(event) => event.target.value !== agent.task && onChange((item) => { item.task = event.target.value })} />

      <label>前置任务 <OriginTag agent={agent} field="depends_on" /></label>
      <Select className={fieldClass('depends_on')} mode="multiple" disabled={disabled} value={agent.depends_on} options={dependencyOptions}
        onChange={(value) => onChange((item) => { item.depends_on = value })} />

      <label>能力与权限 <OriginTag agent={agent} field="capability" /></label>
      <Space wrap className={fieldClass('capability')}>
        <Select disabled={disabled} value={agent.capability.profile} options={['readonly-analyst', 'web-collector', 'sandboxed-runner', 'report-writer', 'custom'].map((value) => ({ value }))}
          onChange={(value) => onChange((item) => { item.capability.profile = value })} />
        <Select disabled={disabled} value={agent.capability.network} options={['none', 'sources_only', 'open'].map((value) => ({ value, label: `网络：${value}` }))}
          onChange={(value) => onChange((item) => { item.capability.network = value })} />
        <Select disabled={disabled} value={agent.capability.shell} options={['none', 'readonly', 'workspace'].map((value) => ({ value, label: `Shell：${value}` }))}
          onChange={(value) => onChange((item) => { item.capability.shell = value })} />
        {/* §CMT-1 货 5：只有采集卡才谈得上评论二跳 */}
        {agent.capability.profile === 'web-collector' && <Select disabled={disabled}
          data-testid="capability-comments"
          value={agent.capability.comments ?? 'on'}
          options={['on', 'off'].map((value) => ({ value, label: `评论二跳：${value === 'on' ? '开' : '关'}` }))}
          onChange={(value) => onChange((item) => { item.capability.comments = value })} />}
      </Space>

      <label>引擎 <OriginTag agent={agent} field="engine" /></label>
      <Select className={fieldClass('engine')} disabled={disabled} value={agent.engine}
        options={['claude', 'codex'].map((value) => ({ value }))}
        onChange={(value) => onChange((item) => { item.engine = value })} />

      <label>额外额度</label>
      <div className="readonly-field"><Input disabled value={agent.extra_quota_credits ?? '不使用'} /><ReadonlyReason>V1.0 暂不生效</ReadonlyReason></div>

      <label>Prompt <OriginTag agent={agent} field="prompt.body" /></label>
      <TextArea key={agent.prompt.body} className={fieldClass('prompt.body')} disabled={disabled} autoSize defaultValue={agent.prompt.body}
        onBlur={(event) => event.target.value !== agent.prompt.body && onChange((item) => { item.prompt.body = event.target.value })} />

      <label>公共前缀</label>
      <div className="readonly-field"><Input disabled value={agent.prompt.preamble_ref} /><ReadonlyReason>公共前缀 common/v1 不可编辑</ReadonlyReason></div>

      <label>Agent 产物 <OriginTag agent={agent} field="output" /></label>
      <Input key={agent.output.path} className={fieldClass('output')} disabled={disabled} defaultValue={agent.output.path}
        onBlur={(event) => event.target.value !== agent.output.path && onChange((item) => { item.output.path = event.target.value })} />

      <label>重试策略</label>
      <div className="readonly-field"><Typography.Text type="secondary">每轮 {String(goal.retry_policy.max_attempts_per_round)} 次 · 最多 {String(goal.retry_policy.max_rounds)} 轮 · {String(goal.retry_policy.goal_deadline_hours)} 小时总闸</Typography.Text><ReadonlyReason>执行策略，V1.0 只读</ReadonlyReason></div>
    </div>
  </Card>
}
