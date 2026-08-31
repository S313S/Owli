import { Steps, Tag } from 'antd'
import { lazy, Suspense } from 'react'
import { backendOrigin } from './origin'

const PlanEditorPage = lazy(() => import('./PlanEditorPage'))
const ReportPage = lazy(() => import('./ReportPage'))
const ResearchInputPage = lazy(() => import('./ResearchInputPage'))
const WorkboardPage = lazy(() => import('./WorkboardPage'))

const steps = [
  { title: '需求输入' }, { title: '历史候选' }, { title: '计划编辑' },
  { title: '实时工作板' }, { title: '报告' },
]

function Header({ current = 0 }: { current?: number }) {
  return <header className="app-header">
    <div className="brand"><span className="brand-mark">O</span> Owli <small>本地调研工作台 · {backendOrigin()}</small></div>
    <Steps className="header-steps" size="small" current={current} items={steps} />
    <Tag color="success">本地服务</Tag>
  </header>
}

export default function App() {
  const reportMatch = window.location.pathname.match(/^\/researches\/([^/]+)\/report$/)
  if (reportMatch) return <Suspense><Header current={4} /><ReportPage researchId={decodeURIComponent(reportMatch[1])} /></Suspense>
  const planMatch = window.location.pathname.match(/^\/researches\/([^/]+)\/plan$/)
  if (planMatch) return <Suspense><Header current={2} /><PlanEditorPage researchId={decodeURIComponent(planMatch[1])} /></Suspense>
  const match = window.location.pathname.match(/^\/researches\/([^/]+)$/)
  if (match) return <Suspense><Header current={3} /><WorkboardPage researchId={decodeURIComponent(match[1])} /></Suspense>
  return <Suspense><Header /><ResearchInputPage /></Suspense>
}
