'use strict';

// System resource stats + connected-agent list for the resources overlay.
// Runs in the Electron main process.
//
// Resource metrics (CPU/memory/disk/network) come from the OS. The agent list
// comes from the local Agent Registry service (skills/agent-registry) — the
// overlay shows precisely which Claude Code / Kimi Code instances have
// registered, not a fuzzy process scan.

const os = require('os');
const { execSync } = require('child_process');

let lastCpu = null; // { idle, total }
let lastNet = null; // { rx, tx, ts }

function cpuSnapshot() {
  let idle = 0;
  let total = 0;
  for (const cpu of os.cpus()) {
    for (const v of Object.values(cpu.times)) total += v;
    idle += cpu.times.idle;
  }
  return { idle, total };
}

function cpuPercent() {
  const snap = cpuSnapshot();
  if (!lastCpu) {
    lastCpu = snap;
    return 0;
  }
  const idleDelta = snap.idle - lastCpu.idle;
  const totalDelta = snap.total - lastCpu.total;
  lastCpu = snap;
  if (totalDelta <= 0) return 0;
  return Math.min(100, Math.max(0, 100 * (1 - idleDelta / totalDelta)));
}

// One shell round-trip for disk usage, cumulative network bytes, memory pressure.
function readShell() {
  const script = `
    iface=$(route -n get default 2>/dev/null | awk '/interface:/{print $2}')
    df -k / | awk 'NR==2{print "disk " $3 " " $2}'
    netstat -ibn 2>/dev/null | awk -v i="$iface" '$1==i{print "net " $7 " " $10; exit}'
    vm_stat 2>/dev/null | awk '/page size of/{print "vmpagesize " $8} /^Pages/{gsub(/[.:]/,"",$NF); print "vm " $0}'
  `;
  return execSync(script, { shell: '/bin/bash', timeout: 4000 }).toString();
}

// macOS "memory used" ~= (active + wired + compressed) pages.
function memoryFromVmStat(pageSize, pages) {
  return (
    ((pages.active || 0) + (pages.wired || 0) + (pages.compressor || 0)) *
    pageSize
  );
}

async function getStats() {
  const now = Date.now();
  const cpu = cpuPercent();

  const totalMem = os.totalmem();
  let usedMem = totalMem - os.freemem();

  let disk = null;
  let net = { rxRate: 0, txRate: 0 };

  try {
    const out = readShell();
    let pageSize = 4096;
    const vmPages = {};

    for (const line of out.split('\n')) {
      if (line.startsWith('disk ')) {
        const [, used, total] = line.split(/\s+/);
        disk = { used: +used * 1024, total: +total * 1024 };
      } else if (line.startsWith('net ')) {
        const [, rx, tx] = line.split(/\s+/);
        const cur = { rx: +rx, tx: +tx, ts: now };
        if (lastNet) {
          const dt = (cur.ts - lastNet.ts) / 1000 || 1;
          net = {
            rxRate: Math.max(0, (cur.rx - lastNet.rx) / dt),
            txRate: Math.max(0, (cur.tx - lastNet.tx) / dt),
          };
        }
        lastNet = cur;
      } else if (line.startsWith('vmpagesize ')) {
        pageSize = parseInt(line.split(/\s+/)[1], 10) || 4096;
      } else if (line.startsWith('vm Pages ')) {
        const m = line.match(/^vm Pages (.+?):?\s+(\d+)$/);
        if (m) {
          const label = m[1];
          const n = parseInt(m[2], 10);
          if (label === 'active') vmPages.active = n;
          else if (label.includes('wired')) vmPages.wired = n;
          else if (label.includes('compressor')) vmPages.compressor = n;
        }
      }
    }
    if (vmPages.active || vmPages.wired) {
      usedMem = memoryFromVmStat(pageSize, vmPages);
    }
  } catch (err) {
    console.error('[stats] shell read failed:', err.message);
  }

  return {
    ts: new Date(now).toISOString(),
    cpu: { percent: Math.round(cpu * 10) / 10, cores: os.cpus().length },
    memory: { used: usedMem, free: totalMem - usedMem, total: totalMem },
    load: os.loadavg(),
    disk,
    network: net,
    uptime: os.uptime(),
  };
}

module.exports = { getStats };
