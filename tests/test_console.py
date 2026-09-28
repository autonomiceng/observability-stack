import shutil
import subprocess
import unittest
from pathlib import Path

CONSOLE = Path(__file__).resolve().parent.parent / "docker/caddy/console/console.js"


class ConsoleTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js required for the static console check")
    def test_badges_follow_status_and_health(self):
        script = r'''
const assert = require('node:assert/strict');
const c = require(process.argv[1]);
assert.deepEqual([200, 502, 503, 504, 404, 500].map(c.probeState),
  ['healthy', 'unreachable', 'unreachable', 'unreachable', 'unknown', 'unknown']);
const on = {enabled: true, version: 'v1.0.0'};
for (const [component, probe, state] of [
  [undefined, 'healthy', 'unknown'],
  [{enabled: false}, 'healthy', 'disabled'],
  [on, 'healthy', 'healthy'],
  [on, 'unreachable', 'unreachable'],
  [on, undefined, 'unknown'],
]) assert.equal(c.componentState(component, probe), state, JSON.stringify([component, probe]));
assert.deepEqual([null, {features: {}}, {features: {alerts: {configured: true}}}, {features: {alerts: {configured: false}}}]
  .map(c.alertState), ['unknown', 'unknown', 'configured', 'degraded']);
assert.deepEqual([undefined, on, {enabled: true, version: null}].map(c.versionText),
  ['Version unknown', 'Configured v1.0.0', 'Configured']);
for (const bad of [null, {contract: 1, stack: 'observability', components: []}, {contract: 2, stack: 'gateway', components: []}])
  assert.throws(() => c.parseStatus(bad));

(async () => {
  const valid = {contract: 2, stack: 'observability', components: [
    {id: 'grafana', enabled: true}, {id: 'rustfs', enabled: false}, {id: 'bad'}]};
  const ids = ['grafana', 'rustfs', 'loki', 'grafana'];
  let probed = [];
  const probe = async (id) => { probed.push(id); return 'healthy'; };
  // Only enabled components are probed, each once; disabled and unlisted ones never.
  let result = await c.load(async () => valid, probe, ids);
  assert.deepEqual(probed, ['grafana']);
  assert.deepEqual(result.health, {grafana: 'healthy'});
  assert.deepEqual([...result.status.components.keys()], ['grafana', 'rustfs']);
  // A rejected or absent document after a valid one leaves nothing known and probes nothing.
  for (const next of [async () => ({...valid, contract: 3}), async () => { throw new Error('404'); }]) {
    probed = [];
    result = await c.load(next, probe, ids);
    assert.deepEqual([result.status, result.health, probed], [null, {}, []]);
  }
})().catch((error) => { console.error(error); process.exit(1); });
'''
        subprocess.run(["node", "-e", script, str(CONSOLE)], check=True)


if __name__ == "__main__":
    unittest.main()
