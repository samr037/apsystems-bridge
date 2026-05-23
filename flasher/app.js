// app.js — UI + flow orchestration for the APsystems Open Bridge Web Flasher.
//
// Vanilla ES module. No framework, no bundler, no npm.

import { CC26xxBSL, MAX_SEND_DATA, crc32 } from './cc26xx-bsl.js';

// ---------------------------------------------------------------------------
// DOM handles.
// ---------------------------------------------------------------------------
const $ = (id) => document.getElementById(id);
const els = {
  unsupported:    $('unsupported'),
  app:            $('app'),
  btnConnect:     $('btn-connect'),
  btnFlash:       $('btn-flash'),
  status:         $('status'),
  fwSelect:       $('fw-select'),
  fwMeta:         $('fw-meta'),
  progressFill:   $('progress-fill'),
  progressLabel:  $('progress-label'),
  log:            $('log'),
};

// ---------------------------------------------------------------------------
// State.
// ---------------------------------------------------------------------------
const state = {
  port: null,
  manifest: null,
  fwIndex: 0,
  flashing: false,
};

// ---------------------------------------------------------------------------
// Log helpers.
// ---------------------------------------------------------------------------
function log(level, msg) {
  const line = document.createElement('div');
  line.className = `log-line log-${level}`;
  const ts = new Date().toISOString().slice(11, 19);
  line.textContent = `[${ts}] ${msg}`;
  els.log.appendChild(line);
  els.log.scrollTop = els.log.scrollHeight;
  // Mirror to devtools for power users.
  const fn = { error: 'error', warn: 'warn', ok: 'log', info: 'log', debug: 'debug' }[level] || 'log';
  console[fn](`[flasher] ${msg}`);
}

function setStatus(text, kind = 'idle') {
  els.status.textContent = text;
  els.status.dataset.kind = kind;
}

function setProgress(done, total) {
  if (total > 0) {
    const pct = Math.min(100, Math.round((done / total) * 100));
    els.progressFill.style.width = pct + '%';
    els.progressLabel.textContent = `${pct}% (${done} / ${total} bytes)`;
  } else {
    els.progressFill.style.width = '0%';
    els.progressLabel.textContent = '';
  }
}

// ---------------------------------------------------------------------------
// Feature detection.
// ---------------------------------------------------------------------------
function featureCheck() {
  if (!('serial' in navigator)) {
    els.unsupported.hidden = false;
    els.app.hidden = true;
    return false;
  }
  return true;
}

// ---------------------------------------------------------------------------
// Manifest loading.
// ---------------------------------------------------------------------------
async function loadManifest() {
  try {
    const res = await fetch('./manifest.json', { cache: 'no-cache' });
    if (!res.ok) throw new Error(`manifest HTTP ${res.status}`);
    state.manifest = await res.json();
    if (!state.manifest.firmware || state.manifest.firmware.length === 0) {
      throw new Error('manifest has no firmware entries');
    }
    populateFirmwareSelect();
    log('ok', `Manifest loaded: ${state.manifest.name} (${state.manifest.firmware.length} build(s))`);
  } catch (e) {
    log('error', `Failed to load manifest.json: ${e.message}`);
  }
}

function populateFirmwareSelect() {
  els.fwSelect.innerHTML = '';
  state.manifest.firmware.forEach((fw, i) => {
    const opt = document.createElement('option');
    opt.value = String(i);
    opt.textContent = `${fw.name} — ${fw.version}`;
    els.fwSelect.appendChild(opt);
  });
  state.fwIndex = 0;
  updateFwMeta();
}

function updateFwMeta() {
  const fw = state.manifest.firmware[state.fwIndex];
  const addr = fw.address || '0x00000000';
  const crcText = fw.crc32 == null ? '(none — skip pre-verify)' : fw.crc32;
  els.fwMeta.innerHTML = `
    <div><strong>URL:</strong> <code>${fw.url}</code></div>
    <div><strong>Address:</strong> <code>${addr}</code> &nbsp; <strong>CRC32:</strong> <code>${crcText}</code></div>
  `;
}

// ---------------------------------------------------------------------------
// Connect / disconnect.
// ---------------------------------------------------------------------------
async function onConnect() {
  if (state.port) {
    // Disconnect path.
    try { await state.port.close(); } catch { /* ignore */ }
    state.port = null;
    setStatus('Disconnected', 'idle');
    els.btnConnect.textContent = 'Connect';
    els.btnFlash.disabled = true;
    log('info', 'Port closed.');
    return;
  }

  try {
    const port = await navigator.serial.requestPort();
    await port.open({ baudRate: 500000, dataBits: 8, stopBits: 1, parity: 'none', flowControl: 'none' });
    // Drive DTR/RTS to a safe idle (both deasserted) so we don't accidentally
    // hold the chip in reset.
    await port.setSignals({ dataTerminalReady: false, requestToSend: false });
    state.port = port;
    setStatus('Connected (500000 8N1)', 'ok');
    els.btnConnect.textContent = 'Disconnect';
    els.btnFlash.disabled = false;
    const info = port.getInfo ? port.getInfo() : {};
    log('ok', `Port opened. USB ${info.usbVendorId ? '0x' + info.usbVendorId.toString(16) : '?'}:${info.usbProductId ? '0x' + info.usbProductId.toString(16) : '?'} @ 500000 8N1.`);
  } catch (e) {
    if (e.name === 'NotFoundError') {
      log('warn', 'No port selected.');
    } else {
      log('error', `Connect failed: ${e.message}`);
    }
  }
}

