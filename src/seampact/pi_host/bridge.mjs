/** JSONL host adapter. Pi owns every model/tool turn; Python owns external I/O. */
import { Agent } from '@earendil-works/pi-agent-core';
import { createAssistantMessageEventStream, getCurrentTools } from '@earendil-works/pi-ai';
import { createRequire } from 'node:module';
import { createInterface } from 'node:readline';
import { pathToFileURL } from 'node:url';

const require = createRequire(import.meta.url);
export const identity = Object.freeze({
  runtime: 'pi-agent-core',
  package: '@earendil-works/pi-agent-core',
  version: require('@earendil-works/pi-agent-core/package.json').version,
});
const emptyUsage = () => ({ input: 0, output: 0, cacheRead: 0, cacheWrite: 0,
  totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } });
const copy = (value) => structuredClone(value);

function modelDescriptor(value = {}) {
  // No endpoint, credential, provider client or direct model connection lives here.
  return { id: value.id || 'seampact-model', name: value.id || 'seampact-model',
    api: 'openai-completions', provider: 'seampact-python', baseUrl: '', reasoning: true,
    input: ['text', 'image'], cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    contextWindow: value.contextWindow || 0, maxTokens: value.maxTokens || 0 };
}

function textContent(content) {
  if (typeof content === 'string') return [{ type: 'text', text: content }];
  if (content == null) return [];
  if (!Array.isArray(content)) throw new Error('Message content must be text or an array');
  return content.flatMap((item) => {
    if (item.type === 'text') return [{ type: 'text', text: item.text }];
    if (item.type === 'image') return [copy(item)];
    const data = item.type === 'image_url' && item.image_url?.url?.match(/^data:([^;,]+);base64,([\s\S]+)$/);
    return data ? [{ type: 'image', mimeType: data[1], data: data[2] }] : [];
  });
}

function parseCall(call) {
  const raw = call.function?.arguments;
  let args;
  let argumentsError;
  try {
    args = typeof raw === 'string' ? JSON.parse(raw) : raw;
    if (!args || Array.isArray(args) || typeof args !== 'object') throw new Error('Expected a JSON object');
  } catch (error) {
    args = {};
    argumentsError = `Invalid tool arguments: ${error.message}; received ${String(raw)}`;
  }
  return { type: 'toolCall', id: call.id, name: call.function?.name, arguments: args,
    ...(argumentsError ? { argumentsError } : {}) };
}

function assistantMessage(wire, model, finishReason = null, usage = null) {
  const content = textContent(wire.content);
  if (wire.reasoning_content) content.push({ type: 'thinking', thinking: wire.reasoning_content });
  content.push(...(wire.tool_calls || []).map(parseCall));
  const complete = finishReason == null || ['stop', 'tool_calls'].includes(finishReason);
  const input = usage?.prompt_tokens ?? usage?.input_tokens ?? 0;
  const output = usage?.completion_tokens ?? usage?.output_tokens ?? 0;
  return { role: 'assistant', content, api: model.api, provider: model.provider,
    model: model.id, usage: { ...emptyUsage(), input, output, totalTokens: usage?.total_tokens ?? input + output },
    stopReason: complete ? (content.some((part) => part.type === 'toolCall') ? 'toolUse' : 'stop') : 'error',
    ...(!complete ? { errorMessage: `Incomplete model response: ${finishReason}` } : {}),
    timestamp: Date.now(), _wire: copy(wire) };
}

export function fromWire(messages, model, declarations = []) {
  const names = new Map();
  return messages.map((wire, index) => {
    let result;
    if (wire.role === 'assistant') {
      result = assistantMessage(wire, model);
      for (const call of wire.tool_calls || []) names.set(call.id, call.function?.name);
    } else if (wire.role === 'tool') {
      result = { role: 'toolResult', toolCallId: wire.tool_call_id,
        toolName: wire.name || names.get(wire.tool_call_id) || 'unknown',
        content: textContent(wire.content), isError: wire.isError === true, timestamp: Date.now(), _wire: copy(wire) };
    } else if (wire.role === 'system' || wire.role === 'developer' || wire.role === 'user') {
      const role = wire.role === 'developer' ? 'system' : wire.role;
      result = { role, content: role === 'system' ? (wire.content || '') : textContent(wire.content),
        timestamp: Date.now(), _wire: copy(wire) };
    } else {
      throw new Error(`Unsupported message role: ${wire.role}`);
    }
    if (index === 0 && result.role === 'system') result.toolsAdded = declarations;
    return result;
  });
}

