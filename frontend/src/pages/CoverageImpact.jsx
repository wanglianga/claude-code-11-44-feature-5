import React, { useEffect, useState } from 'react'
import { api } from '../api.js'
import { CAT_LABELS } from '../components/ZoneMap.jsx'

const HHMM = (s) => (s || '').slice(11, 16)

export default function CoverageImpact() {
  const [vehicle] = useState('VH-01')
  const [corrections, setCorrections] = useState([])
  const [runs, setRuns] = useState([])
  const [exports_, setExports] = useState([])
  const [inputs, setInputs] = useState(null)
  const [error, setError] = useState('')
  const [msg, setMsg] = useState('')
  const [withdraw, setWithdraw] = useState({ group: 'CAL-B', time: '2026-11-15T10:30:00+00:00', reason: '校准批次复核不合格' })
  const [impact, setImpact] = useState(null)
  const [impactQ, setImpactQ] = useState({ kind: 'validation', key: 'VAL-002' })
  const [cmp, setCmp] = useState({ a: '', b: '' })
  const [cmpResult, setCmpResult] = useState(null)
  const [expDetail, setExpDetail] = useState(null)
  const [correctText, setCorrectText] = useState('')

  const load = () => {
    api.get(`/api/coverage/corrections?vehicle_id=${vehicle}`).then((d) => setCorrections(d.corrections)).catch((e) => setError(e.message))
    api.get(`/api/coverage/runs?vehicle_id=${vehicle}`).then((d) => setRuns(d.runs)).catch((e) => setError(e.message))
    api.get('/api/coverage/exports').then((d) => setExports(d.exports)).catch((e) => setError(e.message))
    api.get(`/api/coverage/inputs?vehicle_id=${vehicle}`).then(setInputs).catch((e) => setError(e.message))
  }
  useEffect(load, [vehicle])

  const act = async (fn, text) => {
    setError(''); setMsg('')
    try {
      const r = await fn()
      setMsg(text || '已完成')
      load()
      return r
    } catch (e) { setError(e.message) }
  }

  const doWithdrawGroup = () => act(async () => {
    const r = await api.post('/api/coverage/validations/withdraw-group', {
      condition_group: withdraw.group, withdrawn_from: withdraw.time, reason: withdraw.reason,
    })
    return `已批量撤回 ${r.withdrawn.join('、')}（${r.withdrawn_from} 起），影响 ${r.impacts.length} 项验证的引用运输`
  })

  const doQueryImpact = () => act(async () => {
    const q = impactQ.kind === 'layout'
      ? `/api/coverage/impact?kind=layout&vehicle_id=${vehicle}`
      : `/api/coverage/impact?kind=${impactQ.kind}&key=${encodeURIComponent(impactQ.key)}&vehicle_id=${vehicle}`
    setImpact(await api.get(q))
  })

  const doCompare = () => act(async () => {
    setCmpResult(await api.get(`/api/coverage/compare?run_a=${cmp.a}&run_b=${cmp.b}`))
  })

  const doExport = (runId) => act(async () => {
    await api.post(`/api/coverage/runs/${runId}/export`, { label: `运行${runId}快照` })
  }, '已导出')

  const doCorrect = () => act(async () => {
    const { key, payload } = JSON.parse(correctText)
    const r = await api.post(`/api/coverage/placements/${key}/correct`, payload)
    return `清单 ${key} 已更正为版本 ${r.version}，影响 ${r.impact.affected_runs.length} 次运行`
  })

  return (
    <div>
      {error && <div className="error">{error}</div>}
      {msg && <div className="ok-msg">{msg}</div>}

      <h2>更正操作</h2>
      <div className="panel">
        <h4>批量撤回验证（同一验证条件）</h4>
        <div className="form">
          <label>验证条件组
            <input value={withdraw.group} onChange={(e) => setWithdraw({ ...withdraw, group: e.target.value })} /></label>
          <label>撤回生效时刻
            <input size={24} value={withdraw.time} onChange={(e) => setWithdraw({ ...withdraw, time: e.target.value })} /></label>
          <label>原因
            <input value={withdraw.reason} onChange={(e) => setWithdraw({ ...withdraw, reason: e.target.value })} /></label>
          <button onClick={doWithdrawGroup}>批量撤回</button>
        </div>
        <h4>清单更正（JSON：{"{"}"key": "PL-P4", "payload": {"{"}...{"}"}{"}"}）</h4>
        <div className="form" style={{ alignItems: 'flex-start' }}>
          <textarea rows={3} value={correctText} onChange={(e) => setCorrectText(e.target.value)}
            placeholder='{"key":"PL-P4","payload":{"manifest_id":"MF-01","vehicle_id":"VH-01","pallet_id":"P4","batches":[{"batch_id":"LOT-B","pieces":70}],"rect":{"x":5.0,"y":0.0,"w":0.8,"h":2.0},"layer":"L1","from_lo":"2026-11-15T09:00:00+00:00","to_lo":"2026-11-15T15:00:00+00:00"}}' />
          <button onClick={doCorrect} disabled={!correctText.trim()}>提交更正</button>
        </div>
      </div>

      <h2>影响查询（按实际引用追到货物与分析时段）</h2>
      <div className="toolbar">
        <select value={impactQ.kind} onChange={(e) => setImpactQ({ ...impactQ, kind: e.target.value })}>
          <option value="validation">验证资料</option>
          <option value="placement">装载记录</option>
          <option value="layout">车厢布局</option>
        </select>
        {impactQ.kind !== 'layout' && (
          <input value={impactQ.key} onChange={(e) => setImpactQ({ ...impactQ, key: e.target.value })}
                 placeholder="VAL-002 / PL-P4" />
        )}
        <button onClick={doQueryImpact}>查询影响</button>
      </div>
      {impact && (
        <div className="panel">
          <h4>受影响运行 {impact.affected_runs.length} 个{impact.current_layout_revision ? `（当前布局修订 ${impact.current_layout_revision}）` : ''}</h4>
          <table>
            <thead><tr><th>批次</th><th>分析窗口</th><th>涉及类别</th><th>运行</th></tr></thead>
            <tbody>
              {(impact.affected || []).map((a, i) => (
                <tr key={i}><td>{a.cargo_batch_id}</td>
                  <td><code>{HHMM(a.window_start)}–{HHMM(a.window_end)}</code></td>
                  <td>{a.categories.map((c) => CAT_LABELS[c] || c).join('、')}</td>
                  <td>{a.runs.join('、')}</td></tr>
              ))}
            </tbody>
          </table>
          <p className="muted">受影响导出：{impact.affected_exports?.join('、') || '无'}</p>
        </div>
      )}

      <h2>更正前后比较</h2>
      <div className="toolbar">
        <select value={cmp.a} onChange={(e) => setCmp({ ...cmp, a: e.target.value })}>
          <option value="">运行 A（前）</option>
          {runs.map((r) => <option key={r.id} value={r.id}>#{r.id} {HHMM(r.window_start)}–{HHMM(r.window_end)} {r.status}</option>)}
        </select>
        <select value={cmp.b} onChange={(e) => setCmp({ ...cmp, b: e.target.value })}>
          <option value="">运行 B（后）</option>
          {runs.map((r) => <option key={r.id} value={r.id}>#{r.id} {HHMM(r.window_start)}–{HHMM(r.window_end)} {r.status}</option>)}
        </select>
        <button onClick={doCompare} disabled={!cmp.a || !cmp.b}>比较</button>
      </div>
      {cmpResult && (
        <div className="panel">
          <h4>分窗口类别面积时间（m²·h）对比</h4>
          <table>
            <thead><tr><th>批次</th><th>窗口</th><th>运行 A</th><th>运行 B</th></tr></thead>
            <tbody>
              {cmpResult.windows.map((w, i) => (
                <tr key={i} className={JSON.stringify(w.a) !== JSON.stringify(w.b) ? 'selected' : ''}>
                  <td>{w.cargo_batch_id}</td><td><code>{HHMM(w.window_start)}</code></td>
                  <td>{Object.entries(w.a).filter(([, v]) => v > 0).map(([k, v]) => `${CAT_LABELS[k]}${v}`).join('，') || '—'}</td>
                  <td>{Object.entries(w.b).filter(([, v]) => v > 0).map(([k, v]) => `${CAT_LABELS[k]}${v}`).join('，') || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <h4>类别变化的分片（{cmpResult.changed_cells_total}）</h4>
          <table>
            <thead><tr><th>批次</th><th>托盘</th><th>窗口</th><th>区域</th><th>变化</th></tr></thead>
            <tbody>
              {cmpResult.changed_cells.slice(0, 30).map((x, i) => (
                <tr key={i}><td>{x.cell.cargo_batch_id}</td><td>{x.cell.pallet_id}</td>
                  <td><code>{HHMM(x.cell.window_start)}</code></td>
                  <td>[{x.cell.rect.x},{x.cell.rect.y} {x.cell.rect.w}×{x.cell.rect.h}]</td>
                  <td>{CAT_LABELS[x.from]} → {CAT_LABELS[x.to]}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h2>导出（保留依据，可查更正范围）</h2>
      <div className="toolbar">
        <span className="muted">从运行导出：</span>
        {runs.slice(0, 6).map((r) => (
          <button key={r.id} onClick={() => doExport(r.id)}>导出 #{r.id}</button>
        ))}
      </div>
      <table>
        <thead><tr><th>#</th><th>运行</th><th>标签</th><th>导出时间</th><th>导出后的更正</th></tr></thead>
        <tbody>
          {exports_.map((e) => (
            <tr key={e.id} onClick={async () => setExpDetail(await api.get(`/api/coverage/exports/${e.id}`))}
                style={{ cursor: 'pointer' }}>
              <td>{e.id}</td><td>运行 {e.run_id}</td><td>{e.label}</td>
              <td><code>{(e.created_at || '').slice(0, 19)}</code></td>
              <td>{expDetail?.id === e.id
                ? (expDetail.subsequent_corrections.map((c) => `${c.kind}:${c.entity_key}`).join('、') || '无')
                : '（点击查看）'}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <h2>更正事件</h2>
      <table>
        <thead><tr><th>#</th><th>类型</th><th>对象</th><th>生效时刻</th><th>记录时间</th><th>详情</th></tr></thead>
        <tbody>
          {corrections.map((c) => (
            <tr key={c.id}><td>{c.id}</td><td>{c.kind}</td><td>{c.entity_key}</td>
              <td><code>{c.effective_at || '—'}</code></td>
              <td><code>{(c.created_at || '').slice(0, 19)}</code></td>
              <td className="note muted">{JSON.stringify(c.detail)}</td></tr>
          ))}
        </tbody>
      </table>

      {inputs && (
        <>
          <h2>当前输入状态 <span className="muted">（布局修订 {inputs.current_layout_revision}）</span></h2>
          <h4>验证资料</h4>
          <table>
            <thead><tr><th>键</th><th>探头</th><th>代表范围</th><th>隔板配置</th><th>有效期</th><th>状态</th></tr></thead>
            <tbody>
              {inputs.validations.map((v) => (
                <tr key={v.validation_key + v.revision}>
                  <td>{v.validation_key} r{v.revision}</td><td>{v.probe_id}</td>
                  <td>[{v.range_rect.x},{v.range_rect.y} {v.range_rect.w}×{v.range_rect.h}] {v.range_layer}</td>
                  <td>{(v.partition_config || []).map((c) => `${c.partition_id}@${c.x}`).join('、') || '无'}</td>
                  <td><code>{HHMM(v.valid_from)}–{v.valid_to ? HHMM(v.valid_to) : '至今'}</code></td>
                  <td><span className={`tag ${v.status === 'active' ? 'ok' : 'bad'}`}>{v.status}</span>
                    {v.withdrawn_from && <span className="muted"> {HHMM(v.withdrawn_from)} 起</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <h4>装载清单</h4>
          <table>
            <thead><tr><th>键</th><th>清单</th><th>托盘</th><th>批次</th><th>区域</th><th>生效</th><th>结束</th><th>版本</th></tr></thead>
            <tbody>
              {inputs.placements.map((p) => (
                <tr key={p.placement_key + 'v' + p.version}>
                  <td>{p.placement_key}</td><td>{p.manifest_id}</td><td>{p.pallet_id}</td>
                  <td>{p.batches.map((b) => `${b.batch_id}×${b.pieces ?? '?'}`).join('、')}</td>
                  <td>[{p.rect.x},{p.rect.y} {p.rect.w}×{p.rect.h}] {p.layer}</td>
                  <td><code>{HHMM(p.from_lo)}{p.from_hi !== p.from_lo ? `~${HHMM(p.from_hi)}` : ''}</code></td>
                  <td><code>{p.to_lo ? `${HHMM(p.to_lo)}${p.to_hi !== p.to_lo ? `~${HHMM(p.to_hi)}` : ''}` : '—'}</code></td>
                  <td>v{p.version}{p.corrected_by ? '（已更正）' : ''}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  )
}
