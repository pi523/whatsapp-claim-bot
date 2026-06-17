// whatsapp_bridge.js — standalone WhatsApp bridge for the Claim Bot.
//
// Connects to WhatsApp using whatsapp-web.js (scan a QR code once with your
// phone) and links it to the local claim service:
//   - private messages  -> POST {CLAIM_URL}/claim/incoming
//   - receipt photos     -> downloaded and POSTed to {CLAIM_URL}/claim/media
//   - monthly reports    -> drained from the proactive queue and sent to finance
//
// It deliberately ignores group chats. If the claim service is down, the
// HTTP call just fails and the message is left untouched.
//
// Requires Node.js 18+ (for the global fetch API).
// Run:  npm install  &&  npm start   (then scan the QR code)

const { Client, LocalAuth, MessageMedia } = require('whatsapp-web.js')
const qrcode = require('qrcode-terminal')
const fs = require('fs')
const path = require('path')
const os = require('os')

const CLAIM_URL = process.env.CLAIM_URL || 'http://127.0.0.1:5005'
const QUEUE_PATH =
  process.env.PROACTIVE_QUEUE_PATH || path.join(os.tmpdir(), 'wa_proactive_queue.json')

const client = new Client({
  authStrategy: new LocalAuth({ clientId: 'claim-bot' }),
  puppeteer: {
    headless: true,
    args: ['--no-sandbox', '--disable-setuid-sandbox'],
  },
})

client.on('qr', (qr) => {
  console.log('\nScan this QR code with WhatsApp (Linked devices):\n')
  qrcode.generate(qr, { small: true })
})

client.on('authenticated', () => console.log('[bridge] authenticated'))
client.on('auth_failure', (m) => console.error('[bridge] auth failure:', m))
client.on('disconnected', (r) => console.warn('[bridge] disconnected:', r))

client.on('ready', () => {
  console.log('[bridge] WhatsApp is ready. Listening for claim messages.')
  startQueueDrain()
})

async function postJson(url, body) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return res.json()
}

client.on('message', async (msg) => {
  try {
    // Private chats only — never touch group conversations.
    if (!msg.from || msg.from.endsWith('@g.us')) return

    const sender = msg.from
    const senderNumber = sender.split('@')[0] || ''

    const r = await postJson(`${CLAIM_URL}/claim/incoming`, {
      sender,
      text: msg.body || '',
      has_media: msg.hasMedia,
      sender_number: senderNumber,
    })

    // Not a claim message -> ignore (hand off to your own logic if you have one).
    if (!r || !r.handled) return

    // The service wants the receipt image: download it and report back.
    if (r.fetch_media && msg.hasMedia) {
      const media = await msg.downloadMedia()
      if (media && media.data) {
        const mime = media.mimetype || 'image/jpeg'
        const ext = (mime.split('/')[1] || 'jpg').split(';')[0]
        const tmp = path.join(os.tmpdir(), `claim_${sender.replace(/\W/g, '')}_${process.hrtime.bigint()}.${ext}`)
        fs.writeFileSync(tmp, Buffer.from(media.data, 'base64'))
        const mr = await postJson(`${CLAIM_URL}/claim/media`, {
          sender,
          file_path: tmp,
          mime,
        })
        if (mr && mr.reply) await msg.reply(mr.reply)
        return
      }
      await msg.reply("Sorry, the receipt didn't come through. Please send it again.")
      return
    }

    if (r.reply) await msg.reply(r.reply)
  } catch (e) {
    console.error('[bridge] message error:', e.message)
  }
})

// -- Drain the monthly-report queue and send files to finance -------------
// claim_export.py appends payloads to QUEUE_PATH; we send them here.
function startQueueDrain() {
  setInterval(async () => {
    let items
    try {
      if (!fs.existsSync(QUEUE_PATH)) return
      const raw = fs.readFileSync(QUEUE_PATH, 'utf8').trim()
      if (!raw) return
      items = JSON.parse(raw)
      if (!Array.isArray(items)) items = [items]
      if (!items.length) return
      fs.writeFileSync(QUEUE_PATH, '[]') // claim it before sending (avoid double-send)
    } catch (e) {
      return
    }

    for (const it of items) {
      try {
        const chatId = it.group_id
        if (!chatId) continue
        if (it.message) await client.sendMessage(chatId, it.message)
        for (const f of it.files || []) {
          if (f.path && fs.existsSync(f.path)) {
            const media = MessageMedia.fromFilePath(f.path)
            await client.sendMessage(chatId, media, { caption: f.caption })
          }
        }
      } catch (e) {
        console.error('[bridge] queue send failed:', e.message)
      }
    }
  }, 5000)
}

client.initialize()
