import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

CONSOLE = Path(__file__).resolve().parent.parent / "docker/caddy/console/console.js"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import bootstrap  # noqa: E402


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
const component = (id, enabled) => ({id, name: id, kind: 'app', enabled, image: 'grafana/grafana:13.2.2',
  version: '13.2.2', health: '/health/' + id});
const valid = {contract: 2, stack: 'observability', configuredAt: '2026-09-28T20:00:00Z',
  components: [component('grafana', true), component('rustfs', false)],
  features: {backups: {configured: true, lastCheckpointAt: null}, alerts: {configured: false}}};
assert.deepEqual([...c.parseStatus(valid).components.keys()], ['grafana', 'rustfs']);
const producer = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
assert.deepEqual([...c.parseStatus(producer).components.keys()],
  ['caddy', 'grafana', 'alloy', 'loki', 'mimir', 'tempo', 'rustfs']);
assert.equal(c.parseStatus(producer).components.get('rustfs').enabled, false);
assert.equal(c.parseStatus(producer).features.backups.lastCheckpointAt, null);
const parsed = c.parseStatus(valid);
assert.deepEqual([
  c.summaryText(null, {}, ['grafana']),
  c.summaryText(parsed, {}, ['rustfs']),
  c.summaryText(parsed, {grafana: 'healthy'}, ['grafana', 'rustfs']),
  c.summaryText(parsed, {grafana: 'healthy'}, ['grafana', 'loki', 'rustfs']),
  c.summaryText(parsed, {}, ['loki']),
].map(String), ['Status unavailable', 'Nothing enabled', '1 of 1 components reachable',
  '1 of 1 components reachable · 1 unknown', '1 unknown']);
// The field set is closed: an extra field anywhere, a missing envelope field or a duplicate ID rejects the document.
for (const bad of [null, {...valid, contract: 1}, {...valid, stack: 'gateway'}, {...valid, extra: 1},
  (({features, ...rest}) => rest)(valid), {...valid, components: [{...component('grafana', true), running: true}]},
  {...valid, features: {...valid.features, logs: {}}}, {...valid, features: {...valid.features, alerts: {configured: true, to: 'x'}}},
  {...valid, components: [component('grafana', true), component('grafana', false)]},
  {...valid, configuredAt: null}, {...valid, configuredAt: '2026-02-30T00:00:00Z'},
  {...valid, configuredAt: '2026-09-28T20:00:00+02:00'},
  {...valid, components: Array.from({length: 33}, (_, i) => component('id' + i, true))}])
  assert.throws(() => c.parseStatus(bad), undefined, JSON.stringify(bad));
for (const [field, value] of [
  ['name', ''], ['name', 1], ['kind', 'service'], ['enabled', 'true'], ['image', ''],
  ['image', 'repo@sha256:digest'], ['version', 12], ['version', 'bad version'],
  ['health', '/health/loki'], ['url', 'ftp://example.test'], ['url', 'https://example.test/admin'],
  ['url', 'https://user:pass@example.test'],
  ['url', 'https://example.test/?token=secret'], ['url', 3],
]) {
  const malformed = {...component('grafana', true), [field]: value};
  const result = c.parseStatus({...valid, components: [malformed, component('loki', true)]});
  assert.deepEqual([...result.components.keys()], ['loki'], field);
}
for (const missing of ['name', 'kind', 'enabled', 'image', 'version', 'health']) {
  const malformed = {...component('grafana', true)};
  delete malformed[missing];
  assert.deepEqual([...c.parseStatus({...valid, components: [malformed, component('loki', true)]}).components.keys()],
    ['loki'], missing);
}
assert.deepEqual([...c.parseStatus({...valid, components: [{id: 'grafana', enabled: true}]}).components.keys()], []);
assert.deepEqual([...c.parseStatus({...valid, components: [component('grafana', true),
  {...component('loki', true), url: 'https://example.test'}]}).components.keys()], ['grafana', 'loki']);
