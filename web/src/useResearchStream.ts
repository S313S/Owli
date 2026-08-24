import { useCallback, useEffect, useState } from 'react'
import type { ApiEnvelope, NormalizedEvent, ResearchSnapshot } from './types'

const eventTypes = [
  'stream_connected', 'research_snapshot', 'research_update', 'progress',
  'agent_update', 'card_update', 'artifact', 'error', 'replay_truncated',
]

export function useResearchStream(researchId: string) {
  const [snapshot, setSnapshot] = useState<ResearchSnapshot | null>(null)
  const [connection, setConnection] = useState<'connecting' | 'connected' | 'reconnecting' | 'historical'>('connecting')
  const [loadError, setLoadError] = useState(false)

  const loadSnapshot = useCallback(async () => {
    const response = await fetch(`/api/researches/${encodeURIComponent(researchId)}`)
    if (!response.ok) throw new Error('快照加载失败')
    const result = await response.json() as ApiEnvelope<ResearchSnapshot>
    setSnapshot(result.data)
    setLoadError(false)
    return result.data
  }, [researchId])

  useEffect(() => {
    let disposed = false
    let source: EventSource | null = null

    const receive = (message: MessageEvent<string>) => {
      const event = JSON.parse(message.data) as NormalizedEvent
      sessionStorage.setItem(`owli:last:${researchId}`, String(event.sequence))
      if (event.type === 'replay_truncated') {
        void loadSnapshot().catch(() => setLoadError(true))
        return
      }
      setSnapshot((current) => reduceEvent(current, event))
    }
    void loadSnapshot().then((loaded) => {
      if (disposed) return
      if (loaded.snapshot_source === 'store') {
        setConnection('historical')
        return
      }
      const saved = sessionStorage.getItem(`owli:last:${researchId}`)
      const suffix = saved ? `?last_event_id=${encodeURIComponent(saved)}` : ''
      source = new EventSource(`/api/researches/${encodeURIComponent(researchId)}/events${suffix}`)
      eventTypes.forEach((type) => source?.addEventListener(type, receive as EventListener))
      source.onopen = () => setConnection('connected')
      source.onerror = () => setConnection('reconnecting')
    }).catch(() => setLoadError(true))
    return () => {
      disposed = true
      source?.close()
    }
  }, [loadSnapshot, researchId])

  return { snapshot, connection, loadError, retry: loadSnapshot }
}

function reduceEvent(current: ResearchSnapshot | null, event: NormalizedEvent): ResearchSnapshot | null {
  const data = event.data ?? {}
  if (event.type === 'research_snapshot') return data as unknown as ResearchSnapshot
  if (!current) return current
  const eventLine = { ...event }
  if (event.type === 'research_update') {
    return { ...current, ...data, events: [eventLine, ...current.events].slice(0, 200) } as ResearchSnapshot
  }
  if (event.type === 'progress') {
    return { ...current, progress: { ...current.progress, ...data }, events: [eventLine, ...current.events].slice(0, 200) } as ResearchSnapshot
  }
  if (event.type === 'card_update' && data.card) {
    const card = data.card as ResearchSnapshot['cards'][number]
    return { ...current, cards: [card, ...current.cards.filter((item) => item.card_id !== card.card_id)], events: [eventLine, ...current.events].slice(0, 200) }
  }
  return { ...current, events: [eventLine, ...current.events].slice(0, 200) }
}
