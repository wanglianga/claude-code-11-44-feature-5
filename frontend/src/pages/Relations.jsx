import React, { useEffect, useState } from 'react'
import { api } from '../api.js'

export default function Relations() {
  const [relations, setRelations] = useState([])
  const [rev, setRev] = useState(0)
  const [anchors, setAnchors] = useState([])
  const [error, setError] = useState('')
  const [msg, setMsg] = useState('')
  const [rel, setRel] = useState({
    probe_id: 'PRB-2002', vehicle_id: 'VH-02', box_id: 'BX-02',
    cargo_batch_id: 'LOT-B', valid_from: '2026-11-15T14:00:00+00:00', valid_to: '',
  })
  const [anc, setAnc] = useState({
    device_id: 'PRB-2002', boot_batch: '2', counter: 40,
    server_time: '2026-11-15T13:20:00+00:00', error_s: 2.0, source: 'gateway_sync',
  })

  const load = () => {
    api.get('/api/relations').then((d) => { setRelations(d.relations); setRev(d.relation_revision) })
      .catch((e) => setError(e.message))
    api.get('/api/anchors').then((d) => setAnchors(d.anchors)).catch((e) => setError(e.message))
  }
  useEffect(load, [])

  const addRelation = async () => {
    setError(''); setMsg('')
    try {
      const body = { ...rel, valid_to: rel.valid_to || null }
      const r = await api.post('/api/relations', body)
      setMsg(`已新增安装区间 #${r.id}，关系版本 → ${r.relation_revision}（既有读数不会自动改归属，需重放）`)
      load()
    } catch (e) { setError(e.message) }
  }
  const addAnchor = async () => {
    setError(''); setMsg('')
    try {
      const r = await api.post('/api/anchors', { ...anc, counter: Number(anc.counter), error_s: Number(anc.error_s) })
      setMsg(`已新增锚点 #${r.id}（新增锚点后需重放或等新标准化才会重算时间）`)
      load()
    } catch (e) { setError(e.message) }
  }

  return (
    <div>
      {error && <div className="error">{error}</div>}
      {msg && <div className="ok-msg">{msg}</div>}
      <h2>设备安装关系 <span className="muted">（关系版本 {rev}）</span></h2>
      <table>
        <thead><tr><th>探头</th><th>车辆</th><th>周转箱</th><th>货物批次</th><th>有效区间</th></tr></thead>
        <tbody>
          {relations.map((r) => (
            <tr key={r.id}>
              <td>{r.probe_id}</td><td>{r.vehicle_id}</td><td>{r.box_id}</td>
              <td>{r.cargo_batch_id}</td>
              <td><code>{r.valid_from} ~ {r.valid_to || '至今'}</code></td>
            </tr>
          ))}
        </tbody>
      </table>
      <h4>新增安装区间</h4>
      <div className="form">
        {['probe_id', 'vehicle_id', 'box_id', 'cargo_batch_id', 'valid_from', 'valid_to'].map((k) => (
          <label key={k}>{k}
            <input value={rel[k]} onChange={(e) => setRel({ ...rel, [k]: e.target.value })} />
          </label>
        ))}
        <button onClick={addRelation}>新增（关系版本 +1）</button>
      </div>

      <h2>可信锚点 <span className="muted">（设备本地计数 ↔ 服务器时间）</span></h2>
      <table>
        <thead><tr><th>设备</th><th>启动批次</th><th>计数</th><th>服务器时间</th><th>误差(s)</th><th>来源</th></tr></thead>
        <tbody>
          {anchors.map((a) => (
            <tr key={a.id}>
              <td>{a.device_id}</td><td>{a.boot_batch}</td><td>{a.counter}</td>
              <td><code>{a.server_time}</code></td><td>{a.error_s}</td><td>{a.source}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <h4>新增锚点</h4>
      <div className="form">
        {['device_id', 'boot_batch', 'counter', 'server_time', 'error_s'].map((k) => (
          <label key={k}>{k}
            <input value={anc[k]} onChange={(e) => setAnc({ ...anc, [k]: e.target.value })} />
          </label>
        ))}
        <button onClick={addAnchor}>新增锚点</button>
      </div>
    </div>
  )
}
