import React, { useEffect, useState } from 'react'
import { api } from '../api.js'
import HexView from '../components/HexView.jsx'

const FRAME_STATUS = {
  ok: ['完整帧', 'tag ok'], bad_crc: ['坏校验', 'tag bad'],
  bad_length: ['非法长度', 'tag bad'], garbage: ['噪声字节', 'tag warn'],
  truncated: ['截断', 'tag warn'],
}
const PARSE_STATUS = {
  parsed_new: ['新样本', 'tag ok'], parsed_dup: ['重复去重', 'tag'],
  conflict: ['载荷冲突', 'tag bad'], pending_contract: ['待解析(无契约)', 'tag warn'],
  resolved: ['已重放解决', 'tag'], error: ['解析错误', 'tag bad'],
}

export default function Batches() {
  const [overview, setOverview] = useState(null)
  const [batches, setBatches] = useState([])
  const [detail, setDetail] = useState(null)
  const [frameDetail, setFrameDetail] = useState(null)
  const [error, setError] = useState('')

  const load = () => {
    api.get('/api/overview').then(setOverview).catch((e) => setError(e.message))
    api.get('/api/batches').then((d) => setBatches(d.batches)).catch((e) => setError(e.message))
  }
  useEffect(load, [])

  const openBatch = (id) => {
    setFrameDetail(null)
    api.get(`/api/batches/${id}`).then(setDetail).catch((e) => setError(e.message))
  }
  const openFrame = (id) => {
    api.get(`/api/frames/${id}`).then(setFrameDetail).catch((e) => setError(e.message))
  }

  return (
    <div>
      {error && <div className="error">{error}</div>}
      {overview && (
        <div className="cards">
          <Stat label="传输批次" v={overview.batches} />
          <Stat label="原始信封" v={overview.envelopes} />
          <Stat label="帧" v={overview.frames} />
          <Stat label="样本" v={overview.samples} />
          <Stat label="载荷冲突" v={overview.conflicts} warn={overview.conflicts > 0} />
          <Stat label="待解析(无契约)" v={overview.pending_contract} warn={overview.pending_contract > 0} />
          <Stat label="待归属(缺锚)" v={overview.pending_anchor} warn={overview.pending_anchor > 0} />
          <Stat label="待归属(边界)" v={overview.pending_attribution} warn={overview.pending_attribution > 0} />
        </div>
      )}

      <h2>发送批次</h2>
      <table>
        <thead>
          <tr>
            <th>批次</th><th>来源</th><th>协议</th><th>说明</th>
            <th>完整帧</th><th>坏校验</th><th>截断</th><th>噪声</th>
            <th>新样本</th><th>去重</th><th>冲突</th><th>待解析</th><th>待归属</th><th></th>
          </tr>
        </thead>
        <tbody>
          {batches.map((b) => (
            <tr key={b.id} className={detail?.batch.id === b.id ? 'selected' : ''}>
              <td>#{b.id}</td><td>{b.source}</td><td>{b.protocol}</td>
              <td className="note">{b.note}</td>
              <td>{b.frames_ok}</td>
              <td className={b.frames_bad_crc ? 'bad' : ''}>{b.frames_bad_crc}</td>
              <td className={b.frames_truncated ? 'warn' : ''}>{b.frames_truncated}</td>
              <td>{b.frames_garbage + b.frames_bad_length}</td>
              <td>{b.parsed_new}</td><td>{b.parsed_dup}</td>
              <td className={b.conflicts ? 'bad' : ''}>{b.conflicts}</td>
              <td className={b.pending_contract ? 'warn' : ''}>{b.pending_contract}</td>
              <td className={b.pending_attribution + b.pending_anchor ? 'warn' : ''}>
                {b.pending_attribution + b.pending_anchor}
              </td>
              <td><button onClick={() => openBatch(b.id)}>下钻</button></td>
            </tr>
          ))}
        </tbody>
      </table>

      {detail && (
        <div className="panel">
          <h3>批次 #{detail.batch.id} · {detail.batch.note}</h3>
          <p className="muted">
            连接：{detail.connections.map((c) =>
              `${c.conn_key}(${c.protocol}${c.open ? ', 打开' : ', 关闭'}，收 ${c.received_bytes}B，待组帧 ${c.pending_bytes}B)`
            ).join('；') || '无'}
          </p>

          <h4>帧序列（字节偏移为连接流内绝对位置）</h4>
          <table>
            <thead><tr><th>帧</th><th>连接</th><th>字节区间</th><th>状态</th><th>说明</th><th></th></tr></thead>
            <tbody>
              {detail.frames.map((f) => {
                const [label, cls] = FRAME_STATUS[f.status] || [f.status, 'tag']
                return (
                  <tr key={f.id}>
                    <td>#{f.id}</td><td>{f.conn}</td>
                    <td><code>[{f.start_off}, {f.end_off})</code></td>
                    <td><span className={cls}>{label}</span></td>
                    <td className="note">{f.detail}</td>
                    <td>{f.status === 'ok' && <button onClick={() => openFrame(f.id)}>解析与换算</button>}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>

          <h4>解析记录</h4>
          <table>
            <thead><tr><th>记录</th><th>状态</th><th>设备</th><th>启动批次</th><th>序号</th><th>说明</th></tr></thead>
            <tbody>
              {detail.parsed.map((p) => {
                const [label, cls] = PARSE_STATUS[p.status] || [p.status, 'tag']
                return (
                  <tr key={p.id}>
                    <td>#{p.id}</td>
                    <td><span className={cls}>{label}</span></td>
                    <td>{p.device_id || '—'}</td><td>{p.boot_batch || '—'}</td>
                    <td>{p.sample_seq ?? '—'}</td>
                    <td className="note">{p.error || ''}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>

          {detail.conflicts.length > 0 && (
            <>
              <h4>载荷冲突（相同标识不同载荷，保留原值）</h4>
              <table>
                <thead><tr><th>冲突</th><th>样本</th><th>到达载荷</th></tr></thead>
                <tbody>
                  {detail.conflicts.map((c) => (
                    <tr key={c.id}>
                      <td>#{c.id}</td><td>样本 {c.sample_id}</td>
                      <td><pre>{JSON.stringify(c.incoming_fields?.raw_fields, null, 1)}</pre></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}

          <details>
            <summary>原始信封字节（无损保存）</summary>
            {detail.envelopes.map((e) => (
              <div key={e.id} className="env">
                <span className="muted">信封 #{e.id} {e.kind} {e.off?.[0] != null && `[${e.off[0]}, ${e.off[1]})`} {e.size}B</span>
                <HexView hex={e.hex} baseOff={e.off?.[0] || 0} />
              </div>
            ))}
          </details>
        </div>
      )}

      {frameDetail && (
        <div className="panel">
          <h3>帧 #{frameDetail.frame.id} 解析与换算</h3>
          <p>
            字节区间 <code>[{frameDetail.frame.start_off}, {frameDetail.frame.end_off})</code>{' '}
            <span className="tag ok">{frameDetail.frame.status}</span>
          </p>
          <HexView hex={frameDetail.frame.hex} baseOff={frameDetail.frame.start_off} />
          {frameDetail.parsed?.fields && (
            <>
              <h4>解析字段（按契约）</h4>
              <pre>{JSON.stringify(frameDetail.parsed.fields.raw_fields, null, 2)}</pre>
            </>
          )}
          {frameDetail.reading && (
            <>
              <h4>标准化结果</h4>
              <p>
                标准温度 <b>{frameDetail.reading.std_c}°C</b>（{frameDetail.reading.quality_flag}），
                采样时间 {frameDetail.reading.sample_time} ±{frameDetail.reading.time_error_s?.toFixed(2)}s，
                归属 {frameDetail.reading.vehicle_id || '待归属'}
                {frameDetail.reading.cargo_batch_id ? ` / ${frameDetail.reading.cargo_batch_id}` : ''}
                <span className="muted">（契约 {frameDetail.reading.contract_key} r{frameDetail.reading.contract_revision}）</span>
              </p>
              <h4>换算过程</h4>
              <ol className="steps">
                {frameDetail.reading.conversion?.steps?.map((s, i) => (
                  <li key={i}><code>{s.step}</code> → <b>{s.value}</b> {s.unit}
                    <span className="muted"> {s.note}</span></li>
                ))}
              </ol>
              <h4>时钟映射</h4>
              <pre>{JSON.stringify(frameDetail.reading.conversion?.clock, null, 2)}</pre>
            </>
          )}
          <button onClick={() => setFrameDetail(null)}>收起</button>
        </div>
      )}
    </div>
  )
}

function Stat({ label, v, warn }) {
  return (
    <div className={warn ? 'card warn' : 'card'}>
      <div className="card-v">{v}</div>
      <div className="card-l">{label}</div>
    </div>
  )
}
