// claim-receipts — a tiny receipt-image hosting Worker.
// Routes:
//   POST /upload   upload an image (needs X-Auth secret), stored in KV with TTL, returns { url }
//   GET  /r/<id>   HTML viewer page (shows the receipt + this claim's info)
//   GET  /i/<id>   raw image bytes (used by the page's <img>)
// Images and metadata live in KV; on expiry (default 60 days) Cloudflare deletes them.

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]
  ))
}

function viewerHtml(id, info) {
  const amount = info.amount
    ? `${esc(info.currency || '')} ${esc(info.amount)}`.trim()
    : ''
  const line = [
    info.employee ? `<b>${esc(info.employee)}</b>` : '',
    esc(info.type || ''),
    amount,
    esc(info.date || ''),
  ].filter(Boolean).join(' · ')
  return `<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Receipt</title>
<style>
  body{margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f0f2f5;color:#222}
  .bar{background:#1F4E78;color:#fff;padding:12px 16px;font-size:15px;font-weight:600}
  .info{padding:10px 16px;font-size:14px;color:#444}
  .info b{color:#1F4E78}
  img{display:block;max-width:100%;height:auto;margin:0 auto;padding:8px}
</style></head><body>
  <div class="bar">📎 Receipt</div>
  <div class="info">${line}</div>
  <img src="/i/${id}" alt="receipt">
</body></html>`
}

function goneHtml() {
  return `<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Expired</title>
<style>body{font-family:-apple-system,sans-serif;text-align:center;padding:48px 20px;color:#555}</style>
</head><body><h2>🕒 This receipt link has expired</h2>
<p>Please ask for a fresh report — it will regenerate the link from the original on file.</p>
</body></html>`
}

function decodeMeta(b64) {
  try {
    const bin = atob(b64)
    const bytes = Uint8Array.from(bin, (c) => c.charCodeAt(0))
    return JSON.parse(new TextDecoder().decode(bytes))
  } catch (e) {
    return {}
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url)
    const path = url.pathname

    // Upload
    if (request.method === 'POST' && path === '/upload') {
      if (!env.UPLOAD_SECRET || request.headers.get('X-Auth') !== env.UPLOAD_SECRET) {
        return new Response('Forbidden', { status: 403 })
      }
      const days = parseInt(request.headers.get('X-TTL-Days') || '60', 10)
      const ttl = Math.max(60, (Number.isFinite(days) ? days : 60) * 24 * 3600)
      const ct = request.headers.get('Content-Type') || 'image/jpeg'
      const metaB64 = request.headers.get('X-Meta') || ''
      const id = crypto.randomUUID().replace(/-/g, '')
      const body = await request.arrayBuffer()
      if (!body || body.byteLength === 0) return new Response('Empty body', { status: 400 })

      await env.RECEIPTS.put(`img:${id}`, body, { expirationTtl: ttl, metadata: { ct } })
      await env.RECEIPTS.put(`meta:${id}`, metaB64 || '{}', { expirationTtl: ttl })
      return Response.json({ url: `${url.protocol}//${url.host}/r/${id}` })
    }

    // Raw image
    let m = path.match(/^\/i\/([a-f0-9]+)$/)
    if (request.method === 'GET' && m) {
      const { value, metadata } = await env.RECEIPTS.getWithMetadata(`img:${m[1]}`, { type: 'arrayBuffer' })
      if (!value) return new Response('Not found or expired', { status: 404 })
      return new Response(value, {
        headers: {
          'Content-Type': (metadata && metadata.ct) || 'image/jpeg',
          'Cache-Control': 'private, max-age=3600',
        },
      })
    }

    // Viewer page
    m = path.match(/^\/r\/([a-f0-9]+)$/)
    if (request.method === 'GET' && m) {
      const metaB64 = await env.RECEIPTS.get(`meta:${m[1]}`)
      if (metaB64 === null) {
        return new Response(goneHtml(), { status: 404, headers: { 'Content-Type': 'text/html; charset=utf-8' } })
      }
      return new Response(viewerHtml(m[1], decodeMeta(metaB64)), {
        headers: { 'Content-Type': 'text/html; charset=utf-8' },
      })
    }

    return new Response('claim-receipts OK')
  },
}
