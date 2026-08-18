import { Steps, Tag } from 'antd'
import ResearchInputPage from './ResearchInputPage'
import WorkboardPage from './WorkboardPage'

const steps = [
  { title: '需求输入' }, { title: '历史候选' }, { title: '计划编辑' },
  { title: '实时工作板' }, { title: '报告' },
]

function Header({ board = false }: { board?: boolean }) {
  return <header className="app-header">
    <div className="brand"><span className="brand-mark">O</span> Owli <small>本地调研工作台 · 127.0.0.1:8721</small></div>
    <Steps className="header-steps" size="small" current={board ? 3 : 0} items={steps} />
    <Tag color="success">本地服务</Tag>
  </header>
}

export default function App() {
  const match = window.location.pathname.match(/^\/researches\/([^/]+)$/)
  if (match) return <><Header board /><WorkboardPage researchId={decodeURIComponent(match[1])} /></>
  return <><Header /><ResearchInputPage /></>
}
