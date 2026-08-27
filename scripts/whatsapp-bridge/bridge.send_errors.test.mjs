/**
 * Regression tests for delivery-failure tracking on outbound sends.
 *
 * WhatsApp's error 463 ("account restricted / missing tctoken") — sent
 * when this account is blocked from starting a new chat with a contact —
 * arrives asynchronously via messages.update, AFTER /send has already
 * returned a message id and reported success. Before this tracker existed,
 * /send always reported `success: true` as soon as WhatsApp *accepted* the
 * send, even when it was later rejected — so the agent confidently told
 * Carlos messages were delivered when they silently never arrived
 * (incident 2026-07-28: this happened on every cold-outreach send for
 * several days without anyone noticing).
 *
 * These tests exercise the tracker in isolation — no live WhatsApp socket.
 */

import { strict as assert } from 'node:assert';

import { createSendErrorTracker } from './bridge_helpers.js';

// -- no error recorded → waitFor resolves null without hanging ----------
{
  const tracker = createSendErrorTracker();
  const start = Date.now();
  const result = await tracker.waitFor(['msg-1'], { timeoutMs: 60, pollMs: 10 });
  const elapsed = Date.now() - start;
  assert.equal(result, null);
  assert.ok(elapsed >= 60, 'should wait out the full window when nothing arrives');
  console.log('✓ no delivery error recorded → waitFor resolves null');
}

// -- error recorded before waitFor is called → resolves immediately -----
{
  const tracker = createSendErrorTracker();
  tracker.record({ id: 'msg-2' }, { messageStubParameters: ['463'] });

  const start = Date.now();
  const result = await tracker.waitFor(['msg-2'], { timeoutMs: 5000, pollMs: 10 });
  const elapsed = Date.now() - start;

  assert.equal(result.messageId, 'msg-2');
  assert.equal(result.code, '463');
  assert.match(result.note, /account restricted/i);
  assert.match(result.note, /message this number first/i);
  assert.ok(elapsed < 200, 'should return as soon as the error is found, not wait out the timeout');
  console.log('✓ pre-recorded 463 error is surfaced with an explanatory note');
}

// -- error arrives mid-poll → waitFor picks it up before the deadline ----
{
  const tracker = createSendErrorTracker();
  setTimeout(() => {
    tracker.record({ id: 'msg-3' }, { messageStubParameters: ['500'] });
  }, 30);

  const result = await tracker.waitFor(['msg-3'], { timeoutMs: 2000, pollMs: 10 });
  assert.equal(result.messageId, 'msg-3');
  assert.equal(result.code, '500');
  assert.match(result.note, /rejected this message \(error 500\)/i);
  console.log('✓ delivery error arriving mid-poll is caught before the deadline');
}

// -- multiple candidate ids: only the erroring one is reported -----------
{
  const tracker = createSendErrorTracker();
  tracker.record({ id: 'chunk-2' }, { messageStubParameters: ['463'] });

  const result = await tracker.waitFor(['chunk-1', 'chunk-2', 'chunk-3'], { timeoutMs: 500, pollMs: 10 });
  assert.equal(result.messageId, 'chunk-2');
  console.log('✓ waitFor identifies which specific chunk failed among several sent');
}

// -- empty/falsy id list short-circuits without polling ------------------
{
  const tracker = createSendErrorTracker();
  const start = Date.now();
  const result = await tracker.waitFor([undefined, null], { timeoutMs: 5000, pollMs: 10 });
  const elapsed = Date.now() - start;
  assert.equal(result, null);
  assert.ok(elapsed < 50, 'should not poll at all when there are no real ids to check');
  console.log('✓ no message ids → resolves immediately without waiting');
}

// -- known-good chats skip the wait entirely (latency guard) ------------
{
  const tracker = createSendErrorTracker();
  tracker.markChatKnownGood('established@s.whatsapp.net');

  const start = Date.now();
  const result = await tracker.checkDelivery('established@s.whatsapp.net', ['msg-9'], { timeoutMs: 500, pollMs: 10 });
  const elapsed = Date.now() - start;

  assert.equal(result, null);
  assert.ok(elapsed < 50, 'known-good chats must not pay the verification wait at all');
  console.log('✓ known-good chat skips delivery verification (stays fast)');
}

// -- new chat: verified once, then becomes known-good for next send -----
{
  const tracker = createSendErrorTracker();
  assert.equal(tracker.isChatKnownGood('newcontact@s.whatsapp.net'), false);

  const first = await tracker.checkDelivery('newcontact@s.whatsapp.net', ['msg-10'], { timeoutMs: 60, pollMs: 10 });
  assert.equal(first, null, 'clean first send should report no error');
  assert.equal(tracker.isChatKnownGood('newcontact@s.whatsapp.net'), true,
    'a clean verified send should mark the chat known-good');

  const start = Date.now();
  const second = await tracker.checkDelivery('newcontact@s.whatsapp.net', ['msg-11'], { timeoutMs: 5000, pollMs: 10 });
  const elapsed = Date.now() - start;
  assert.equal(second, null);
  assert.ok(elapsed < 50, 'second send to the same now-known-good chat should be fast');
  console.log('✓ first send to a new contact verifies once, then subsequent sends are fast');
}

// -- new chat that actually fails delivery is NOT marked known-good -----
{
  const tracker = createSendErrorTracker();
  tracker.record({ id: 'msg-12' }, { messageStubParameters: ['463'] });

  const result = await tracker.checkDelivery('blocked@s.whatsapp.net', ['msg-12'], { timeoutMs: 500, pollMs: 10 });
  assert.equal(result.code, '463');
  assert.equal(tracker.isChatKnownGood('blocked@s.whatsapp.net'), false,
    'a failed send must not be remembered as known-good');
  console.log('✓ a failed delivery does not get remembered as known-good');
}

console.log('✅ All send-error tracker tests passed.');
