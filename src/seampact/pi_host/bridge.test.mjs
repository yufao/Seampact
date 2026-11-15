import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import test from 'node:test';
import { PiHost, fromWire, identity, toWire } from './bridge.mjs';

const tool = { type: 'function', function: { name: 'effect', description: 'A Python-owned side effect',
  parameters: { type: 'object', properties: { value: { type: 'integer' } }, required: ['value'], additionalProperties: false } } };
const response = (message, finish_reason = 'stop') => ({ choices: [{ message: { role: 'assistant', ...message }, finish_reason }],
  usage: { prompt_tokens: 9, completion_tokens: 3, total_tokens: 12 } });
const call = (id, args, name = 'effect') => ({ id, type: 'function', function: { name, arguments: args } });
const initial = { type: 'start', messages: [{ role: 'system', content: 'original' }, { role: 'user', content: 'run' }],
  tools: [tool], model: { id: 'fake-python-model' } };

async function scenario(handle) {
  const output = [];
  let host;
  host = new PiHost((value) => {
    output.push(structuredClone(value));
    if (value.type === 'request') queueMicrotask(() => {
      Promise.resolve(handle(value, host)).then((result) => {
        if (result !== undefined) host.receive({ type: 'reply', id: value.id, result });
      }).catch((error) => host.receive({ type: 'reply', id: value.id, error: { message: error.message } }));
    });
  });
  host.receive(structuredClone(initial));
  await host.completion;
  return output;
}

test('the installed official Pi owns sequential tools, errors and the next model turn', async () => {
  let models = 0;
  const effects = [];
  const output = await scenario((request, host) => {
    if (request.method === 'context') {
      const messages = structuredClone(request.params.messages);
      messages[0].content = 'prepared by Python';
      return { messages, tools: [tool] };
    }
    if (request.method === 'tool') {
      effects.push(request.params);
      return { content: JSON.stringify({ value: request.params.arguments.value }), isError: effects.length === 2 };
    }
    assert.equal(request.params.messages[0].content, 'prepared by Python');
    if (++models === 1) return response({ content: null, reasoning_content: 'provider reasoning',
      tool_calls: [call('first', '{"value":1}'), call('second', '{"value":2}')] }, 'tool_calls');
    assert.deepEqual(request.params.messages.slice(-2).map((message) => message.tool_call_id), ['first', 'second']);
    host.receive({ type: 'model_delta', id: request.id, text: 'done' });
    return response({ content: 'done' });
  });
  assert.equal(identity.package, '@earendil-works/pi-agent-core');
  assert.equal(identity.version, '1.5.2');
  assert.equal(models, 2);
  assert.deepEqual(effects, [{ name: 'effect', arguments: { value: 1 }, call_id: 'first' },
    { name: 'effect', arguments: { value: 2 }, call_id: 'second' }]);
  const ended = output.filter((value) => value.event?.type === 'tool_execution_end');
  assert.deepEqual(ended.map((value) => value.event.isError), [false, true]);
  const done = output.at(-1);
  assert.equal(done.type, 'done');
  assert.equal(done.error, undefined);
  assert.equal(done.context_messages[0].content, 'prepared by Python');
  assert.equal(done.context_messages.length, 6);
  assert.equal(done.context_messages[2].reasoning_content, 'provider reasoning');
  assert.equal(output.filter((value) => value.event?.assistantMessageEvent?.type === 'text_delta').length, 1);
});

test('Pi validates malformed and unknown tool calls without executing Python effects', async () => {
  let models = 0;
  const output = await scenario((request) => {
    if (request.method === 'context') return request.params;
    assert.notEqual(request.method, 'tool', 'invalid tool arguments must never reach an effect');
    if (++models === 1) return response({ content: null, tool_calls: [
      call('invalid-json', '{"value":'), call('wrong-type', '{"value":"bad"}'), call('unknown', '{}', 'absent'),
    ] }, 'tool_calls');
    assert.equal(request.params.messages.filter((message) => message.role === 'tool').length, 3);
    assert.equal(request.params.messages[2].tool_calls[0].function.arguments, '{"value":');
    return response({ content: 'recovered' });
  });
  const errors = output.filter((value) => value.event?.type === 'tool_execution_end');
  assert.equal(errors.length, 3);
  assert.ok(errors.every((value) => value.event.isError));
  assert.equal(output.at(-1).error, undefined);
});

