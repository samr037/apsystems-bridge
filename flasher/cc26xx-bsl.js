// cc26xx-bsl.js — CC2652P ROM Serial Bootloader client over Web Serial.
//
// Reference: TI SWRA466 ("CC13xx / CC26xx Serial Bootloader Interface") and
// JelmerT/cc2538-bsl (Python). Byte-faithful to both.
//
// Wire format (host → target):
//   [size(1)] [checksum(1)] [opcode(1)] [payload...]
//   - size      : total packet length INCLUDING the 2 header bytes (8-bit, ≤ 255)
//   - checksum  : (sum of all bytes from opcode onward) & 0xFF
//   - First payload byte is the command opcode.
// ACK = 0x00 0xCC, NACK = 0x00 0x33. After a command packet the target replies
// with ACK/NACK. For commands that return data, the target then sends a
// response packet [size][checksum][data...] and expects the host to ACK it.
//
// Default baud: 500000 8N1. The Sonoff ZBDongle-P routes DTR/RTS through two
// NPN transistors to the CC2652P !RESET and IO15 (BSL) pins. See invokeBootloader.

// ---------------------------------------------------------------------------
// Opcodes — directly from cc2538-bsl / SWRA466.
// ---------------------------------------------------------------------------
export const OP = Object.freeze({
  PING:         0x20,
  DOWNLOAD:     0x21,
  RUN:          0x22,
  GET_STATUS:   0x23,
  SEND_DATA:    0x24,
  RESET:        0x25,
  SECTOR_ERASE: 0x26,
  CRC32:        0x27,
  GET_CHIP_ID:  0x28,
  MEMORY_READ:  0x2A,
  MEMORY_WRITE: 0x2B,
  BANK_ERASE:   0x2C,
});

// GET_STATUS return codes.
export const STATUS = Object.freeze({
  0x40: 'Success',
  0x41: 'Unknown command',
  0x42: 'Invalid command',
  0x43: 'Invalid address',
  0x44: 'Flash fail',
});

// Default per-command timeout (ms).
const DEFAULT_TIMEOUT_MS = 2000;
// Max bytes of *data* we can shove into one SEND_DATA packet: 252.
// (255 total packet limit − 2 header − 1 opcode = 252.)
export const MAX_SEND_DATA = 252;

// ---------------------------------------------------------------------------
// Pure-JS CRC32 — IEEE 802.3 (reflected poly 0xEDB88320). Same one used by
// Ethernet, ZIP, and the CC26xx ROM bootloader's CRC32 command. Table-driven.
// ---------------------------------------------------------------------------
const CRC32_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let i = 0; i < 256; i++) {
    let c = i;
    for (let k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
    t[i] = c >>> 0;
  }
  return t;
})();

export function crc32(bytes, seed = 0) {
  let c = (seed ^ 0xFFFFFFFF) >>> 0;
  for (let i = 0; i < bytes.length; i++) {
    c = (CRC32_TABLE[(c ^ bytes[i]) & 0xFF] ^ (c >>> 8)) >>> 0;
  }
  return (c ^ 0xFFFFFFFF) >>> 0;
}

// ---------------------------------------------------------------------------
// Helpers: big-endian 32-bit packing.
// ---------------------------------------------------------------------------
function u32be(value) {
  const v = value >>> 0;
  return new Uint8Array([(v >>> 24) & 0xFF, (v >>> 16) & 0xFF, (v >>> 8) & 0xFF, v & 0xFF]);
}

function u32beFrom(buf, offset = 0) {
  return (
    ((buf[offset] << 24) >>> 0) |
    (buf[offset + 1] << 16) |
    (buf[offset + 2] << 8) |
    buf[offset + 3]
  ) >>> 0;
}

function hex(byteOrU32, width = 2) {
  return '0x' + (byteOrU32 >>> 0).toString(16).toUpperCase().padStart(width, '0');
}

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

// ---------------------------------------------------------------------------
// SerialReader — buffered byte reader with timeouts.
//
// Web Serial gives us a ReadableStream of Uint8Array chunks. We hold a single
// reader for the lifetime of the BSL session and pull bytes out of an internal
// buffer with `read(n, timeoutMs)`. If the stream returns before we've satisfied
// n bytes, we keep pulling. If the deadline elapses, we throw a TimeoutError.
// ---------------------------------------------------------------------------
class SerialReader {
  constructor(port) {
    this._port = port;
    this._reader = port.readable.getReader();
    this._buf = new Uint8Array(0);
    this._closed = false;
  }