// ---------------------------------------------------------------------------
// Firmware fetch.
// ---------------------------------------------------------------------------
async function fetchFirmware(fw) {
  log('info', `Fetching firmware from ${fw.url} ...`);
  const res = await fetch(fw.url, { cache: 'no-cache', mode: 'cors' });
  if (!res.ok) throw new Error(`firmware HTTP ${res.status}`);
  const buf = new Uint8Array(await res.arrayBuffer());
  log('ok', `Firmware downloaded: ${buf.length} bytes.`);
  return buf;
}

// ---------------------------------------------------------------------------
// Flash flow.
// ---------------------------------------------------------------------------
async function onFlash() {
  if (state.flashing) return;
  if (!state.port) { log('error', 'Not connected.'); return; }
  state.flashing = true;
  els.btnFlash.disabled = true;
  els.btnConnect.disabled = true;
  setProgress(0, 0);

  let bsl = null;
  try {
    const fw = state.manifest.firmware[state.fwIndex];

    // 1. Fetch firmware.
    const image = await fetchFirmware(fw);

    // 2. Local CRC32.
    const localCrc = crc32(image);
    log('info', `Local CRC32 of image: 0x${localCrc.toString(16).toUpperCase().padStart(8, '0')}`);

    // 3. Optional pre-verify vs. manifest's crc32 field.
    if (fw.crc32 != null) {
      const expected = (typeof fw.crc32 === 'string')
        ? parseInt(fw.crc32, 16) >>> 0
        : (fw.crc32 >>> 0);
      if (localCrc !== expected) {
        throw new Error(`manifest CRC32 mismatch: image=0x${localCrc.toString(16)} expected=0x${expected.toString(16)}`);
      }
      log('ok', 'Manifest CRC32 matches downloaded image.');
    }

    const addr = (typeof fw.address === 'string') ? (parseInt(fw.address, 16) >>> 0) : (fw.address >>> 0);

    // 4. Talk to the bootloader.
    bsl = new CC26xxBSL(state.port, log);
    setStatus('Entering bootloader...', 'busy');
    await bsl.invokeBootloader();

    setStatus('Syncing...', 'busy');
    await bsl.sync();

    await bsl.ping();
    const chipId = await bsl.getChipId();
    setStatus(`Bootloader OK — chip 0x${chipId.toString(16).toUpperCase().padStart(8, '0')}`, 'busy');

    // 5. Erase.
    setStatus('Erasing flash...', 'busy');
    await bsl.bankErase();

    // 6. Download header.
    setStatus('Programming...', 'busy');
    await bsl.download(addr, image.length);

    // 7. Stream chunks.
    let sent = 0;
    setProgress(0, image.length);
    while (sent < image.length) {
      const remaining = image.length - sent;
      const chunkSize = Math.min(MAX_SEND_DATA, remaining);
      const chunk = image.subarray(sent, sent + chunkSize);
      await bsl.sendData(chunk);
      sent += chunkSize;
      setProgress(sent, image.length);
      // Log every ~32 KiB to keep the log informative but not spammy.
      if ((sent % 32768) < MAX_SEND_DATA || sent === image.length) {
        log('info', `... programmed ${sent} / ${image.length} bytes`);
      }
    }
    log('ok', `All ${image.length} bytes programmed.`);

    // 8. Verify with on-device CRC32.
    setStatus('Verifying CRC32...', 'busy');
    const deviceCrc = await bsl.crc32(addr, image.length, 0);
    const deviceCrcHex = '0x' + deviceCrc.toString(16).toUpperCase().padStart(8, '0');
    const localCrcHex  = '0x' + localCrc.toString(16).toUpperCase().padStart(8, '0');
    if (deviceCrc !== localCrc) {
      throw new Error(`CRC32 mismatch: device=${deviceCrcHex} host=${localCrcHex}`);
    }
    log('ok', `CRC32 verified: ${deviceCrcHex}`);

    // 9. Reset into application.
    await bsl.reset();
    setStatus('Done — chip running new firmware', 'ok');
    log('ok', 'Flashing complete. Unplug / replug the dongle if your host software needs to re-enumerate.');
  } catch (e) {
    log('error', `Flash failed: ${e.message}`);
    setStatus('Error — see log', 'err');
  } finally {
    // Tear down BSL session cleanly so the port can be reused.
    if (bsl) { try { await bsl.close(); } catch { /* ignore */ } }
    state.flashing = false;
    els.btnConnect.disabled = false;
    els.btnFlash.disabled = !state.port;
  }
}

// ---------------------------------------------------------------------------
// Bootstrap.
// ---------------------------------------------------------------------------
async function main() {
  if (!featureCheck()) return;
  els.btnConnect.addEventListener('click', onConnect);
  els.btnFlash.addEventListener('click', onFlash);
  els.fwSelect.addEventListener('change', () => {
    state.fwIndex = parseInt(els.fwSelect.value, 10) || 0;
    updateFwMeta();
  });
  // If the user yanks the dongle mid-session, surface it.
  navigator.serial.addEventListener('disconnect', (e) => {
    if (state.port && e.target === state.port) {
      log('warn', 'Serial device disconnected.');
      setStatus('Disconnected', 'idle');
      els.btnConnect.textContent = 'Connect';
      els.btnFlash.disabled = true;
      state.port = null;
    }
  });
  await loadManifest();
  log('info', 'Ready. Plug the Sonoff ZBDongle-P, click Connect, pick the USB serial device, then click Flash.');
}

main();
