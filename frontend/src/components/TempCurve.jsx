import React from 'react'

const COLORS = ['#2563eb', '#dc2626', '#16a34a', '#9333ea', '#ea580c', '#0891b2']

/** 多序列温度曲线（纯 SVG）：x 轴采样时间，y 轴标准摄氏温度。
 *  limits 可选 {min, max}：画批次温度上下限参考线。 */
export default function TempCurve({ series, height = 320, limits = null, windowRange = null }) {
  const entries = Object.entries(series || {})
  if (!entries.length) return <div className="empty">暂无曲线数据</div>
  const all = entries.flatMap(([, pts]) => pts)
  if (!all.length) return <div className="empty">暂无曲线数据</div>

  const W = 920, H = height, PL = 56, PR = 16, PT = 20, PB = 44
  const xs = all.map((p) => new Date(p.t).getTime())
  const ys = all.map((p) => p.std_c)
  const x0 = windowRange ? new Date(windowRange[0]).getTime() : Math.min(...xs)
  const x1 = windowRange ? new Date(windowRange[1]).getTime() : Math.max(...xs)
  let y0 = Math.min(...ys), y1 = Math.max(...ys)
  if (limits) { y0 = Math.min(y0, limits.min); y1 = Math.max(y1, limits.max) }
  if (y1 - y0 < 1) { y0 -= 0.5; y1 += 0.5 }
  const X = (t) => PL + ((t - x0) / Math.max(1, x1 - x0)) * (W - PL - PR)
  const Y = (v) => PT + (1 - (v - y0) / (y1 - y0)) * (H - PT - PB)

  const ticks = []
  for (let i = 0; i <= 4; i++) {
    const v = y0 + ((y1 - y0) * i) / 4
    ticks.push(v)
  }
  const fmtT = (t) => {
    const d = new Date(t)
    return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  }

  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} className="curve">
        {ticks.map((v, i) => (
          <g key={i}>
            <line x1={PL} x2={W - PR} y1={Y(v)} y2={Y(v)} stroke="#e5e7eb" />
            <text x={PL - 6} y={Y(v) + 4} textAnchor="end" fontSize="11" fill="#6b7280">
              {v.toFixed(1)}°C
            </text>
          </g>
        ))}
        {[0, 0.5, 1].map((r, i) => (
          <text key={i} x={X(x0 + (x1 - x0) * r)} y={H - PB + 18} textAnchor="middle"
                fontSize="11" fill="#6b7280">
            {fmtT(x0 + (x1 - x0) * r)}
          </text>
        ))}
        {limits && [limits.min, limits.max].map((v, i) => (
          <g key={`lim${i}`}>
            <line x1={PL} x2={W - PR} y1={Y(v)} y2={Y(v)} stroke="#dc2626"
                  strokeDasharray="6 4" strokeWidth="1.2" />
            <text x={W - PR} y={Y(v) - 3} textAnchor="end" fontSize="10" fill="#dc2626">
              {i === 0 ? `下限 ${v}°C` : `上限 ${v}°C`}
            </text>
          </g>
        ))}
        {entries.map(([name, pts], si) => {
          const color = COLORS[si % COLORS.length]
          const d = pts.map((p, i) =>
            `${i ? 'L' : 'M'}${X(new Date(p.t).getTime()).toFixed(1)},${Y(p.std_c).toFixed(1)}`).join(' ')
          return (
            <g key={name}>
              <path d={d} fill="none" stroke={color} strokeWidth="2" />
              {pts.map((p, i) => (
                <circle key={i} cx={X(new Date(p.t).getTime())} cy={Y(p.std_c)} r="3.5"
                        fill={p.quality_flag === 'ok' ? color : '#f59e0b'}>
                  <title>{`${name}\n${p.t}\n${p.std_c}°C ±${p.err_s?.toFixed?.(1)}s ${p.vehicle_id || '待归属'}`}</title>
                </circle>
              ))}
            </g>
          )
        })}
      </svg>
      <div className="legend">
        {entries.map(([name], si) => (
          <span key={name}>
            <i style={{ background: COLORS[si % COLORS.length] }} /> {name}
          </span>
        ))}
      </div>
    </div>
  )
}