  async _pumpOnce(timeoutMs) {
    // Race the reader against a timeout.
    let timeoutHandle;
    const timeout = new Promise((_, reject) => {
      timeoutHandle = setTimeout(() => reject(new Error('timeout')), timeoutMs);
    });
    try {
      const { value, done } = await Promise.race([this._reader.read(), timeout]);
      clearTimeout(timeoutHandle);
      if (done) { this._closed = true; return false; }
      if (value && value.length) {
        const merged = new Uint8Array(this._buf.length + value.length);
        merged.set(this._buf, 0);
        merged.set(value, this._buf.length);
        this._buf = merged;
      }
      return true;
    } catch (e) {
      clearTimeout(timeoutHandle);
      throw e;
    }
  }

  /** Read exactly n bytes, or throw if the total deadline elapses. */
  async read(n, timeoutMs = DEFAULT_TIMEOUT_MS) {
    const deadline = performance.now() + timeoutMs;
    while (this._buf.length < n) {
      const remaining = deadline - performance.now();
      if (remaining <= 0) throw new Error(`timeout waiting for ${n} byte(s) (have ${this._buf.length})`);
      await this._pumpOnce(remaining);
      if (this._closed && this._buf.length < n) throw new Error('serial stream closed');
    }
    const out = this._buf.slice(0, n);
    this._buf = this._buf.slice(n);
    return out;
  }

  /** Drain whatever's buffered AND whatever arrives within ms (best-effort). */
  async drain(ms = 50) {
    try {
      const deadline = performance.now() + ms;
      while (performance.now() < deadline) {
        const r = deadline - performance.now();
        if (r <= 0) break;
        try { await this._pumpOnce(r); } catch { break; }
      }
    } catch { /* ignore */ }
    this._buf = new Uint8Array(0);
  }

  async release() {
    try { await this._reader.cancel(); } catch { /* ignore */ }
    try { this._reader.releaseLock(); } catch { /* ignore */ }
  }
}

// ---------------------------------------------------------------------------
// SerialWriter — simple raw byte writer. We grab a writer per write so we
// never leave the writable stream locked between commands.
// ---------------------------------------------------------------------------
async function writeBytes(port, bytes) {
  const writer = port.writable.getWriter();
  try {
    await writer.write(bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes));
  } finally {
    writer.releaseLock();
  }
}

// ---------------------------------------------------------------------------
// CC26xxBSL — the BSL client.
// ---------------------------------------------------------------------------
export class CC26xxBSL {
  /**
   * @param {SerialPort} port - already opened Web Serial port.
   * @param {(level: 'info'|'warn'|'error'|'ok'|'debug', msg: string) => void} log
   */
  constructor(port, log = () => {}) {
    this.port = port;
    this.log = log;
    this.reader = new SerialReader(port);
  }

  // -------------------------------------------------------------------------
  // Framing.
  // -------------------------------------------------------------------------
  _frame(payload) {
    // payload[0] = opcode, payload[1..] = args. Checksum = sum(payload) & 0xFF.
    if (payload.length > 253) throw new Error(`payload too large: ${payload.length}`);
    const size = payload.length + 2; // total packet length
    let checksum = 0;
    for (let i = 0; i < payload.length; i++) checksum = (checksum + payload[i]) & 0xFF;
    const out = new Uint8Array(size);
    out[0] = size;
    out[1] = checksum;
    out.set(payload, 2);
    return out;
  }

  async _writeRaw(bytes) {
    await writeBytes(this.port, bytes);
  }

  // Read ACK/NACK (0x00 0xCC or 0x00 0x33). cc2538-bsl tolerates leading 0x00
  // bytes — we do the same: skip 0x00s, then expect 0xCC or 0x33.
  async _readAck(timeoutMs = DEFAULT_TIMEOUT_MS) {
    const deadline = performance.now() + timeoutMs;
    for (;;) {
      const remaining = deadline - performance.now();
      if (remaining <= 0) throw new Error('timeout waiting for ACK');
      const b = await this.reader.read(1, remaining);
      if (b[0] === 0x00) continue;            // padding
      if (b[0] === 0xCC) return true;          // ACK
      if (b[0] === 0x33) return false;         // NACK
      // Anything else: still tolerant — skip until we see ACK/NACK or timeout.
    }
  }

