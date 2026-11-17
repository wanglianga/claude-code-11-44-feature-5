import React from 'react'

export const CAT_COLORS = {
  supported: '#86efac',
  conflict: '#fca5a5',
  no_reading: '#fcd34d',
  no_qualification: '#d1d5db',
  layout_pending: '#c4b5fd',
}
export const CAT_LABELS = {
  supported: '有观测支持',
  conflict: '观测冲突',
  no_reading: '有资格缺读数',
  no_qualification: '缺资格（未确认）',
  layout_pending: '布局待核',
}
const PROBE_COLORS = ['#2563eb', '#dc2626', '#16a34a', '#9333ea', '#ea580c']

/** 车厢俯视图（纯 SVG）：货物分片类别、托盘边界、验证代表范围、探头测量点、隔板。
 *  x 轴指向门口（右），y 轴指向右侧（下）。 */
export default function ZoneMap({ layout, cells, validations, probes, partitions,
                                  placements, selectedId, onSelect, height = 300 }) {
  if (!layout) return <div className="empty">暂无布局</div>
  const W = 920, H = height, PL = 48, PR = 16, PT = 26, PB = 34
  const L = layout.length_m, Wd = layout.width_m
  const X = (x) => PL + (x / L) * (W - PL - PR)
  const Y = (y) => PT + (y / Wd) * (H - PT - PB)
  const probeColor = {}
  ;(validations || []).forEach((v) => {
    if (!(v.probe_id in probeColor)) {
      probeColor[v.probe_id] = PROBE_COLORS[Object.keys(probeColor).length % PROBE_COLORS.length]
    }
  })

  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} className="curve">
        {/* 厢体与分区 */}
        <rect x={X(0)} y={Y(0)} width={X(L) - X(0)} height={Y(Wd) - Y(0)}
              fill="#f8fafc" stroke="#334155" strokeWidth="1.5" />
        {(layout.zones || []).map((z) => (
          <g key={z.zone_id}>
            <rect x={X(z.rect.x)} y={Y(z.rect.y)}
                  width={X(z.rect.x + z.rect.w) - X(z.rect.x)}
                  height={Y(z.rect.y + z.rect.h) - Y(z.rect.y)}
                  fill="none" stroke="#cbd5e1" strokeDasharray="3 3" />
            <text x={X(z.rect.x) + 4} y={Y(z.rect.y) + 12} fontSize="10" fill="#94a3b8">
              {z.name}
            </text>
          </g>
        ))}
        {/* 货物分片（类别着色；占用冲突加斜纹） */}
        <defs>
          <pattern id="occ" width="6" height="6" patternUnits="userSpaceOnUse"
                   patternTransform="rotate(45)">
            <rect width="6" height="6" fill="#fecaca" />
            <line x1="0" y1="0" x2="0" y2="6" stroke="#991b1b" strokeWidth="1.5" />
          </pattern>
        </defs>
        {(cells || []).map((c) => (
          <g key={c.id} onClick={() => onSelect && onSelect(c)} style={{ cursor: 'pointer' }}>
            <rect x={X(c.rect.x)} y={Y(c.rect.y)}
                  width={Math.max(1, X(c.rect.x + c.rect.w) - X(c.rect.x))}
                  height={Math.max(1, Y(c.rect.y + c.rect.h) - Y(c.rect.y))}
                  fill={c.occupancy_conflict ? 'url(#occ)' : CAT_COLORS[c.category]}
                  fillOpacity="0.75"
                  stroke={selectedId === c.id ? '#1d4ed8' : '#64748b'}
                  strokeWidth={selectedId === c.id ? 2.5 : 0.8}>
              <title>{`${c.pallet_id} [${c.rect.x},${c.rect.y} ${c.rect.w}×${c.rect.h}] ${CAT_LABELS[c.category]}`}</title>
            </rect>
            {c.rect.w > 0.4 && (
              <text x={X(c.rect.x + c.rect.w / 2)} y={Y(c.rect.y + c.rect.h / 2) + 4}
                    textAnchor="middle" fontSize="11" fill="#1f2937" pointerEvents="none">
                {c.pallet_id}
              </text>
            )}
          </g>
        ))}
        {/* 托盘边界（虚线） */}
        {(placements || []).map((p) => (
          <rect key={p.placement_key + p.pallet_id} x={X(p.rect.x)} y={Y(p.rect.y)}
                width={X(p.rect.x + p.rect.w) - X(p.rect.x)}
                height={Y(p.rect.y + p.rect.h) - Y(p.rect.y)}
                fill="none" stroke="#475569" strokeDasharray="5 3" strokeWidth="1">
            <title>{`${p.pallet_id}（${p.manifest_id}）${p.rect.w}×${p.rect.h}m`}</title>
          </rect>
        ))}
        {/* 验证代表范围（按探头着色，点划线） */}
        {(validations || []).map((v) => (
          <rect key={v.validation_key} x={X(v.range_rect.x)} y={Y(v.range_rect.y)}
                width={X(v.range_rect.x + v.range_rect.w) - X(v.range_rect.x)}
                height={Y(v.range_rect.y + v.range_rect.h) - Y(v.range_rect.y)}
                fill="none" stroke={probeColor[v.probe_id]} strokeWidth="1.6"
                strokeDasharray="10 4" fillOpacity="0">
            <title>{`${v.validation_key}：${v.probe_id} 已验证代表范围`}</title>
          </rect>
        ))}
        {/* 探头测量点 */}
        {(probes || []).map((p) => (
          <g key={p.probe_id + p.x}>
            <circle cx={X(p.x)} cy={Y(p.y)} r="5"
                    fill={probeColor[p.probe_id] || '#0f172a'} stroke="#fff" strokeWidth="1.5">
              <title>{`${p.probe_id} 实际测量点 (${p.x}, ${p.y})`}</title>
            </circle>
            <text x={X(p.x) + 8} y={Y(p.y) - 6} fontSize="10"
                  fill={probeColor[p.probe_id] || '#0f172a'}>{p.probe_id}</text>
          </g>
        ))}
        {/* 隔板 */}
        {(partitions || []).map((pt) => (
          <g key={pt.partition_id}>
            <line x1={X(pt.x_position)} x2={X(pt.x_position)} y1={Y(0)} y2={Y(Wd)}
                  stroke="#7c3aed" strokeWidth="2.5" strokeDasharray="2 3" />
            <text x={X(pt.x_position)} y={Y(0) - 6} textAnchor="middle" fontSize="10"
                  fill="#7c3aed">{`隔板 ${pt.partition_id}@${pt.x_position}m`}</text>
          </g>
        ))}
        {/* 坐标轴标注 */}
        <text x={X(L / 2)} y={H - 8} textAnchor="middle" fontSize="11" fill="#6b7280">
          x（指向门口，米）0 = 最内侧，{L} = 门口
        </text>
        <text x={14} y={Y(Wd / 2)} fontSize="11" fill="#6b7280"
              transform={`rotate(-90 14 ${Y(Wd / 2)})`} textAnchor="middle">y（米）</text>
      </svg>
      <div className="legend">
        {Object.entries(CAT_LABELS).map(([k, label]) => (
          <span key={k}><i style={{ background: CAT_COLORS[k] }} /> {label}</span>
        ))}
        <span><i style={{ background: 'url(#occ)', border: '1px solid #991b1b' }} /> 占用冲突</span>
      </div>
    </div>
  )
}
