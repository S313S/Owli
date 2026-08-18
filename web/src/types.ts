export type ServerAction = {
  id: string
  label: string
  href: string
  method: string
  danger?: boolean
  confirm?: string
  type?: 'OPEN_URL' | 'OPEN_FILE' | 'TEXT_INPUT' | 'CHOICE_2'
}

export type AgentState = {
  id: string
  name: string
  engine: string
  status: string
  activity: string
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
  title: string
  body?: string
  blocking: string
  status: string
  actions: ServerAction[]
}

export type ResearchSnapshot = {
  research_id: string
  title: string
  status: string
  status_label: string
  progress: { done: number; total: number; summary: string }
  actions: ServerAction[]
  goals: GoalState[]
  cards: ActionCard[]
  events: NormalizedEvent[]
}

export type NormalizedEvent = {
  sequence: number
  type: string
  occurred_at: string
  data?: Record<string, unknown>
  raw?: unknown
}

export type ApiEnvelope<T> = { ok: boolean; data: T; error: unknown }