export function toWire(messages) {
  return messages.map((message) => {
    // Retain provider content, image URLs, reasoning and even invalid raw arguments exactly.
    if (message._wire) return copy(message._wire);
    if (message.role === 'system') return { role: 'system', content: message.content };
    if (message.role === 'user') return { role: 'user', content: message.content.map((part) => part.type === 'image'
      ? { type: 'image_url', image_url: { url: `data:${part.mimeType};base64,${part.data}` } } : copy(part)) };
    if (message.role === 'toolResult') return { role: 'tool', tool_call_id: message.toolCallId,
      content: message.content.length === 1 && message.content[0].type === 'text'
        ? message.content[0].text : message.content.map((part) => part.type === 'image'
          ? { type: 'image_url', image_url: { url: `data:${part.mimeType};base64,${part.data}` } } : copy(part)),
       };
    const text = message.content.filter((part) => part.type === 'text').map((part) => part.text).join('');
    const thinking = message.content.filter((part) => part.type === 'thinking').map((part) => part.thinking).join('');
    const calls = message.content.filter((part) => part.type === 'toolCall');
    return { role: 'assistant', content: text || null,
      ...(thinking ? { reasoning_content: thinking } : {}),
      ...(calls.length ? { tool_calls: calls.map((part) => ({ id: part.id, type: 'function',
        function: { name: part.name, arguments: JSON.stringify(part.arguments) } })) } : {}) };
  });
}

function declarations(tools) {
  return tools.map((tool) => ({ name: tool.function.name, description: tool.function.description || '',
    parameters: tool.function.parameters || { type: 'object', properties: {} } }));
}

class HostError extends Error {
  constructor(value) {
    super(typeof value === 'string' ? value : value?.message || 'Python host request failed');
    this.name = value?.type || 'HostError';
    this.fatal = value?.fatal === true;
  }
}

/** One outstanding request is normal; ids also reject late responses after abort. */
class Peer {
  constructor(send) { this.send = send; this.sequence = 0; this.pending = new Map(); }
  request(method, params, signal, onDelta) {
    if (signal?.aborted) return Promise.reject(new DOMException('Operation aborted', 'AbortError'));
    const id = String(++this.sequence);
    return new Promise((resolve, reject) => {
      const settle = (error, result) => {
        if (!this.pending.delete(id)) return;
        signal?.removeEventListener('abort', abort);
        error ? reject(error) : resolve(result);
      };
      const abort = () => settle(new DOMException('Operation aborted', 'AbortError'));
      this.pending.set(id, { settle, onDelta });
      signal?.addEventListener('abort', abort, { once: true });
      this.send({ type: 'request', id, method, params });
    });
  }
  receive(value) {
    const request = this.pending.get(String(value.id));
    if (!request) return;
    if (value.type === 'model_delta') request.onDelta?.(value.text);
    else if (value.type === 'reply') request.settle(value.error ? new HostError(value.error) : null, value.result);
  }
  close() {
    for (const request of [...this.pending.values()]) request.settle(new Error('Python host disconnected'));
  }
}

