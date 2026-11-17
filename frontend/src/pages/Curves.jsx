import React, { useEffect, useState } from 'react'
import { api } from '../api.js'
import TempCurve from '../components/TempCurve.jsx'

export default function Curves() {
  const [cargo, setCargo] = useState('LOT-A')
  const [device, setDevice] = useState('')
  const [series, setSeries] = useState({})
  const [error, setError] = useState('')

  const load = () => {
    const q = device ? `device_id=${device}` : `cargo_batch_id=${cargo}`
    api.get(`/api/curves?${q}`).then((d) => setSeries(d.series)).catch((e) => setError(e.message))
  }
  useEffect(load, [cargo, device])

  return (
    <div>
      {error && <div className="error">{error}</div>}
      <div className="toolbar">
        <label>货物批次：</label>
        <select value={cargo} onChange={(e) => { setCargo(e.target.value); setDevice('') }}>
          {['LOT-A', 'LOT-B', 'LOT-C'].map((c) => <option key={c}>{c}</option>)}
        </select>
        <label>或设备：</label>
        <select value={device} onChange={(e) => setDevice(e.target.value)}>
          <option value="">（按货物批次）</option>
          {['PRB-1001', 'PRB-2002', 'PRB-3003'].map((d) => <option key={d}>{d}</option>)}
        </select>
      </div>
      <TempCurve series={series} />
      <table>
        <thead>
          <tr><th>序列</th><th>点数</th><th>时间范围</th><th>温度范围</th></tr>
        </thead>
        <tbody>
          {Object.entries(series).map(([name, pts]) => {
            const ts = pts.map((p) => p.t).sort()
            const vs = pts.map((p) => p.std_c)
            return (
              <tr key={name}>
                <td>{name}</td><td>{pts.length}</td>
                <td>{ts[0]} ~ {ts[ts.length - 1]}</td>
                <td>{Math.min(...vs).toFixed(2)} ~ {Math.max(...vs).toFixed(2)} °C</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
