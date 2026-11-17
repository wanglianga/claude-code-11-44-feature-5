import React, { useEffect, useState } from 'react'
import { api } from '../api.js'

const R2_TEMPLATE = JSON.stringify({
  contract_key: 'json-temp', kind: 'json',
  match: { fw: ['2.3.0', '2.3.1', '3.0.0'] },
  definition: {
    kind: 'json',
    match: { fw: ['2.3.0', '2.3.1', '3.0.0'] },
    rules: {
      '2.3': {
        device_field: 'dev', device_map: {}, boot_field: 'boot', seq_field: 'seq',
        temp: { field: 'c', unit: 'C', cal_mul: 1.0, cal_add: 0.0 },
        clock: { mode: 'explicit_ts', field: 'ts', error_s: 5.0 },
      },
      '3.0': {
        device_field: 'asset', device_map: { 'A-1001': 'PRB-1001' },
        boot_field: 'boot', seq_field: 'seq',
        temp: { field: 'f', unit: 'F', cal_mul: 1.0, cal_add: 0.0 },
        clock: { mode: 'explicit_ts', field: 'ts', error_s: 8.0 },
      },
      default: {
        device_field: 'dev', device_map: {}, boot_field: 'boot', seq_field: 'seq',
        temp: { field: 'c', unit: 'C', cal_mul: 1.0, cal_add: 0.0 },
        clock: { mode: 'explicit_ts', field: 'ts', error_s: 5.0 },
      },
    },
  },
  note: '补充固件 3.0.0：华氏度 + 资产号映射',
}, null, 2)

export default function Contracts() {
  const [contracts, setContracts] = useState([])
  const [runs, setRuns] = useState([])
  const [text, setText] = useState(R2_TEMPLATE)
  const [msg, setMsg] = useState('')
  const [error, setError] = useState('')

  const load = () => {
    api.get('/api/contracts').then((d) => setContracts(d.contracts)).catch((e) => setError(e.message))
    api.get('/api/runs').then((d) => setRuns(d.runs)).catch((e) => setError(e.message))
  }
  useEffect(load, [])

  const create = async () => {
    setError(''); setMsg('')
    try {
      const body = JSON.parse(text)
      const r = await api.post('/api/contracts', body)
      setMsg(`已创建契约草稿 #${r.id}（修订 ${r.revision}），请激活`)
      load()
    } catch (e) { setError(e.message) }
  }
  const act = async (path) => {
    setError(''); setMsg('')
    try {
      const r = await api.post(path)
      setMsg(JSON.stringify(r))
      load()
    } catch (e) { setError(e.message) }
  }

  return (
    <div>
      {error && <div className="error">{error}</div>}
      {msg && <div className="ok-msg">{msg}</div>}
      <h2>协议契约（版本化）</h2>
      <table>
        <thead>
          <tr><th>契约</th><th>修订</th><th>类型</th><th>适用固件</th><th>状态</th><th>说明</th><th>操作</th></tr>
        </thead>
        <tbody>
          {contracts.map((c) => (
            <tr key={c.id}>
              <td>{c.contract_key}</td><td>r{c.revision}</td><td>{c.kind}</td>
              <td><code>{JSON.stringify(c.match)}</code></td>
              <td><span className={c.status === 'active' ? 'tag ok' : c.status === 'draft' ? 'tag warn' : 'tag'}>
                {c.status}</span></td>
              <td className="note">{c.note}</td>
              <td>
                {c.status === 'draft' &&
                  <button onClick={() => act(`/api/contracts/${c.id}/activate`)}>激活</button>}
                {c.status === 'active' &&
                  <button onClick={() => act(`/api/contracts/${c.id}/replay`)}>按此契约重放</button>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <h3>新建契约修订（JSON）</h3>
      <textarea rows={18} value={text} onChange={(e) => setText(e.target.value)} />
      <div><button onClick={create}>创建草稿</button></div>

      <h3>重放/标准化运行</h3>
      <table>
        <thead><tr><th>运行</th><th>类型</th><th>契约</th><th>修订</th><th>状态</th><th>统计</th><th>中止原因</th></tr></thead>
        <tbody>
          {runs.map((r) => (
            <tr key={r.id}>
              <td>#{r.id}</td><td>{r.kind}</td><td>{r.contract_key}</td>
              <td>r{r.from_revision ?? '-'} → r{r.to_revision ?? '-'}</td>
              <td><span className={r.status === 'committed' ? 'tag ok' : r.status === 'aborted' ? 'tag bad' : 'tag warn'}>
                {r.status}</span></td>
              <td><code>{JSON.stringify(r.stats)}</code></td>
              <td className="note">{r.abort_reason}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