test('delivery repair is one native Pi follow-up with retained tool receipts', async () => {
  let models = 0;
  let effects = 0;
  const output = await scenario((request) => {
    if (request.method === 'context') return { ...request.params, tools: models === 2 ? [] : [tool] };
    if (request.method === 'tool') {
      effects += 1;
      return { content: 'effect completed once', isError: false };
    }
    models += 1;
    if (models === 1) return response({ content: null, tool_calls: [call('once', '{"value":1}')] }, 'tool_calls');
    if (models === 2) return { ...response({ content: 'undelivered draft' }), _seampact_revision_reason: 'Use supported facts.' };
    assert.equal(request.params.tools.length, 0);
    assert.equal(request.params.messages.at(-1).role, 'user');
    assert.ok(request.params.messages.slice(1).every((message) => message.role !== 'system'));
    assert.match(request.params.messages.at(-1).content, /Use supported facts\./);
    assert.equal(request.params.messages.filter((message) => message.tool_call_id === 'once').length, 1);
    return response({ content: 'revised answer' });
  });
  assert.equal(models, 3);
  assert.equal(effects, 1);
  assert.equal(output.filter((value) => value.event?.type === 'agent_start').length, 1);
  assert.equal(output.at(-1).error, undefined);
  assert.equal(output.at(-1).context_messages.at(-1).content, 'revised answer');
});

test('the bridge rejects a second delivery repair without a third model answer', async () => {
  let models = 0;
  const output = await scenario((request) => {
    if (request.method === 'context') return { ...request.params, tools: [] };
    models += 1;
    return { ...response({ content: 'undelivered' }), _seampact_revision_reason: 'Still unsupported.' };
  });
  assert.equal(models, 2);
  assert.match(output.at(-1).error, /exhausted delivery revision/);
});

test('abort never starts the remaining side effect or replays an active tool', async () => {
  let effects = 0;
  let models = 0;
  const output = await scenario((request, host) => {
    if (request.method === 'context') return request.params;
    if (request.method === 'tool') {
      effects += 1;
      host.receive({ type: 'abort' });
      host.receive({ type: 'reply', id: request.id, result: { content: 'late result', isError: false } });
      return undefined;
    }
    models += 1;
    return response({ content: null, tool_calls: [call('once', '{"value":1}'), call('never', '{"value":2}')] }, 'tool_calls');
  });
  assert.equal(effects, 1);
  assert.equal(models, 1);
  assert.equal(output.at(-1).aborted, true);
});

test('wire conversion retains provider image URLs, tool ids and reasoning verbatim', () => {
  const messages = [{ role: 'user', content: [{ type: 'image_url', image_url: { url: 'https://example.test/image.png', detail: 'high' } }] },
    { role: 'assistant', content: null, reasoning_content: 'reason', tool_calls: [call('opaque-id', '{ "value": 2 }')] },
    { role: 'tool', tool_call_id: 'opaque-id', content: '{"error":"unchanged"}' }];
  assert.deepEqual(toWire(fromWire(messages, { api: 'custom', provider: 'python', id: 'model' })), messages);
});

test('the stdio entry point emits real package identity and settles one run', { timeout: 10000 }, async () => {
  const child = spawn(process.execPath, [new URL('./bridge.mjs', import.meta.url).pathname.replace(/^\/([A-Za-z]:\/)/, '$1')],
    { stdio: ['pipe', 'pipe', 'pipe'] });
  const errors = [];
  child.stderr.on('data', (value) => errors.push(value.toString()));
  const lines = createInterface({ input: child.stdout });
  const seen = [];
  lines.on('line', (line) => {
    const value = JSON.parse(line);
    seen.push(value);
    if (value.type === 'request') child.stdin.write(JSON.stringify({ type: 'reply', id: value.id,
      result: value.method === 'context' ? value.params : response({ content: 'stdio works' }) }) + '\n');
  });
  child.stdin.write(JSON.stringify(initial) + '\n');
  const code = await new Promise((resolve) => child.on('exit', resolve));
  assert.equal(code, 0, errors.join(''));
  assert.deepEqual(seen[0], { type: 'ready', identity });
  assert.equal(seen.at(-1).type, 'done');
  assert.equal(seen.at(-1).context_messages.at(-1).content, 'stdio works');
});
