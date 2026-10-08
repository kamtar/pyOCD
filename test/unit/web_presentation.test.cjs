// Run with: node --test test/unit/web_presentation.test.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../../pyocd/web/static/assets/app.js'), 'utf8');
const connectSource = source.slice(source.indexOf('// auto-gdb:begin'), source.indexOf('// auto-gdb:end'));

function harness({ enabled = false, connectError = false, gdbError = false } = {}) {
  const calls = [], messages = [];
  const context = vm.createContext({
    GDB_PORT: 3030,
    gdbOnConnect: () => enabled,
    connectTarget: async () => { calls.push('connect'); if (connectError) throw Error('No probe'); },
    api: async (route, options) => { calls.push([route, JSON.parse(options.body)]); if (gdbError) throw Error('Port occupied'); },
    refresh: async () => { calls.push('refresh'); },
    toast: (message, error) => messages.push({ message, error }),
  });
  vm.runInContext(connectSource, context);
  return { context, calls, messages };
}
test('the auto-start section is present in app.js', () => {
  assert.match(connectSource, /async function connectWithGdb/);
});
test('auto-start disabled leaves the connection available for direct operations', async () => {
  const { context, calls } = harness();
  await context.connectWithGdb();
  assert.deepEqual(calls, ['connect']);
});
test('auto-start starts GDB only after connection succeeds', async () => {
  const { context, calls } = harness({ enabled: true });
  await context.connectWithGdb();
  assert.deepEqual(calls, ['connect', ['/gdb/start', { port: 3030 }], 'refresh']);
});
test('a failed connection never starts GDB', async () => {
  const { context, calls } = harness({ enabled: true, connectError: true });
  await assert.rejects(context.connectWithGdb(), /No probe/);
  assert.deepEqual(calls, ['connect']);
});
test('GDB failure reports the partial success and refreshes target state', async () => {
  const { context, calls, messages } = harness({ enabled: true, gdbError: true });
  await context.connectWithGdb();
  assert.equal(calls.at(-1), 'refresh');
  assert.match(messages[0].message, /Target connected, but GDB could not start: Port occupied/);
  assert.equal(messages[0].error, true);
});
