import React, { useEffect, useState } from 'react'
import { api } from '../api.js'

export default function Publish() {
  const [pubs, setPubs] = useState([])
  const [purposes, setPurposes] = useState({})
  const [consumers, setConsumers] = useState([])
  const [page, setPage] = useState(null)
  const [records, setRecords] = useState([])
  const [msg, setMsg] = useState('')
  const [error, setError] = useState('')
  const [newConsumer, setNewConsumer] = useState({ name: 'audit-app', purpose: 'batch_audit' })

  const load = () => {
    api.get('/api/publications').then((d) => { setPubs(d.publications); setPurposes(d.purposes) })
      .catch((e) => setError(e.message))
    api.get('/api/consumers').then((d) => setConsumers(d.consumers)).catch((e) => setError(e.message))
    api.get('/api/consumption-records').then((d) => setRecords(d.records)).catch((e) => setError(e.message))
  }
  useEffect(load, [])

  const run = async (fn, ok) => {
    setError(''); setMsg('')
    try { setMsg(ok(await fn())); load() } catch (e) { setError(e.message) }
  }
  const build = (purpose) => run(
    () => api.post(`/api/publications/build?purpose=${purpose}`),
    (r) => `已发布 ${r.purpose} v${r.version}：${r.item_count} 条`)
  const createConsumer = () => run(
    () => api.post('/api/consumers', newConsumer),
    (r) => `消费端 #${r.id}（${r.name}/${r.purpose}）就绪`)
  const pull = (cid) => run(
    () => api.post(`/api/consumers/${cid}/pull`),
    (r) => { setPage({ cid, ...r }); return r.items?.length ? `拉到 ${r.items.length} 条（版本 ${r.version} 分页 ${r.from}..${r.to}${r.redelivered ? '，重发' : ''}）` : `无更多数据（${r.note || '读完'}）` })
  const ack = () => run(
    () => api.post(`/api/consumers/${page.cid}/ack`,
      { version: page.version, from_seq: page.from, to_seq: page.to }),
    (r) => { setPage(null); return `已确认，游标 → v${r.cursor_version}#${r.cursor_seq}` })

  return (
    <div>
      {error && <div className="error">{error}</div>}
      {msg && <div className="ok-msg">{msg}</div>}
      <h2>质量发布</h2>
      <div className="cards">
        {Object.entries(purposes).map(([k, bar]) => (
          <div className="card" key={k}>
            <div className="card-l">{k}</div>
            <div className="muted">
              时间误差 ≤ {bar.max_time_error_s}s{bar.require_attribution ? '，必须完整归属' : '，归属可选'}
            </div>
            <button onClick={() => build(k)}>构建发布版本</button>
          </div>
        ))}
      </div>
      <table>
        <thead><tr><th>用途</th><th>版本</th><th>状态</th><th>条数</th><th>关系版本</th><th>发布时间</th></tr></thead>
        <tbody>
          {pubs.map((p) => (
            <tr key={p.id}>
              <td>{p.purpose}</td><td>v{p.version}</td>
              <td><span className={p.status === 'current' ? 'tag ok' : 'tag'}>{p.status}</span></td>
              <td>{p.item_count}</td><td>{p.relation_revision}</td><td>{p.created_at}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <h2>模拟消费端</h2>
      <div className="form">
        <label>名称 <input value={newConsumer.name}
          onChange={(e) => setNewConsumer({ ...newConsumer, name: e.target.value })} /></label>
        <label>用途
          <select value={newConsumer.purpose}
            onChange={(e) => setNewConsumer({ ...newConsumer, purpose: e.target.value })}>
            {Object.keys(purposes).map((p) => <option key={p}>{p}</option>)}
          </select>
        </label>
        <button onClick={createConsumer}>创建/获取消费端</button>
      </div>
      <table>
        <thead><tr><th>消费端</th><th>用途</th><th>游标</th><th>未确认分页</th><th>已确认页数</th><th>操作</th></tr></thead>
        <tbody>
          {consumers.map((c) => (
            <tr key={c.id}>
              <td>{c.name}</td><td>{c.purpose}</td>
              <td>{c.cursor_version ? `v${c.cursor_version}#${c.cursor_seq}` : '（未开始）'}</td>
              <td>{c.pending_page ? `v${c.pending_page.version} [${c.pending_page.from}..${c.pending_page.to}]` : '—'}</td>
              <td>{c.acked_pages}</td>
              <td><button onClick={() => pull(c.id)}>拉取下一页</button></td>
            </tr>
          ))}
        </tbody>
      </table>

      {page && page.items?.length > 0 && (
        <div className="panel">
          <h4>未确认分页：版本 {page.version} [{page.from}..{page.to}]
            {page.redelivered && <span className="tag warn">失败恢复重发</span>}</h4>
          <table>
            <thead><tr><th>seq</th><th>设备</th><th>标准温度</th><th>采样时间</th><th>误差(s)</th><th>车辆</th><th>货物批次</th></tr></thead>
            <tbody>
              {page.items.map((i) => (
                <tr key={i.seq}>
                  <td>{i.seq}</td><td>{i.device_id}</td><td>{i.std_c}°C</td>
                  <td>{i.sample_time}</td><td>{i.time_error_s?.toFixed(1)}</td>
                  <td>{i.vehicle_id || '—'}</td><td>{i.cargo_batch_id || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <button onClick={ack}>确认此分页（推进游标）</button>
        </div>
      )}

      <h3>消费记录</h3>
      <table>
        <thead><tr><th>记录</th><th>消费端</th><th>发布版本</th><th>分页</th><th>条数</th><th>确认时间</th></tr></thead>
        <tbody>
          {records.map((r) => (
            <tr key={r.id}>
              <td>#{r.id}</td><td>{r.consumer_id}</td><td>pub {r.publication_id}</td>
              <td>[{r.from_seq}..{r.to_seq}]</td><td>{r.item_count}</td><td>{r.acked_at}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
