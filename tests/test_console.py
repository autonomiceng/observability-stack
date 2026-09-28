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
assert.deepEqual([
  [null, {}], [{}, {}], [{}, {a: 'healthy', b: 'unreachable'}], [{}, {a: 'healthy', b: 'unknown'}], [{}, {a: 'unknown'}],
].map(([status, health]) => c.summaryText(status, health)),
  ['Status unavailable', 'Nothing enabled', '1 of 2 components reachable', '1 of 1 components reachable · 1 unknown',
   '1 unknown']);
const component = (id, enabled) => ({id, name: id, kind: 'app', enabled, image: 'grafana/grafana:13.2.2',
  version: '13.2.2', health: '/health/' + id});
const valid = {contract: 2, stack: 'observability', configuredAt: '2026-09-28T20:00:00Z',
  components: [component('grafana', true), component('rustfs', false), {id: 'bad'}],
  features: {backups: {configured: true, lastCheckpointAt: null}, alerts: {configured: false}}};
assert.deepEqual([...c.parseStatus(valid).components.keys()], ['grafana', 'rustfs']);
// The field set is closed: an extra field anywhere, a missing envelope field or a duplicate ID rejects the document.
for (const bad of [null, {...valid, contract: 1}, {...valid, stack: 'gateway'}, {...valid, extra: 1},
  (({features, ...rest}) => rest)(valid), {...valid, components: [{...component('grafana', true), running: true}]},
  {...valid, features: {...valid.features, logs: {}}}, {...valid, features: {...valid.features, alerts: {configured: true, to: 'x'}}},
  {...valid, components: [component('grafana', true), component('grafana', false)]}])
  assert.throws(() => c.parseStatus(bad), undefined, JSON.stringify(bad));

(async () => {
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
