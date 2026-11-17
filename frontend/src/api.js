const BASE = ''

async function req(method, path, body, raw) {
  const opt = { method, headers: {} }
  if (body !== undefined) {
    opt.headers['Content-Type'] = 'application/json'
    opt.body = JSON.stringify(body)
  }
  if (raw !== undefined) {
    opt.headers['Content-Type'] = 'application/octet-stream'
    opt.body = raw
  }
  const resp = await fetch(BASE + path, opt)
  if (!resp.ok) {
    let detail = resp.statusText
    try { detail = (await resp.json()).detail || detail } catch { /* ignore */ }
    throw new Error(`${resp.status}: ${detail}`)
  }
  return resp.json()
}

export const api = {
  get: (p) => req('GET', p),
  post: (p, body) => req('POST', p, body),
  postRaw: (p, raw) => req('POST', p, undefined, raw),
}
