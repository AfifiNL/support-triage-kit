# Design notes

## Goals, in priority order

1. No personal data reaches the model, and nothing reaches a customer without a person's yes.
2. Tickets that carry legal or safety weight reach the right people whatever the model says.
3. Every model-written draft is checked before a person sees it; a failed check is visible.
4. Quality is measured on a labelled set, and regressions fail CI at zero cost.
5. The model vendor is replaceable, and so is the workflow tool in front of the service.

## Decisions

**The model fills one step; code decides the rest.** Redaction, rules, routing, draft checks and
the review flow are deterministic and tested with scripted models (a model that invents contact
details, one that ignores a data-protection request, one that crashes). These properties do not
depend on the vendor, and the eval proves they stay at 1.0 with a useless model.

**Redact before the model, restore after the person.** Placeholders are typed and numbered
(`[EMAIL_1]`), and one value always maps to one placeholder, so a draft can still say "the link
goes to [EMAIL_1]". The map stays in the service; the reviewer sees the restored reply, because
the reviewer is staff and must see what the customer will get. Checksums decide whether a digit
run is a card (Luhn) or an IBAN (mod-97); a digit run that fails them may still be redacted as a
phone number, because over-redaction is the safe direction. Phone numbers need 9 to 15 digits so
order numbers, dates and amounts survive.

**Rules win over the model, in one direction.** Rules can force a category and a queue and set a
priority floor. The model can raise a priority above a floor but never lower it. Privacy beats
escalations when both match, because data-protection requests carry a statutory deadline.
Patterns were written from general knowledge of these request types, then checked against
near-misses ("screen burn-in", "data plan", "food court").

**A failed draft check never deletes the draft.** Dropping the draft would hide what the model
did. The ticket goes to the queue flagged with the findings, and the API refuses to approve that
draft unedited (HTTP 422); the reviewer writes or edits the reply.

**The policy checks commitments, not tone.** The draft rules block promises a person must make
(refunds, compensation, deadlines, confirmed deletion), invented personal data, placeholders the
ticket does not have, internal queue ids and talk of being an AI. Tone is the reviewer's job.

**Nothing sends.** The service has no outbound channel. Approved replies wait in an outbox that
an integration polls and acknowledges, so delivery, retries and the helpdesk API stay in the
workflow tool, and the service cannot send by mistake.

**Signed, timestamped webhooks; fail closed.** HMAC-SHA256 over `timestamp.body`, a five-minute
window against replays, constant-time comparison. Without a configured secret the webhook
refuses everything rather than accepting unsigned input. Duplicate deliveries of a ticket id
return the existing review (webhooks are delivered at least once).

**SQLite with an append-only event log.** Triggers reject updates and deletes on the event
table. One process, standard library only; a larger deployment would swap the store.

**A hard live-call budget in the provider.** The Gemini adapter refuses to make any call unless
`TRIAGE_MAX_LIVE_CALLS` is set, and stops at that number, so a misconfigured test or eval cannot
spend money. Vendor errors are reported by type and HTTP status only.

**Record once, replay in CI.** The cassette maps a hash of exactly what the model was sent
(system prompt, user prompt, output schema) to its answer. Replay is strict: an unseen request
fails the gate with "re-record". Re-recording reuses unchanged calls, so it costs only the diff.

## Calibration

| setting | value | evidence |
|---|---|---|
| gate floors | `evals/gate.json`, `evals/gate_replay.json` | written before the first run; deterministic metrics at 1.0, model metrics at levels a useful triage must reach |
| baseline tolerance | 0.02 (mock), 0.00 (replay) | the mock and the replay are deterministic, so any change is real |
| prompt change | placeholder list per ticket | made on `dev` after 6/30 drafts used a placeholder that did not exist; `test` was run once afterwards |
