import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
import test from 'node:test';
import { createUuid } from '../../src/functions/uuid.ts';

const uuidV4 =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

test('uses the native UUID generator when available', (context) => {
  const expected = 'e8ad4f88-6df5-4b19-bd31-2d249e5fe14c';
  const native = context.mock.method(
    globalThis.crypto,
    'randomUUID',
    () => expected
  );
  assert.equal(createUuid(), expected);
  assert.equal(native.mock.callCount(), 1);
});

test('sets UUID version and variant bits without randomUUID', () => {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'crypto')!;
  try {
    Object.defineProperty(globalThis, 'crypto', {
      configurable: true,
      value: {
        getRandomValues: (bytes: Uint8Array) => bytes.fill(0xff)
      }
    });
    assert.equal(createUuid(), 'ffffffff-ffff-4fff-bfff-ffffffffffff');
  } finally {
    Object.defineProperty(globalThis, 'crypto', descriptor);
  }
});

test('HTTP fallback produces distinct valid UUIDs using Web Crypto randomness', () => {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'crypto')!;
  try {
    Object.defineProperty(globalThis, 'crypto', {
      configurable: true,
      value: { getRandomValues: webcrypto.getRandomValues.bind(webcrypto) }
    });
    const ids = Array.from({ length: 1000 }, () => createUuid());
    for (const id of ids) assert.match(id, uuidV4);
    assert.equal(new Set(ids).size, ids.length);
  } finally {
    Object.defineProperty(globalThis, 'crypto', descriptor);
  }
});