  async _sendAck() { await this._writeRaw(new Uint8Array([0x00, 0xCC])); }
  // (NACK send not needed in our flow.)

  // Send a framed command packet and require ACK.
  async _sendCommand(payload, timeoutMs = DEFAULT_TIMEOUT_MS) {
    const frame = this._frame(payload);
    await this._writeRaw(frame);
    const ok = await this._readAck(timeoutMs);
    if (!ok) throw new Error(`NACK for opcode ${hex(payload[0])}`);
  }

  // Receive a response packet: [size][checksum][data...]. ACK it.
  async _recvResponse(timeoutMs = DEFAULT_TIMEOUT_MS) {
    // Per SWRA466, response is preceded by 0x00 padding. Skip 0x00s until we
    // see a non-zero size byte.
    const deadline = performance.now() + timeoutMs;
    let size = 0;
    for (;;) {
      const remaining = deadline - performance.now();
      if (remaining <= 0) throw new Error('timeout waiting for response');
      const b = await this.reader.read(1, remaining);
      if (b[0] === 0x00) continue;
      size = b[0];
      break;
    }
    if (size < 2) throw new Error(`bogus response size: ${size}`);
    const cksum = (await this.reader.read(1, timeoutMs))[0];
    const data = await this.reader.read(size - 2, timeoutMs);
    let s = 0;
    for (let i = 0; i < data.length; i++) s = (s + data[i]) & 0xFF;
    if (s !== cksum) throw new Error(`response checksum mismatch: got ${hex(cksum)}, expected ${hex(s)}`);
    await this._sendAck();
    return data;
  }

  // -------------------------------------------------------------------------
  // Bootloader entry — Sonoff ZBDongle-P specific (CP2102N → 2 NPN gates).
  //
  //   DTR  RTS  ->  RST  IO15
  //    1    1   ->   1    1
  //    0    0   ->   1    1
  //    1    0   ->   0    1   (chip held in reset)
  //    0    1   ->   1    0   (released; IO15 low at boot → ROM BSL)
  //
  // Web Serial: port.setSignals({dataTerminalReady, requestToSend}). Note that
  // the boolean here is the *logical line state* the host drives; the NPN
  // transistor inverts it on the way to the chip pin, so this table matches
  // cc2538-bsl's `sonoff_usb` sequence verbatim.
  // -------------------------------------------------------------------------
  async invokeBootloader() {
    this.log('info', 'Invoking ROM bootloader via DTR/RTS toggle (Sonoff sequence)...');
    // Idle.
    await this.port.setSignals({ dataTerminalReady: true, requestToSend: true });
    await sleep(100);
    // Release both — both pins high.
    await this.port.setSignals({ dataTerminalReady: false, requestToSend: false });
    await sleep(100);
    // DTR=1, RTS=0 → hold RST low, IO15 high.
    await this.port.setSignals({ dataTerminalReady: true, requestToSend: false });
    await sleep(100);
    // DTR=0, RTS=1 → release RST, IO15 low at boot = enter ROM BSL.
    await this.port.setSignals({ dataTerminalReady: false, requestToSend: true });
    await sleep(150);
    // Idle the lines so they don't keep driving the gates.
    await this.port.setSignals({ dataTerminalReady: false, requestToSend: false });
    await sleep(100);
    // Drain anything the bootloader might have spat out during the toggle.
    await this.reader.drain(50);
    this.log('ok', 'Bootloader entry signals issued.');
  }

  // -------------------------------------------------------------------------
  // Initial sync: send 0x55 0x55 until the target ACKs. cc2538-bsl tries this
  // up to ~5 times before giving up.
  // -------------------------------------------------------------------------
  async sync({ retries = 5, perAttemptMs = 500 } = {}) {
    for (let attempt = 1; attempt <= retries; attempt++) {
      this.log('debug', `sync attempt ${attempt}/${retries}`);
      try {
        await this._writeRaw(new Uint8Array([0x55, 0x55]));
        const ok = await this._readAck(perAttemptMs);
        if (ok) { this.log('ok', 'Sync ACK received.'); return; }
        this.log('warn', `sync attempt ${attempt}: NACK`);
      } catch (e) {
        this.log('warn', `sync attempt ${attempt}: ${e.message}`);
      }
      await sleep(100);
    }
    throw new Error('Failed to sync with bootloader (no ACK to 0x55 0x55). Replug the dongle and try again.');
  }

