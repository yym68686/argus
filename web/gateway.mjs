import crypto from 'node:crypto'
import http from 'node:http'
import net from 'node:net'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const listenHost = process.env.LISTEN_HOST ?? '127.0.0.1'
const listenPort = Number(process.env.LISTEN_PORT ?? '8080')

const codexHost = process.env.CODEX_TCP_HOST ?? '127.0.0.1'
const codexPort = Number(process.env.CODEX_TCP_PORT ?? '7777')

const htmlPath = path.join(
  path.dirname(fileURLToPath(import.meta.url)),
  'chat.html'
)
const html = readFileSync(htmlPath)

function wsAcceptKey(key) {
  const magic = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
  return crypto.createHash('sha1').update(key + magic).digest('base64')
}

function wsSendText(socket, text) {
  const payload = Buffer.from(text, 'utf8')
  const len = payload.length

  let header
  if (len < 126) {
    header = Buffer.alloc(2)
    header[0] = 0x81
    header[1] = len
  } else if (len < 65536) {
    header = Buffer.alloc(4)
    header[0] = 0x81
    header[1] = 126
    header.writeUInt16BE(len, 2)
  } else {
    header = Buffer.alloc(10)
    header[0] = 0x81
    header[1] = 127
    header.writeBigUInt64BE(BigInt(len), 2)
  }

  socket.write(Buffer.concat([header, payload]))
}

function wsSendClose(socket, code = 1000, reason = '') {
  const reasonBuf = Buffer.from(reason, 'utf8')
  const payload = Buffer.alloc(2 + reasonBuf.length)
  payload.writeUInt16BE(code, 0)
  reasonBuf.copy(payload, 2)

  const len = payload.length
  const header = Buffer.alloc(2)
  header[0] = 0x88
  header[1] = len
  socket.write(Buffer.concat([header, payload]))
}

function parseWsFrames(buffer, onText, onClose, onPing) {
  let buf = buffer
  const messages = []

  while (buf.length >= 2) {
    const b0 = buf[0]
    const b1 = buf[1]
    const fin = (b0 & 0x80) !== 0
    const opcode = b0 & 0x0f
    const masked = (b1 & 0x80) !== 0
    let payloadLen = b1 & 0x7f
    let offset = 2

    if (!fin) {
      throw new Error('Fragmented frames are not supported')
    }

    if (payloadLen === 126) {
      if (buf.length < offset + 2) break
      payloadLen = buf.readUInt16BE(offset)
      offset += 2
    } else if (payloadLen === 127) {
      if (buf.length < offset + 8) break
      const bigLen = buf.readBigUInt64BE(offset)
      if (bigLen > BigInt(Number.MAX_SAFE_INTEGER)) {
        throw new Error('Frame too large')
      }
      payloadLen = Number(bigLen)
      offset += 8
    }

    const maskLen = masked ? 4 : 0
    if (buf.length < offset + maskLen + payloadLen) break

    let mask
    if (masked) {
      mask = buf.subarray(offset, offset + 4)
      offset += 4
    }

    let payload = buf.subarray(offset, offset + payloadLen)
    buf = buf.subarray(offset + payloadLen)

    if (masked) {
      const out = Buffer.alloc(payload.length)
      for (let i = 0; i < payload.length; i++) {
        out[i] = payload[i] ^ mask[i % 4]
      }
      payload = out
    }

    if (opcode === 0x1) {
      messages.push(payload.toString('utf8'))
      continue
    }
    if (opcode === 0x8) {
      onClose?.()
      return { remaining: Buffer.alloc(0), messages }
    }
    if (opcode === 0x9) {
      onPing?.(payload)
      continue
    }
    if (opcode === 0xa) {
      continue
    }

    throw new Error(`Unsupported opcode: ${opcode}`)
  }

  for (const m of messages) onText(m)
  return { remaining: buf, messages: [] }
}

const server = http.createServer((req, res) => {
  if (!req.url || req.url === '/' || req.url.startsWith('/chat')) {
    res.writeHead(200, {
      'content-type': 'text/html; charset=utf-8',
      'cache-control': 'no-store',
    })
    res.end(html)
    return
  }

  res.writeHead(404, { 'content-type': 'text/plain; charset=utf-8' })
  res.end('Not found')
})

server.on('upgrade', (req, socket, head) => {
  if (req.url !== '/ws') {
    socket.destroy()
    return
  }

  const key = req.headers['sec-websocket-key']
  if (typeof key !== 'string') {
    socket.destroy()
    return
  }

  const accept = wsAcceptKey(key)
  socket.write(
    [
      'HTTP/1.1 101 Switching Protocols',
      'Upgrade: websocket',
      'Connection: Upgrade',
      `Sec-WebSocket-Accept: ${accept}`,
      '',
      '',
    ].join('\r\n')
  )

  const codex = net.createConnection({ host: codexHost, port: codexPort })
  codex.setNoDelay(true)

  let wsBuf = head && head.length ? Buffer.from(head) : Buffer.alloc(0)
  let codexBuf = ''
  let closed = false

  function closeBoth() {
    if (closed) return
    closed = true
    try {
      wsSendClose(socket)
    } catch {}
    try {
      socket.destroy()
    } catch {}
    try {
      codex.destroy()
    } catch {}
  }

  codex.on('data', (chunk) => {
    codexBuf += chunk.toString('utf8')
    while (true) {
      const idx = codexBuf.indexOf('\n')
      if (idx === -1) break
      const line = codexBuf.slice(0, idx).trimEnd()
      codexBuf = codexBuf.slice(idx + 1)
      if (!line) continue
      wsSendText(socket, line)
    }
  })

  codex.on('error', () => closeBoth())
  codex.on('close', () => closeBoth())

  socket.on('data', (chunk) => {
    if (closed) return
    wsBuf = Buffer.concat([wsBuf, chunk])
    try {
      const result = parseWsFrames(
        wsBuf,
        (text) => {
          if (!text.endsWith('\n')) text += '\n'
          codex.write(text)
        },
        () => closeBoth(),
        (payload) => {
          const len = payload.length
          const header = Buffer.alloc(len < 126 ? 2 : 4)
          header[0] = 0x8a
          if (len < 126) {
            header[1] = len
            socket.write(Buffer.concat([header, payload]))
          } else {
            header[1] = 126
            header.writeUInt16BE(len, 2)
            socket.write(Buffer.concat([header, payload]))
          }
        }
      )
      wsBuf = result.remaining
    } catch {
      closeBoth()
    }
  })

  socket.on('error', () => closeBoth())
  socket.on('close', () => closeBoth())
})

server.listen(listenPort, listenHost, () => {
  console.log(
    `Gateway listening on http://${listenHost}:${listenPort} (ws: /ws) → TCP ${codexHost}:${codexPort}`
  )
})
