import test from 'node:test';
import assert from 'node:assert/strict';
import { once } from 'node:events';
import { WebSocketServer } from 'ws';
import { ArgusClient } from './index.mjs';

function client() {
  const c = new ArgusClient({ gatewayHttpUrl: 'http://localhost', gatewayWsUrl: 'ws://localhost', cwd: '/workspace' });
  c.sent = [];
  c._send = (message) => c.sent.push(message);
  return c;
}

test('provisioning and initialize have separate bounded clocks, with one initialize', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const c = client();
  let complete = false;
  const initialized = c.initialize().then(() => { complete = true; });
  c._handleNotification({ method: 'argus/runtime/status', params: { phase: 'provisioning' } });
  t.mock.timers.tick(35_000);
  await Promise.resolve();
  assert.equal(complete, false);
  c._handleNotification({ method: 'argus/session', params: { id: 'session-test' } });
  t.mock.timers.tick(29_000);
  c._handleWire(JSON.stringify({ id: 1, result: { userAgent: 'runtime' } }));
  await initialized;
  assert.equal(c.sent.filter(x => x.method === 'initialize').length, 1);
  assert.equal(c.sent.at(-1).method, 'initialized');
});

test('static upstream without status keeps the initialize timeout', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const c = client();
  const rejected = assert.rejects(c.initialize(), /Timeout waiting for initialize/);
  t.mock.timers.tick(30_000);
  await rejected;
});

test('repeated provisioning notifications cannot extend the deadline forever', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const c = client();
  c._handleNotification({ method: 'argus/runtime/status', params: { phase: 'provisioning' } });
  const rejected = assert.rejects(c.initialize(), /Timeout waiting for runtime provisioning/);
  t.mock.timers.tick(200_000);
  c._handleNotification({ method: 'argus/runtime/status', params: { phase: 'provisioning' } });
  t.mock.timers.tick(40_000);
  await rejected;
});

test('runtime failure rejects the waiting initialize with the actual public cause', async () => {
  const c = client();
  const rejected = assert.rejects(c.initialize(), /存储空间不足/);
  c._handleNotification({ method: 'argus/runtime/error', params: { message: '存储空间不足' } });
  await rejected;
  assert.equal(c.pending.size, 0);
});

for (const createNew of [false, true]) {
  test(`real websocket reports early provisioning errors (${createNew ? 'new' : 'existing'} session)`, async () => {
    const server = new WebSocketServer({ port: 0, host: '127.0.0.1' });
    await once(server, 'listening');
    server.on('connection', ws => {
      ws.send(JSON.stringify({ method: 'argus/runtime/status', params: { phase: 'provisioning' } }));
      ws.send(JSON.stringify({ method: 'argus/runtime/error', params: { message: '存储空间不足' } }));
      ws.close(1011, 'Runtime storage capacity unavailable');
    });
    const c = new ArgusClient({ gatewayHttpUrl: 'http://localhost', gatewayWsUrl: `ws://127.0.0.1:${server.address().port}`, cwd: '/workspace' });
    try {
      await assert.rejects(createNew ? c.connectNewSession() : c.connectToSession('session-test'), /存储空间不足/);
      assert.equal(c._onSessionId, null);
    } finally {
      for (const ws of server.clients) ws.terminate();
      await new Promise(resolve => server.close(resolve));
    }
  });
}