  // -------------------------------------------------------------------------
  // Commands.
  // -------------------------------------------------------------------------
  async ping() {
    await this._sendCommand(new Uint8Array([OP.PING]));
    this.log('ok', 'PING ACKed.');
  }

  async getStatus() {
    await this._sendCommand(new Uint8Array([OP.GET_STATUS]));
    const resp = await this._recvResponse();
    if (resp.length < 1) throw new Error('empty GET_STATUS response');
    return resp[0];
  }

  async _expectStatusOK(context) {
    const s = await this.getStatus();
    if (s !== 0x40) {
      const name = STATUS[s] || `unknown ${hex(s)}`;
      throw new Error(`${context}: status ${hex(s)} (${name})`);
    }
    this.log('debug', `${context}: status OK (0x40)`);
  }

  async getChipId() {
    await this._sendCommand(new Uint8Array([OP.GET_CHIP_ID]));
    const resp = await this._recvResponse();
    // 4-byte chip ID, big-endian. cc2538-bsl maps these to friendly names; we
    // just surface the raw value in the log and don't hard-fail on unknown.
    if (resp.length < 4) throw new Error(`short GET_CHIP_ID response (${resp.length} bytes)`);
    const chipId = u32beFrom(resp, 0);
    this.log('info', `Chip ID: ${hex(chipId, 8)}`);
    // Heuristic: CC26x2/CC13x2 ROM bootloader chip IDs usually begin with 0xB964/0xB965.
    // We don't hard-gate; we just warn if it looks unfamiliar.
    const high = (chipId >>> 16) & 0xFFFF;
    if (high !== 0xB964 && high !== 0xB965) {
      this.log('warn', `Chip ID prefix ${hex(high, 4)} is not a known CC26x2/CC13x2 family marker — proceeding anyway.`);
    } else {
      this.log('ok', 'Chip ID looks like a CC26x2/CC13x2 part.');
    }
    return chipId;
  }

  async bankErase() {
    this.log('info', 'Erasing main flash bank (BANK_ERASE)...');
    await this._sendCommand(new Uint8Array([OP.BANK_ERASE]), 10000);
    await this._expectStatusOK('BANK_ERASE');
    this.log('ok', 'Flash bank erased.');
  }

  async download(addr, size) {
    this.log('info', `DOWNLOAD addr=${hex(addr, 8)} size=${size} bytes`);
    const payload = new Uint8Array(1 + 4 + 4);
    payload[0] = OP.DOWNLOAD;
    payload.set(u32be(addr), 1);
    payload.set(u32be(size), 5);
    await this._sendCommand(payload);
    await this._expectStatusOK('DOWNLOAD');
  }

  /**
   * Send one SEND_DATA chunk. Max 252 data bytes.
   * The bootloader writes the chunk to the address established by DOWNLOAD,
   * advancing internally.
   */
  async sendData(chunk) {
    if (chunk.length === 0 || chunk.length > MAX_SEND_DATA) {
      throw new Error(`sendData chunk size must be 1..${MAX_SEND_DATA}, got ${chunk.length}`);
    }
    const payload = new Uint8Array(1 + chunk.length);
    payload[0] = OP.SEND_DATA;
    payload.set(chunk, 1);
    await this._sendCommand(payload, 4000);
    await this._expectStatusOK('SEND_DATA');
  }

  async crc32(addr, size, readCount = 0) {
    this.log('info', `Computing on-device CRC32 over ${size} bytes @ ${hex(addr, 8)}...`);
    const payload = new Uint8Array(1 + 4 + 4 + 4);
    payload[0] = OP.CRC32;
    payload.set(u32be(addr), 1);
    payload.set(u32be(size), 5);
    payload.set(u32be(readCount), 9);
    await this._sendCommand(payload, 30000); // can take a while on big images
    const resp = await this._recvResponse(30000);
    if (resp.length < 4) throw new Error(`short CRC32 response (${resp.length} bytes)`);
    return u32beFrom(resp, 0);
  }

  async reset() {
    this.log('info', 'Resetting chip into application...');
    // RESET doesn't return a status — the chip just resets after ACKing.
    await this._sendCommand(new Uint8Array([OP.RESET]));
    this.log('ok', 'RESET issued.');
  }

  // -------------------------------------------------------------------------
  // Cleanup — release the reader/lock so the page can re-open the port.
  // -------------------------------------------------------------------------
  async close() {
    await this.reader.release();
  }
}
