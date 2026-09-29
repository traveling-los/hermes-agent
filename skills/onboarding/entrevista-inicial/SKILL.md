---
name: entrevista-inicial
description: Use when a persistent agent-client relationship is being set up and the bot needs to learn how to address the user, the user's working hours and timezone, who to escalate to, what to always remember and what never to do alone, in 8 to 12 WhatsApp messages, one fact per memory entry, with a 5-line confirmation summary at the end.
version: 0.1.0
author: Minimax M3 (Careflow fleet)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Onboarding, Interview, Memory, Preferences, WhatsApp, Client-Bot]
    category: onboarding
    related_skills: []
---

# Entrevista Inicial

Conduct the first conversation between a persistent bot and the human it will
serve (the "principal"). Learn the principal's preferences and write each
answer as a separate memory entry. End with a five-line summary the principal
confirms before the bot takes any real action on their behalf.

## When to Use

- The bot has just been created or assigned to a new principal and has never
  spoken with them.
- The principal has not yet told the bot how they want to be addressed, when
  they work, who to escalate to, or what the bot is forbidden from doing solo.
- You need a durable, auditable record of those preferences before the bot
  handles any real conversation on the principal's behalf.

Do **not** use this skill for:

- First contact with a *customer* of the principal (use the principal's bot
  prompt instead).
- Routine preference updates from the principal after the first interview
  (write the new fact as a memory entry directly, no interview needed).
- Filling in missing preferences later via follow-up questions (one entry per
  question, no full re-interview).

## Core Principle

**One question per message. One fact per memory entry. The principal
confirms a summary before the bot acts on the new information.**

Splitting turns keeps each answer auditable; one entry per fact keeps memory
queryable; the final summary forces the principal to validate the bot's
understanding before the bot is trusted with real work.

## Recipe

The interview has three phases: **opening**, **questions** (8 to 12), and
**confirmation**. Never skip the third phase.

### Phase 1 — Opening (single message)

Tell the principal, in their own language:

1. Who you are (bot name and who you serve).
2. That you will ask N questions, one per message.
3. How to stop (a stop word they can send at any time).

Keep it to two or three short lines. No emoji flood.

### Phase 2 — Questions (8 to 12 single-question messages)

For each question:

1. Send **one** question. Do not bundle. Do not ask two things in one turn.
2. Wait for the answer before sending the next question.
3. As soon as the answer lands, write **one** memory entry that contains
   exactly one fact derived from that answer. Do not paraphrase two answers
   into one entry. Do not write a "preferences" blob.
4. If the answer is empty or "I don't know", write a memory entry that says
   exactly that the fact is unknown for now, and move on. Do not invent.

The question set below covers the minimum a bot needs to be useful to its
principal. Pick 8 to 12; you may reorder them so the conversation feels
natural, but every chosen question must be asked.

| # | Question (intent) | What goes in memory |
|---|-------------------|---------------------|
| 1 | How do you want me to address you (tú/usted, formal/casual, name/nickname)? | `principal_address_style` |
| 2 | Which language do we use day to day, and what if a customer writes in the other one? | `principal_language` |
| 3 | What tone do I use with **your** customers — close, professional, relaxed — and do you want emojis? | `customer_tone` |
| 4 | What are your working hours for customer replies? (days + time range) | `principal_hours` |
| 5 | What timezone are those hours in? | `principal_timezone` |
| 6 | Who do I escalate to when a customer is angry, asks for a discount, or goes off-script? (name + channel) | `escalation_contact` |
| 7 | What must I **always** remember about you, your business, or your customers? | `always_remember` |
| 8 | What am I **never** allowed to do without asking you first? (discounts, cancellations, exceptions, etc.) | `never_alone` |
| 9 | What must I **never** share with a customer? (costs, vendors, personal data, internal pricing) | `never_share` |
| 10 | When a customer asks something I do not know, what is the default move — say "I will check", page you, or ask a teammate? | `unknown_default` |
| 11 | (optional) How do I reach you outside business hours if something is urgent? | `off_hours_contact` |
| 12 | (optional) Anything else you want me to know before I take a real conversation? | `free_form` |

If the principal sends more than one answer in a single message, split it
yourself into the corresponding number of memory entries. Do not skip any
question.

### Phase 3 — Confirmation (single message)

After the last question, write the **summary** in five short lines and ask the
principal to confirm or correct it. Use this exact shape:

1. Address style + language with the principal.
2. Tone with customers.
3. Working hours + timezone.
4. Escalation contact.
5. Things the bot must never do alone or never share.

If the principal corrects any line, update the corresponding memory entry
**before** answering them. Do not batch the corrections for later.

When the principal confirms (any clear "yes / OK / correcto / dale"), send
one closing line. Then the interview is over; do not re-ask any of the
questions in this session.

## Memory Entry Format

Write each entry with:

- A short stable key (snake_case, one of the names in the table above).
- The principal's words, lightly cleaned (no embellishment, no truncation
  that changes meaning).
- The timestamp the answer was received.

One fact per entry. Two facts go in two entries.

## Common Mistakes

| Mistake | Why it fails |
|---------|--------------|
| Sending two or three questions in one message to "save time" | The principal answers both at once and the audit trail collapses |
| Grouping several answers into one "preferences" memory entry | Future lookups can only see the blob, not the individual fact |
| Inventing a sensible default when the principal says "I don't know" | Future-you trusts the entry as if it were said |
| Skipping the confirmation summary "because they already answered" | The principal never validated the bot's reading; silent drift is the most common onboarding failure |
| Re-asking a question because the answer was short | Short answers are answers; ask for clarification only if genuinely ambiguous |
| Using the principal's bot prompt template as the source of preferences | The bot prompt is for customers; preferences for the principal are separate |

## Real-World Impact

- Memory entries written with this skill are queryable in the same shape as
  preferences written later, so no migration is needed when the bot grows up.
- The 5-line summary has caught every drift in the first week of every bot
  Carlos has run since this protocol was introduced.
- The "sin dato" marker for unanswered questions is what makes a later
  follow-up legitimate instead of a re-interview.

## Related

- The Mia / Samy profile of Urbanika Shades is the first bot onboarded with
  this protocol. A literal draft of the messages sent to Romina on day 0 is
  in `RESEARCH/URBANIKA_ENTREVISTA_INICIAL_BORRADOR_2026-09-29.md` (this
  branch).
- The SOUL addendum that points the Mia profile at this skill is in
  `skills/onboarding/entrevista-inicial/SOUL_ADDENDUM_MIA.md` (this branch).