for (const [features, backups, alerts] of [
  [{backups: {configured: 'false', lastCheckpointAt: null}, alerts: {configured: true}}, undefined, true],
  [{backups: {configured: true, lastCheckpointAt: 'yesterday'}, alerts: {configured: true}}, undefined, true],
  [{backups: {configured: true}, alerts: {configured: true}}, undefined, true],
  [{backups: {configured: false, lastCheckpointAt: null}, alerts: {configured: 'true'}}, false, undefined],
  [{backups: {configured: false, lastCheckpointAt: null}}, false, undefined],
]) {
  const result = c.parseStatus({...valid, features});
  assert.deepEqual([...result.components.keys()], ['grafana', 'rustfs']);
  assert.equal(result.features.backups?.configured, backups);
  assert.equal(result.features.alerts?.configured, alerts);
}

(async () => {
  const ids = ['grafana', 'rustfs', 'loki', 'grafana'];
  let probed = [];
  const probe = async (id) => { probed.push(id); return 'healthy'; };
  // Only enabled components are probed, each once; disabled and unlisted ones never.
  let result = await c.load(async () => valid, probe, ids);
  assert.deepEqual(probed, ['grafana']);
  assert.deepEqual(result.health, {grafana: 'healthy'});
  assert.deepEqual([...result.status.components.keys()], ['grafana', 'rustfs']);
  for (const components of [
    [{id: 'grafana', enabled: true}],
    [{...component('grafana', true), health: '/health/loki'}],
    [{...component('grafana', true), name: 42}],
  ]) {
    probed = [];
    result = await c.load(async () => ({...valid, components}), probe, ids);
    assert.deepEqual([probed, result.health, c.summaryText(result.status, result.health, ['grafana'])],
      [[], {}, '1 unknown']);
  }
  probed = [];
  for (const malformed of [{id: 'grafana', enabled: true}, {...component('grafana', true), name: 42}]) {
    probed = [];
    result = await c.load(async () => ({...valid, components: [malformed, component('loki', true)]}), probe, ids);
    assert.deepEqual([probed, result.health, c.summaryText(result.status, result.health, ['grafana', 'loki'])],
      [['loki'], {loki: 'healthy'}, '1 of 1 components reachable · 1 unknown']);
  }
  probed = [];
  result = await c.load(async () => ({...valid, features: {backups: {configured: 'false', lastCheckpointAt: null},
    alerts: {configured: true}}}), probe, ids);
  assert.deepEqual([probed, result.health, result.status.features],
    [['grafana'], {grafana: 'healthy'}, {alerts: {configured: true}}]);
  // A rejected or absent document after a valid one leaves nothing known and probes nothing.
  for (const next of [async () => ({...valid, contract: 3}), async () => ({...valid, configuredAt: null}),
    async () => { throw new Error('404'); }]) {
    probed = [];
    result = await c.load(next, probe, ids);
    assert.deepEqual([result.status, result.health, probed], [null, {}, []]);
  }
  const response = (body, type = 'application/json', extra = {}) =>
    new Response(body, {status: 200, headers: {'Content-Type': type, ...extra}});
  probed = [];
  result = await c.load(() => c.readStatus(response(JSON.stringify(valid), 'application/json; charset=utf-8')),
    probe, ids);
  assert.deepEqual([probed, result.health], [['grafana'], {grafana: 'healthy'}]);
  for (const badResponse of [
    response(JSON.stringify(valid), 'text/html'),
    response(JSON.stringify(valid) + ' '.repeat(65537)),
    response(JSON.stringify(valid), 'application/json', {'Content-Length': '65537'}),
  ]) {
    probed = [];
    result = await c.load(() => c.readStatus(badResponse), probe, ids);
    assert.deepEqual([result.status, result.health, probed], [null, {}, []]);
  }
})().catch((error) => { console.error(error); process.exit(1); });
'''
        refs = bootstrap.images(CONSOLE.parents[3] / "compose.yaml")
        available = {name: {"image": ref} for name, ref in refs.items()}
        producer = bootstrap.status_document(available, {name: available[name] for name in available if name != "rustfs"},
                                             {}, CONSOLE.parents[3] / "absent-backups", "2026-09-28T20:00:00Z")
        subprocess.run(["node", "-e", script, str(CONSOLE)], input=json.dumps(producer), text=True, check=True)


if __name__ == "__main__":
    unittest.main()
