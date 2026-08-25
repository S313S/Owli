export type ServerAction = {
  id: string
  label: string
  href?: string
  method?: string
  danger?: boolean
  confirm?: string
  type?: 'OPEN_URL' | 'OPEN_FILE' | 'TEXT_INPUT' | 'CHOICE_2'
  value?: string
  default?: boolean
}

export type AgentState = {
  id: string
  name: string
  engine: string
  status: string
  activity: string
  retry_attempt?: number
  retry_max?: number
}

export type GoalState = {
  id: string
  title: string
  status: string
  summary: string
  agents: AgentState[]
}

export type ActionCard = {
  card_id: string
  card_type: string
  research_id: string
  goal_id?: string | null
  agent_id?: string | null
  title: string
  body?: string
  blocking: string
  status: string
  actions: ServerAction[]
  target?: Record<string, unknown>
  deadline?: string | null
  result?: Record<string, unknown> | null
  created_at?: string
  resolved_at?: string | null
}

export type Origin = Record<string, 'generated' | 'user' | 'reset'>

export type PlanAgent = {
  agent_id: string
  display_name: string
  task: string
  depends_on: string[]
  inputs: Array<Record<string, unknown>>
  engine: string
  model: string | null
  capability: {
    profile: string
    tools: string[]
    sources: string[]
    fs: { read: string[]; write: string[] }
    network: string
    shell: string
    justification?: string
  }
  prompt: { preamble_ref: string; body: string; assumptions_policy: string }
  output: { format: string; path: string; validators: string[] }
  extra_quota_credits: number | null
  origin: Origin
  status: string
}

export type PlanGoal = {
  goal_id: string
  title: string
  objective: string
  depends_on: string[]
  deliverable: { format: string; path: string; description: string }
  acceptance: string[]
  intervention: { on_complete: boolean; prompt: string }
  retry_policy: Record<string, string | number>
  on_upstream_failure: string
  agents: PlanAgent[]
  status: string
}

export type DecisionQuestion = {
  q_id: string
  question: string
  options: string[]
  input_type: 'single' | 'multi' | 'choice_2' | 'text'
  answer: string | string[] | null
  affects: string[]
  answered_at: string | null
}

export type ResearchPlan = {
  research_id: string
  plan_rev: number
  title: string
  research_question: string
  use_case: string
  status: string
  approved_at: string | null
  decision_balance: DecisionQuestion[]
  expert_panel: Record<string, unknown> | null
  goals: PlanGoal[]
  change_log: Array<Record<string, unknown>>
  baseline: { title: string; use_case: string; goals: PlanGoal[] }
  baseline_source: string
  created_at: string
  updated_at: string
}

export type LlmUsage = {
  input_tokens: number
  cached_input_tokens: number
  cache_creation_input_tokens: number
  cache_write_input_tokens: number
  output_tokens: number
  reasoning_output_tokens: number
  cost_usd: number
  calls: number
  costed_calls: number
}

export type ResearchSnapshot = {
  research_id: string
  title: string
  status: string
  status_label: string
  snapshot_source?: 'store'
  progress: { done: number; total: number; summary: string }
  usage: LlmUsage
  report_path?: string | null
  report_content?: string | null
  summary?: string | null
  summary_line?: string | null
  actions: ServerAction[]
  goals: GoalState[]
  chapters?: ChapterProgress[]
  missing?: HistoricalMissing[]
  cards: ActionCard[]
  events: NormalizedEvent[]
}

export type ChapterProgress = {
  research_id: string
  goal_id: string
  chapter_id: string
  status: string
  attempts: number
  engine?: string | null
  reason?: string | null
  engine_error?: string | null
  conclusion_error?: string | null
  actual_output_path?: string | null
  actual_count?: number | null
  extra?: { usage?: LlmUsage }
  updated_at: string
}

export type HistoricalMissing = {
  goal_id: string
  chapter_id: string
  status: 'missing' | 'deferred'
  reason?: string | null
  error?: string | null
}

export type NormalizedEvent = {
  sequence: number
  type: string
  occurred_at: string
  data?: Record<string, unknown>
  raw?: unknown
}

export type ApiError = { code: string; message: string; details?: unknown }
export type ApiEnvelope<T> = { ok: boolean; data: T; error: ApiError | null }
