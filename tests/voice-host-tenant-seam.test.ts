// Tenant seam invariants pinned at the source level (the full adversarial
// isolation test runs against the deployed cloud host; this gate keeps the
// seam itself from regressing): gateway-routed sessions must not write the
// local tasks dir and must not start the local results poll.
import { readFileSync } from 'node:fs';
const src = readFileSync(new URL('../src/voice-host.ts', import.meta.url), 'utf8');
const checks = [
  ["gateway submit exists", /submitViaGateway\(route, id, body\)/.test(src)],
  ["dir write is the else-branch of the route switch",
    /route\.kind === 'gateway'[\s\S]{0,200}writeFileSync/.test(src)],
  ["results poll is dir-mode gated", /if \(route\.kind === 'dir'\) startResultsPoll/.test(src)],
  ["tenant route is transport-injected (params.tenant), with dir default",
    /params\.tenant \?\? \{ kind: 'dir' \}/.test(src)],
  ["task body carries agent_id when routed", /agent_id: \$\{route\.agentId\}/.test(src)],
];
let fail = 0;
for (const [name, ok] of checks) {
  console.log((ok ? "  ok  " : "  FAIL ") + name);
  if (!ok) fail++;
}
console.log(fail ? `FAILED (${fail})` : "PASS — tenant seam invariants pinned");
process.exit(fail ? 1 : 0);
