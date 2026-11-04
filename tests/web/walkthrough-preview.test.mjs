import assert from 'node:assert/strict';
import test from 'node:test';
import preview from './walkthrough-preview.cjs';

const poster = Buffer.from(Array.from({ length: 64 * 40 }, (_, i) => i % 256));

test('opening preview must match the fully visible title card', () => {
  assert.equal(preview.assertOpeningPixels(poster, poster).meanError, 0);
  assert.throws(() => preview.assertOpeningPixels(Buffer.alloc(poster.length, 245), poster), /does not match/);
  assert.throws(() => preview.assertOpeningPixels(Buffer.alloc(poster.length, 245), Buffer.alloc(poster.length, 245)), /blank/);
  assert.throws(() => preview.assertOpeningPixels(Buffer.alloc(0), poster), /completely/);
});
