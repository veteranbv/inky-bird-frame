import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { setImmediate } from 'node:timers/promises';
import test from 'node:test';

const source = await readFile(
  new URL('../../src/inky_bird_frame/web/frame-tools.js', import.meta.url),
  'utf8',
);
const { frameTools } = await import(
  'data:text/javascript;base64,' + Buffer.from(source).toString('base64')
);

class Element extends EventTarget {
  constructor() {
    super();
    this.children = [];
    this.classes = new Set();
    this.classList = {
      contains: (name) => this.classes.has(name),
      remove: (name) => this.classes.delete(name),
      add: (name) => this.classes.add(name),
      toggle: (name) =>
        this.classes.has(name) ? this.classes.delete(name) : this.classes.add(name),
    };
  }
  append(...nodes) {
    this.children.push(...nodes);
  }
  contains(node) {
    return this.children.includes(node);
  }
}

test('rapid wake-lock toggles and disposal release every deferred sentinel', async () => {
  const oldDocument = globalThis.document,
    oldWindow = globalThis.window;
  const navigatorDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
  const requests = [],
    sentinels = [];
  globalThis.document = Object.assign(new EventTarget(), {
    hidden: false,
    documentElement: {},
    activeElement: {},
  });
  globalThis.window = { isSecureContext: true };
  Object.defineProperty(globalThis, 'navigator', {
    configurable: true,
    value: {
      wakeLock: {
        request: () =>
          new Promise((resolve) =>
            requests.push(() => {
              const sentinel = Object.assign(new EventTarget(), {
                released: false,
                async release() {
                  this.released = true;
                  this.dispatchEvent(new Event('release'));
                },
              });
              sentinels.push(sentinel);
              resolve(sentinel);
            }),
          ),
      },
    },
  });
  let cleanup;
  try {
    const controls = new Element(),
      viewport = new Element(),
      status = { textContent: '' };
    const button = (text, action) => Object.assign(new Element(), { textContent: text, action });
    cleanup = frameTools({
      viewport,
      controls,
      status,
      button,
      previous() {},
      next() {},
      togglePlay() {},
    });
    const awake = controls.children.find((node) => node.textContent === 'Keep screen awake');
    const first = awake.action();
    await awake.action();
    await awake.action();
    assert.equal(requests.length, 1, 'acquisition is serialized while the first request waits');
    requests[0]();
    await first;
    await setImmediate();
    assert.equal(sentinels[0].released, true, 'superseded first sentinel releases itself');
    assert.equal(requests.length, 2, 'the deliberate new on state can request a fresh lock');
    await awake.action();
    cleanup();
    requests[1]();
    await setImmediate();
    assert(
      sentinels.every((sentinel) => sentinel.released),
      'off and disposal release every acquired sentinel',
    );
    assert.equal(requests.length, 2, 'disposal never starts another request');
  } finally {
    cleanup?.();
    globalThis.document = oldDocument;
    globalThis.window = oldWindow;
    if (navigatorDescriptor) Object.defineProperty(globalThis, 'navigator', navigatorDescriptor);
    else delete globalThis.navigator;
  }
});

test('a new on state acquires while an old sentinel release is pending', async () => {
  const oldDocument = globalThis.document,
    oldWindow = globalThis.window,
    navigatorDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
  const sentinels = [],
    releases = [];
  globalThis.document = Object.assign(new EventTarget(), {
    hidden: false,
    documentElement: {},
    activeElement: {},
  });
  globalThis.window = { isSecureContext: true };
  Object.defineProperty(globalThis, 'navigator', {
    configurable: true,
    value: {
      wakeLock: {
        request: async () => {
          const sentinel = Object.assign(new EventTarget(), {
            released: false,
            release() {
              return new Promise((resolve) =>
                releases.push(() => {
                  this.released = true;
                  this.dispatchEvent(new Event('release'));
                  resolve();
                }),
              );
            },
          });
          sentinels.push(sentinel);
          return sentinel;
        },
      },
    },
  });
  let cleanup;
  try {
    const controls = new Element();
    cleanup = frameTools({
      viewport: new Element(),
      controls,
      status: { textContent: '' },
      button: (text, action) => Object.assign(new Element(), { textContent: text, action }),
      previous() {},
      next() {},
      togglePlay() {},
    });
    const awake = controls.children.find((node) => node.textContent === 'Keep screen awake');
    await awake.action();
    const off = awake.action();
    await awake.action();
    assert.equal(sentinels.length, 2);
    releases[0]();
    await off;
    assert(!sentinels[1].released);
    assert.equal(awake.textContent, 'Allow screen sleep');
    cleanup();
    releases[1]();
    await setImmediate();
    assert(sentinels.every((sentinel) => sentinel.released));
  } finally {
    cleanup?.();
    globalThis.document = oldDocument;
    globalThis.window = oldWindow;
    if (navigatorDescriptor) Object.defineProperty(globalThis, 'navigator', navigatorDescriptor);
    else delete globalThis.navigator;
  }
});