export class PiHost {
  constructor(send) { this.send = send; this.peer = new Peer(send); this.started = false; this.fatalError = undefined; }
  receive(value) {
    if (value.type === 'abort') { this.abort(); return; }
    if (value.type !== 'start') { this.peer.receive(value); return; }
    if (this.started) throw new Error('A Pi host process accepts exactly one run');
    this.started = true;
    this.completion = this.run(value);
  }
  toolSet(specifications) {
    return declarations(specifications).map((tool) => ({ ...tool, label: tool.name,
      execute: async (call_id, args, signal) => {
        try {
          const result = await this.peer.request('tool', { name: tool.name, arguments: args, call_id }, signal);
          return { content: typeof result.content === 'string' ? [{ type: 'text', text: result.content }] : result.content,
            isError: result.isError === true, ...(result.details === undefined ? {} : { details: result.details }) };
        } catch (error) {
          if (error.fatal) { this.fatalError = error.message; this.abort(); }
          throw error;
        }
      } }));
  }
  stream(model, context, options = {}) {
    const stream = createAssistantMessageEventStream();
    const partial = { role: 'assistant', content: [], api: model.api, provider: model.provider,
      model: model.id, usage: emptyUsage(), stopReason: 'pending', timestamp: Date.now() };
    let textStarted = false;
    stream.push({ type: 'start', partial });
    const delta = (text) => {
      if (typeof text !== 'string' || !text || options.signal?.aborted) return;
      if (!textStarted) {
        textStarted = true;
        partial.content.push({ type: 'text', text: '' });
        stream.push({ type: 'text_start', contentIndex: 0, partial });
      }
      partial.content[0].text += text;
      stream.push({ type: 'text_delta', contentIndex: 0, delta: text, partial });
    };
    void (async () => {
      try {
        const tools = getCurrentTools(context.messages).map((tool) => ({ type: 'function', function: tool }));
        const response = await this.peer.request('model', { messages: toWire(context.messages), tools,
          model: { id: model.id, contextWindow: model.contextWindow, maxTokens: model.maxTokens } }, options.signal, delta);
        if (response?._seampact_context_messages) {
          this.replaceActiveContext(response._seampact_context_messages, model, tools, partial);
        }
        const choice = response?.choices?.[0];
        if (!choice?.message) throw new Error('Model response has no assistant message');
        const wire = { ...choice.message, role: 'assistant' };
        const final = assistantMessage(wire, model, choice.finish_reason, response.usage);
        const finalText = final.content.find((part) => part.type === 'text')?.text || '';
        if (!textStarted && finalText) delta(finalText);
        if (textStarted) stream.push({ type: 'text_end', contentIndex: 0, content: finalText, partial });
        for (const part of final.content.filter((item) => item.type !== 'text')) {
          const contentIndex = partial.content.length;
          if (part.type === 'thinking') {
            partial.content.push({ type: 'thinking', thinking: '' });
            stream.push({ type: 'thinking_start', contentIndex, partial });
            partial.content[contentIndex].thinking = part.thinking;
            stream.push({ type: 'thinking_delta', contentIndex, delta: part.thinking, partial });
            stream.push({ type: 'thinking_end', contentIndex, content: part.thinking, partial });
          } else if (part.type === 'toolCall') {
            partial.content.push({ ...part, arguments: {} });
            stream.push({ type: 'toolcall_start', contentIndex, partial });
            partial.content[contentIndex] = part;
            stream.push({ type: 'toolcall_delta', contentIndex, delta: JSON.stringify(part.arguments), partial });
            stream.push({ type: 'toolcall_end', contentIndex, toolCall: part, partial });
          }
        }
        Object.assign(partial, final);
        if (final.stopReason === 'error') stream.push({ type: 'error', reason: 'error', error: final });
        else stream.push({ type: 'done', reason: final.stopReason, message: final });
        stream.end(final);
      } catch (error) {
        const aborted = options.signal?.aborted || error.name === 'AbortError';
        const final = { ...partial, stopReason: aborted ? 'aborted' : 'error', errorMessage: error.message };
        stream.push({ type: 'error', reason: final.stopReason, error: final });
        stream.end(final);
      }
    })();
    return stream;
  }
  async run(input) {
    const model = modelDescriptor(input.model);
    const tools = input.tools || [];
    const messages = copy(input.messages || []);
    if (messages[0]?.role !== 'system') messages.unshift({ role: 'system', content: input.systemPrompt || '' });
    let active = fromWire(messages, model, declarations(tools));
    let activeWire = copy(messages);
    this.replaceActiveContext = (messages, model, tools, partial) => {
      // Python may compact once after a pre-generation context-limit rejection.
      const pending = active.includes(partial) ? [partial] : [];
      active.splice(0, active.length, ...fromWire(messages, model, declarations(tools)), ...pending);
      activeWire = copy(messages);
    };
    this.agent = new Agent({
      initialState: { model, messages: active, tools: this.toolSet(tools) },
      toolExecution: 'sequential', streamFn: (...args) => this.stream(...args),
      prepareRequest: async ({ context }, signal) => {
        const result = await this.peer.request('context', {
          messages: toWire(context.messages),
          tools: (context.tools || []).map(({ name, description, parameters }) =>
            ({ type: 'function', function: { name, description, parameters } })),
          model: { id: model.id, contextWindow: model.contextWindow, maxTokens: model.maxTokens },
        }, signal);
        const nextTools = result.tools || [];
        const nextMessages = copy(result.messages);
        if (nextMessages[0]?.role !== 'system') throw new Error('Prepared context must start with a system message');
        active = fromWire(nextMessages, model, declarations(nextTools));
        activeWire = copy(nextMessages);
        return { context: { messages: active, tools: this.toolSet(nextTools) } };
      },
    });
    this.agent.subscribe((event) => {
      const wireMessage = event.type === 'message_end' ? toWire([event.message])[0] : undefined;
      if (wireMessage) activeWire.push(copy(wireMessage));
      this.send({ type: 'event', event: { ...event, ...(wireMessage ? { wire_message: wireMessage } : {}) } });
    });
    try {
      await this.agent.continue();
      this.send({ type: 'done', identity, messages: toWire(this.agent.state.messages),
        context_messages: activeWire, aborted: this.aborted === true,
        ...(this.fatalError || this.agent.state.errorMessage ? { error: this.fatalError || this.agent.state.errorMessage } : {}) });
    } catch (error) {
      this.send({ type: 'done', identity, messages: toWire(this.agent.state.messages),
        context_messages: activeWire, aborted: this.aborted === true, error: error.message });
    }
  }
  abort() { this.aborted = true; this.agent?.abort(); }
  close() { this.abort(); this.peer.close(); }
}

function main() {
  const send = (value) => process.stdout.write(JSON.stringify(value) + '\n');
  const host = new PiHost(send);
  const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
  send({ type: 'ready', identity });
  lines.on('line', (line) => {
    try {
      const value = JSON.parse(line);
      if (value.type === 'abort') host.abort();
      else host.receive(value);
      if (value.type === 'start') void host.completion.finally(() => { lines.close(); process.stdin.pause(); });
    } catch (error) {
      host.close();
      send({ type: 'protocol_error', error: error.message });
    }
  });
  lines.on('close', () => host.close());
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) main();
