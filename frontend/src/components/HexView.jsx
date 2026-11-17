import React from 'react'

/** 十六进制字节视图：按 8 字节分组展示，可高亮指定字节区间。 */
export default function HexView({ hex, baseOff = 0, highlight = null }) {
  if (!hex) return null
  const bytes = []
  for (let i = 0; i < hex.length; i += 2) bytes.push(hex.slice(i, i + 2))
  return (
    <code className="hexview">
      {bytes.map((b, i) => {
        const abs = baseOff + i
        const hl = highlight && abs >= highlight[0] && abs < highlight[1]
        return (
          <span key={i} className={hl ? 'hex hl' : 'hex'} title={`偏移 ${abs}`}>
            {b}{' '}
          </span>
        )
      })}
    </code>
  )
}
