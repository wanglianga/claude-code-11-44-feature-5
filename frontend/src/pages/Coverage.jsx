import React, { useEffect, useMemo, useState } from 'react'
import { api } from '../api.js'
import ZoneMap, { CAT_LABELS } from '../components/ZoneMap.jsx'
import TempCurve from '../components/TempCurve.jsx'

const HHMM = (s) => (s || '').slice(11, 16)

export default function Coverage() {
  const [vehicle] = useState('VH-01')
  const [runs, setRuns] = useState([])
  const [runId, setRunId] = useState(null)
  const [detail, setDetail] = useState(null)
  const [cells, setCells] = useState([])
  const [batch, setBatch] = useState('LOT-A')
  const [winKey, setWinKey] = useState('')
  const [intIdx, setIntIdx] = useState(0)
  const [sel, setSel] = useState(null)
  const [curves, setCurves] = useState({})
  const [unconfirmed, setUnconfirmed] = useState(null)
  const [error, setError] = useState('')
  const [msg, setMsg] = useState('')
  const [win, setWin] = useState({ start: '2026-11-15T09:00:00+00:00', end: '2026-11-15T15:00:00+00:00' })
  const [bundleText, setBundleText] = useState('')
  const [showImport, setShowImport] = useState(false)

  const loadRuns = () => {
    api.get(`/api/coverage/runs?vehicle_id=${vehicle}`)
      .then((d) => setRuns(d.runs)).catch((e) => setError(e.message))
  }
  useEffect(loadRuns, [vehicle])

  const loadRun = (id) => {
    setError(''); setSel(null); setCurves({})
    Promise.all([
      api.get(`/api/coverage/runs/${id}`),
      api.get(`/api/coverage/runs/${id}/cells`),
      api.get(`/api/coverage/runs/${id}/unconfirmed`),
    ]).then(([d, c, u]) => {
      setDetail(d); setCells(c.cells); setUnconfirmed(u)
      const batches = Object.keys(d.run.stats?.batches || {})
      if (batches.length && !batches.includes(batch)) setBatch(batches[0])
    }).catch((e) => setError(e.message))
  }
  useEffect(() => { if (runId) loadRun(runId) }, [runId])

  const stats = detail?.run?.stats?.batches?.[batch]
  const spec = stats?.spec
  const windows = stats?.windows || []
  const winStat = windows.find((w) => HHMM(w.start) === winKey) || windows[0]
  const winStart = winStat?.start, winEnd = winStat?.end

  // 当前窗口内的布局区间（由引擎在有效时刻切开）
  const intervals = useMemo(() => {
    const s = new Map()
    cells.filter((c) => c.cargo_batch_id === batch && c.window_start === winStart)
      .forEach((c) => s.set(c.interval_start, c.interval_end))
    return [...s.entries()].sort()
  }, [cells, batch, winStart])
  const interval = intervals[Math.min(intIdx, Math.max(0, intervals.length - 1))]

  const mapCells = useMemo(() => cells.filter((c) =>
    c.cargo_batch_id === batch && c.window_start === winStart &&
    (!interval || c.interval_start === interval[0])), [cells, batch, winStart, interval])

  // 区间内生效的隔板 / 验证 / 探头（与引擎同一套判断）
  const active = useMemo(() => {
    if (!detail || !interval) return { partitions: [], validations: [], probes: [] }
    const [is, ie] = interval
    const partitions = detail.partitions.filter((p) =>
      p.effective_from <= is && (!p.effective_to || p.effective_to >= ie))
    const config = new Set(partitions.map((p) => `${p.partition_id}@${p.x_position}`))
    const validations = detail.validations.filter((v) =>
      v.valid_from <= is && (!v.valid_to || v.valid_to >= ie)
      && (!v.withdrawn_from || v.withdrawn_from >= ie)
      && (v.partition_config || []).every((c) => config.has(`${c.partition_id}@${c.x}`))
      && (v.partition_config || []).length === config.size)
    const probes = detail.probes.filter((p) =>
      p.installed_from <= is && (!p.installed_to || p.installed_to >= ie))
    return { partitions, validations, probes }
  }, [detail, interval])

  // 温度时间轴联动：窗口内出现的探头
  useEffect(() => {
    if (!winStart) return
    const ids = new Set()
    cells.filter((c) => c.cargo_batch_id === batch && c.window_start === winStart)
      .forEach((c) => (c.probes || []).forEach((p) => ids.add(p.probe_id)))
    const out = {}
    Promise.all([...ids].map((id) =>
      api.get(`/api/curves?device_id=${id}`).then((d) => {
        Object.entries(d.series).forEach(([name, pts]) => {
          const inWin = pts.filter((p) => p.t >= winStart && p.t < winEnd)
          if (inWin.length) out[name] = inWin
        })
      }).catch(() => {}))).then(() => setCurves(out))
  }, [cells, batch, winStart, winEnd])

  const demo = async () => {
    setError(''); setMsg('')
    try {
      const r = await api.post('/api/coverage/demo')
      setMsg(`演示已装入：运行 #${r.run_id}（${r.run_status}）${r.note || ''}`)
      loadRuns(); setRunId(r.run_id)
    } catch (e) { setError(e.message) }
  }
  const runAnalysis = async () => {
    setError(''); setMsg('')
    try {
      const r = await api.post('/api/coverage/runs', {
        vehicle_id: vehicle, window_start: win.start, window_end: win.end,
      })
      setMsg(`分析完成：运行 #${r.run_id}（${r.run_status}）${r.note || ''}`)
      loadRuns(); setRunId(r.run_id)
    } catch (e) { setError(e.message) }
  }
  const doImport = async () => {
    setError(''); setMsg('')
    try {
      const bundle = JSON.parse(bundleText)
      const r = await api.post('/api/coverage/import', bundle)
      const n = Object.values(r.results).flat().length
      setMsg(`导入完成：${n} 条记录已处理（详见「更正与影响」页输入状态）`)
      setShowImport(false)
    } catch (e) { setError(e.message) }
  }

  return (
    <div>
      {error && <div className="error">{error}</div>}
      {msg && <div className="ok-msg">{msg}</div>}

      <div className="toolbar">
        <button onClick={demo}>一键演示（补读数+导入+分析）</button>
        <label>窗口开始 <input size={22} value={win.start}
          onChange={(e) => setWin({ ...win, start: e.target.value })} /></label>
        <label>窗口结束 <input size={22} value={win.end}
          onChange={(e) => setWin({ ...win, end: e.target.value })} /></label>
        <button onClick={runAnalysis}>运行分析</button>
        <button onClick={() => setShowImport(!showImport)}>导入输入…</button>
      </div>
      {showImport && (
        <div className="panel">
          <h4>导入覆盖输入（layouts / placements / partitions / probes / validations / batch_specs）</h4>
          <textarea rows={6} value={bundleText} onChange={(e) => setBundleText(e.target.value)}
                    placeholder='{"validations": [...]}' />
          <button onClick={doImport}>导入（幂等）</button>
        </div>
      )}

      <h2>分析运行</h2>
      <table>
        <thead><tr><th>#</th><th>窗口</th><th>批次</th><th>状态</th><th>输入版本</th><th>说明</th></tr></thead>
        <tbody>
          {runs.map((r) => (
            <tr key={r.id} className={r.id === runId ? 'selected' : ''}
                onClick={() => setRunId(r.id)} style={{ cursor: 'pointer' }}>
              <td>{r.id}</td>
              <td><code>{HHMM(r.window_start)}–{HHMM(r.window_end)}</code></td>
              <td>{r.cargo_batch_id || '全部'}</td>
              <td>
                <span className={`tag ${r.status === 'current' ? 'ok' : 'warn'}`}>{r.status}</span>
                {r.inputs_stale && <span className="tag warn">输入已变</span>}
              </td>
              <td className="muted">布局 r{r.used?.layout_revision}</td>
              <td className="note muted">{r.note}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {detail && stats && (
        <>
          <div className="toolbar">
            <label>货物批次：</label>
            <select value={batch} onChange={(e) => { setBatch(e.target.value); setIntIdx(0); setSel(null) }}>
              {Object.keys(detail.run.stats.batches).map((b) => <option key={b}>{b}</option>)}
            </select>
            <label>统计窗口：</label>
            <select value={winKey || HHMM(windows[0]?.start)}
              onChange={(e) => { setWinKey(e.target.value); setIntIdx(0); setSel(null) }}>
              {windows.map((w) => <option key={w.start} value={HHMM(w.start)}>
                {HHMM(w.start)}–{HHMM(w.end)}</option>)}
            </select>
            {intervals.length > 1 && (
              <>
                <label>布局区间：</label>
                <select value={intIdx} onChange={(e) => { setIntIdx(Number(e.target.value)); setSel(null) }}>
                  {intervals.map(([s, e2], i) => (
                    <option key={s} value={i}>{HHMM(s)}–{HHMM(e2)}</option>))}
                </select>
              </>
            )}
            <span className="muted">
              上下限 {spec.temp_min_c}~{spec.temp_max_c}°C · 统计窗 {spec.window_minutes}min
            </span>
          </div>

          {winStat && (
            <div className="cards">
              {Object.entries(CAT_LABELS).map(([k, label]) => (
                <div className="card" key={k}>
                  <div className="card-v">{((winStat.ratios?.[k] ?? 0) * 100).toFixed(1)}%</div>
                  <div className="card-l">{label}（{winStat.categories[k]} m²·h）</div>
                </div>
              ))}
              <div className="card warn">
                <div className="card-v">{winStat.occupancy_conflict}</div>
                <div className="card-l">占用冲突 m²·h（不计入分母）</div>
              </div>
            </div>
          )}

          <h3>车厢俯视（{batch} · {HHMM(winStart)}–{HHMM(winEnd)}
            {interval && ` · 区间 ${HHMM(interval[0])}–${HHMM(interval[1])}`}）</h3>
          <ZoneMap
            layout={detail.layout} cells={mapCells}
            validations={active.validations} probes={active.probes}
            partitions={active.partitions}
            placements={detail.placements.filter((p) => !p.corrected_by)}
            selectedId={sel?.id} onSelect={setSel} />

          {sel && (
            <div className="panel">
              <h4>分片详情：{sel.pallet_id} [{sel.rect.x},{sel.rect.y} {sel.rect.w}×{sel.rect.h}m]
                <span className={`tag ${sel.category === 'supported' ? 'ok' : 'warn'}`}>
                  {CAT_LABELS[sel.category]}</span>
                {sel.reading_verdict && <span className="tag">{sel.reading_verdict}</span>}
                {sel.mixed_pallet && <span className="tag warn">混批托盘·共同覆盖</span>}
                {sel.occupancy_conflict && <span className="tag bad">占用冲突</span>}
              </h4>
              <p className="muted">原因：{sel.reasons.join('、')}；封闭分区 {sel.enclosure}；
                布局区间 {HHMM(sel.interval_start)}–{HHMM(sel.interval_end)}</p>
              {sel.conflict_with?.length > 0 &&
                <p className="bad">与 {sel.conflict_with.join('、')} 争夺同一空间</p>}
              {sel.layout_variants && (
                <table>
                  <thead><tr><th>可行布局</th><th>类别</th></tr></thead>
                  <tbody>{sel.layout_variants.map((v, i) => (
                    <tr key={i}><td>{v.variant}</td><td>{v.category}</td></tr>))}
                  </tbody>
                </table>
              )}
              <table>
                <thead><tr><th>探头</th><th>有资格</th><th>验证</th><th>读数</th><th>温度</th><th>结论</th></tr></thead>
                <tbody>
                  {(sel.probes || []).map((p) => (
                    <tr key={p.probe_id}>
                      <td>{p.probe_id}</td>
                      <td>{p.validated ? '是' : '否（点观测不代表区域）'}</td>
                      <td>{(p.validation_keys || []).join('、') || '—'}</td>
                      <td>{p.readings}</td>
                      <td>{(p.temps || []).join('、')}</td>
                      <td>{p.verdict || '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <h3>温度时间轴（窗口内探头读数，红虚线 = 批次上下限）</h3>
          <TempCurve series={curves} height={260}
                     limits={spec ? { min: spec.temp_min_c, max: spec.temp_max_c } : null}
                     windowRange={[winStart, winEnd]} />

          {unconfirmed && unconfirmed.unconfirmed_cells.length > 0 && (
            <>
              <h3>未确认原因（本运行全部窗口）</h3>
              <table>
                <thead><tr><th>批次</th><th>窗口</th><th>托盘</th><th>区域</th><th>类别</th><th>原因</th></tr></thead>
                <tbody>
                  {unconfirmed.unconfirmed_cells
                    .filter((c) => c.cargo_batch_id === batch).slice(0, 50).map((c) => (
                    <tr key={c.id}>
                      <td>{c.cargo_batch_id}</td>
                      <td><code>{HHMM(c.window_start)}</code></td>
                      <td>{c.pallet_id}</td>
                      <td>[{c.rect.x},{c.rect.y} {c.rect.w}×{c.rect.h}]</td>
                      <td>{CAT_LABELS[c.category]}</td>
                      <td className="note muted">{c.reasons.join('、')}</td>
                    </tr>))}
                </tbody>
              </table>
              {unconfirmed.unvalidated_probe_points.length > 0 && (
                <p className="muted">探头实际测量点（点观测不等同整块区域）：
                  {unconfirmed.unvalidated_probe_points.map((p) =>
                    `${p.probe_id}@(${p.point[0]},${p.point[1]})`).join('、')}</p>
              )}
            </>
          )}
        </>
      )}
    </div>
  )
}
