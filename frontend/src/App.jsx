import React, { useState } from 'react'
import Batches from './pages/Batches.jsx'
import Curves from './pages/Curves.jsx'
import Contracts from './pages/Contracts.jsx'
import Relations from './pages/Relations.jsx'
import Publish from './pages/Publish.jsx'
import Coverage from './pages/Coverage.jsx'
import CoverageImpact from './pages/CoverageImpact.jsx'

const TABS = [
  ['batches', '发送批次'],
  ['curves', '批次曲线'],
  ['coverage', '分区覆盖'],
  ['coverage-impact', '更正与影响'],
  ['contracts', '协议契约'],
  ['relations', '关系与锚点'],
  ['publish', '发布与消费'],
]

export default function App() {
  const [tab, setTab] = useState('batches')
  return (
    <div className="app">
      <header>
        <h1>冷链批次温控信号数据服务</h1>
        <span className="subtitle">集中解析 · 版本化契约 · 质量发布 · 分区覆盖</span>
      </header>
      <nav>
        {TABS.map(([k, label]) => (
          <button key={k} className={tab === k ? 'active' : ''} onClick={() => setTab(k)}>
            {label}
          </button>
        ))}
      </nav>
      <main>
        {tab === 'batches' && <Batches />}
        {tab === 'curves' && <Curves />}
        {tab === 'coverage' && <Coverage />}
        {tab === 'coverage-impact' && <CoverageImpact />}
        {tab === 'contracts' && <Contracts />}
        {tab === 'relations' && <Relations />}
        {tab === 'publish' && <Publish />}
      </main>
    </div>
  )
}
