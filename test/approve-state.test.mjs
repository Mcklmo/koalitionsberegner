import assert from 'node:assert/strict';
import test from 'node:test';

import { buttonStates } from '../js/approve-state.js';

test('a queued draft can be sent or rejected', () => {
  assert.deepEqual(buttonStates('pending'), { send: true, reject: true });
  assert.deepEqual(buttonStates('failed'), { send: true, reject: true });
});

test('a draft claimed but never confirmed posted can only be rejected', () => {
  assert.deepEqual(buttonStates('approved'), { send: false, reject: true });
});

test('a settled draft offers nothing', () => {
  for (const status of ['posted', 'rejected', 'expired']) {
    assert.deepEqual(buttonStates(status), { send: false, reject: false }, status);
  }
});
