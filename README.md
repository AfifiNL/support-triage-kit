# support-triage-kit

Triage for customer-support tickets: an LLM classifies, prioritises and drafts a first reply,
while deterministic code around it redacts personal data before the model sees anything, routes
legally sensitive tickets whatever the model says, checks every draft, and holds every reply for
a person to approve. Nothing is ever sent automatically.

- **Python service** (FastAPI) with an HMAC-signed webhook for new tickets, a review API for
  people, and an outbox that an integration delivers from.
- **Two importable n8n workflows**: ticket intake (sign and forward) and delivery of approved
  replies.
- **Redaction before the model**: e-mail addresses, phone numbers, card numbers (Luhn), IBANs
  (mod-97), IPv4/IPv6 addresses and names in two narrow patterns become placeholders such as
  `[EMAIL_1]`. Only e-mail addresses, phone numbers and names are ever put back, in the
  reviewer's view and in the approved reply; card numbers, IBANs and IP addresses never are.
- **Rules that win over the model**: data-protection requests go to the privacy office; legal
  threats, chargebacks and safety issues go to an escalations queue with a priority floor.
- **Draft checks**: no promised refunds, compensation, deadlines or completed data deletion; no
  invented contact details; no placeholders that do not exist or that stand for a card number,
  IBAN or IP address; no internal queue names. A flagged draft cannot be approved unedited.
- **An eval set** (60 synthetic tickets, labelled) with a gate that fails CI on a drop, a mock
  model so everything runs offline, and a **recorded live run** (Gemini) that CI replays.

## What it does

`make demo` runs the whole flow in-process, offline, with the mock model (output abridged, long lines wrapped):

```text
== 1. tickets arrive through the signed webhook
   demo-1: 200 {'review_id': 1, 'duplicate': False, 'route': 'billing', 'priority': 'high', 'status': 'ready_for_review'}
   demo-2: 200 {'review_id': 2, 'duplicate': False, 'route': 'privacy-office', 'priority': 'high', 'status': 'ready_for_review'}
   demo-3: 200 {'review_id': 3, 'duplicate': False, 'route': 'accounts', 'priority': 'high', 'status': 'ready_for_review'}
   unsigned request: 401 missing signature header
== 2. what the model saw for demo-3 (personal data replaced)
   I'm locked out of my account and the reset mail never arrives at [EMAIL_1]. Call me on [PHONE_1].
== 3. a person reviews
   review 2 [privacy-office/high] escalations=['data_protection_request'] draft=ready_for_review
   approve demo-3 as drafted: 200
   approve demo-1 with an edited reply: 200
   reject demo-2 (handled by the privacy office): 200
== 4. outbox: approved replies, personal data restored, waiting for delivery
   #1 -> demo-3:
      Hello,

      Thank you for getting in touch about your account. For your security we will verify the
      request first and then help you regain access. Any reset link will go to sam.rivera@example.org.

      Support team
```

## Quick start

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
make install       # .venv with all extras and dev tools (locked)
make demo          # the flow above: webhook -> triage -> review -> outbox, offline
make check         # lint, strict types, 120 tests, the eval gate and the replay gate (what CI runs)
```

Run the API (mock model by default; nothing leaves the machine):

```bash
export TRIAGE_WEBHOOK_SECRET=$(openssl rand -hex 16) TRIAGE_REVIEW_TOKEN=$(openssl rand -hex 16) \
       TRIAGE_OUTBOX_TOKEN=$(openssl rand -hex 16)
make serve         # http://127.0.0.1:8000, OpenAPI docs at /docs
```

Each of the three secrets guards one role (signed intake, people reviewing, the delivery
integration); without it, that part of the API refuses every request.

## How it works

```text
ticket (webhook, HMAC-signed, timestamped)
   │
   ▼
redact ─────── e-mail, phone, card (Luhn), IBAN (mod-97), IP, names (narrow) → [KIND_n]
   │           one value = one placeholder; the map never leaves the service
   ▼
rules ──────── data-protection request · legal threat · chargeback · safety
   │           → forced queue and a priority floor the model cannot lower
   ▼
model ──────── category, priority, one-line summary, reply draft (structured output)
   │           mock (offline) | Gemini | replay of a recorded run
   ▼
merge ──────── rules win; the model may raise a priority, never lower a floor
   │
   ▼
draft checks ── commitments · invented personal data · unknown placeholders · internal names
   │           failed → still queued for a person, flagged, cannot be approved unedited
   ▼
review queue ── a person approves (as drafted or edited) or rejects; append-only event log;
   │           erasure clears the personal data of one review on request
   │
   ▼
outbox ──────── approved reply with e-mail, phone and name restored; an integration delivers
                and acknowledges
```

If the model call fails for any reason, the ticket is still queued for a person, in the queue
the rules pick (or the general queue) with no draft. A redelivered ticket (same id, same
content) is answered from the store without calling the model again; the same id with different
content is refused (409).

### API

| Method | Path | Who | Purpose |
|---|---|---|---|
| `POST` | `/v1/webhooks/tickets` | helpdesk / n8n | new ticket; header `X-Triage-Signature: t=<unix>,v1=<hmac>` over `<t>.<body>`; idempotent per ticket id |
| `GET` | `/v1/reviews?status=pending` | reviewer | queue with triage result and the draft as the customer would see it |
| `GET` | `/v1/reviews/{id}` | reviewer | one review plus its event log |
| `POST` | `/v1/reviews/{id}/approve` | reviewer | `{reviewer, reply?}`; a flagged draft needs an edited `reply` |
| `POST` | `/v1/reviews/{id}/reject` | reviewer | `{reviewer, reason?}`; nothing is sent |
| `POST` | `/v1/reviews/{id}/erase` | reviewer | `{reviewer}`; clears the personal data held for that review |
| `GET` | `/v1/outbox` | integration | approved replies not yet delivered |
| `POST` | `/v1/outbox/{id}/ack` | integration | mark delivered |
| `GET` | `/healthz` | anyone | liveness and the configured model |

Reviewer endpoints take `Authorization: Bearer $TRIAGE_REVIEW_TOKEN`; the outbox endpoints take
`Authorization: Bearer $TRIAGE_OUTBOX_TOKEN`, which cannot approve anything.

### n8n workflows

`workflows/n8n/ticket-intake.json` receives a ticket on an n8n webhook, refuses callers without
the `X-Intake-Token` header (401), builds the JSON body, signs it with the Crypto node
(HMAC-SHA256 over `timestamp.body`, the exact string it then sends) and posts it to the service.
`workflows/n8n/deliver-approved-replies.json` polls the outbox every five minutes, posts each
approved reply to `HELPDESK_REPLY_URL` (or replace that node with your helpdesk's own node), and
acknowledges the outbox item only after the send succeeded. They read `TRIAGE_API_URL`,
`TRIAGE_INTAKE_TOKEN`, `TRIAGE_WEBHOOK_SECRET`, `TRIAGE_OUTBOX_TOKEN`, `HELPDESK_REPLY_URL` and
`HELPDESK_TOKEN` from the n8n environment and contain no credentials. The tests check that they
are well formed, that every node is connected, that unauthenticated intake is refused, and that
every URL they call on this service is a route the API serves.

## Evals

`evals/tickets.jsonl` holds 60 synthetic tickets across eight categories, each labelled with
category, priority, queue, expected escalations, and the exact personal-data strings it contains
(e-mails on example domains, phone numbers in reserved fictional ranges, published test card
numbers and IBANs, documentation IP ranges, invented names). It is split into `dev` (30) and
`test` (30); the split was fixed before any run.

| metric | meaning |
|---|---|
| pii_recall | share of labelled e-mails, phones, cards, IBANs and IPs of which no part is visible to the model (no e-mail local part, no run of 4+ letters or digits; floor 1.0) |
| name_recall | the same for names, word by word (narrow patterns; floor 0.9) |
| escalation_recall | share of labelled escalations the rules raised (floor 1.0) |
| route / category / priority accuracy | exact match with the label |
| urgent_recall | labelled-urgent tickets predicted urgent |
| draft_pass_rate | share of drafts that passed the draft checks |

Floors are in `evals/gate.json` (mock) and `evals/gate_replay.json` (recorded live run); both
were written before the first run. The gate also fails when a score drops against the committed
baseline, or on any model error or replay miss. The tests prove it bites: a model that answers
"other / low" for everything fails it, while redaction and escalation stay at 1.0.

**Offline, mock model** (keyword rules; measures the deterministic layers, not a model):

| scope | pii | names | escalations | route | category | priority | urgent recall | drafts pass |
|---|---|---|---|---|---|---|---|---|
| all (60) | 1.000 | 1.000 | 1.000 | 0.833 | 0.783 | 0.700 | 0.833 | 1.000 |

**Live, `gemini-3.6-flash`** (Vertex AI, EU location, thinking level low), 2026-11-08:

| run | items | route | category | priority | urgent recall | drafts pass |
|---|---|---|---|---|---|---|
| dev, first prompt | 30 | 0.933 | 0.933 | 0.933 | 1.000 | **0.800** |
| dev, after the fix below | 30 | 0.933 | 0.933 | 0.933 | 1.000 | 1.000 |
| **test, run once** | 30 | 0.967 | 1.000 | 0.800 | **0.333** | 1.000 |
| all (recorded cassette, replayed with today's checks) | 60 | 0.950 | 0.967 | 0.867 | 0.667 | 0.950 |

PII, name and escalation recall were 1.000 in every run (they do not depend on the model). The
first three rows were scored with the first version of the draft checks. An independent review
then made them stricter (no card number, IBAN or IP placeholder in a reply; more ways of
promising a refund, a deadline or compensation). Replayed through the current checks, three
recorded drafts are flagged: two mention the customer's card as `[CARD_1]` and one an IP address
as `[IP_1]`.

- **The draft checker caught a real failure.** With the first prompt, 6 of 30 dev drafts greeted
  the customer as `[NAME_1]` in tickets that had no name placeholder; the prompt's own example
  had suggested it. The fix lists the placeholders that exist in each ticket. Without the
  checker, those drafts would have reached a reviewer looking finished.
- **Urgency is the weak spot.** On `test` the model called two of three urgent tickets "high":
  a business customer's integration failing on every request (orders not coming through) and an
  unrecognised login. The third (a charging dock that smelled of burning plastic) was called
  urgent by the model, and the safety rule would have forced it anyway. A missed urgent ticket
  lands in the right team at "high", not on call. Adding rules for these cases now would be tuning on the test split; it needs
  fresh tickets first.
- 30 tickets per split is small: one ticket is 0.033. Treat differences of one ticket as noise.

CI replays the recorded run (`make eval-replay`) at no cost: any change to the prompt, the
redaction or the request format changes the request hash, and the replay fails until someone
re-records it (`make record-live`, which needs credentials and costs money). The live runs above
used 91 calls in total (1 smoke test, 2 × 30 on `dev`, 30 on `test`).

## Configuration

| variable | default | meaning |
|---|---|---|
| `TRIAGE_PROVIDER` | `mock` | `mock`, `gemini` or `replay` |
| `TRIAGE_GEMINI_MODEL` | `gemini-3.6-flash` | model name |
| `TRIAGE_GEMINI_VERTEX` | unset | `true`: Vertex AI with Application Default Credentials and `GOOGLE_CLOUD_PROJECT`; otherwise `GEMINI_API_KEY` |
| `TRIAGE_GEMINI_LOCATION`, `TRIAGE_GEMINI_THINKING_LEVEL` | unset | Vertex location, thinking level |
| `TRIAGE_MAX_LIVE_CALLS` | `0` | hard cap on live calls per process; `0` refuses every live call |
| `TRIAGE_CASSETTE`, `TRIAGE_RECORD_CASSETTE` | | replay from / record to a cassette |
| `TRIAGE_WEBHOOK_SECRET`, `TRIAGE_REVIEW_TOKEN`, `TRIAGE_OUTBOX_TOKEN` | unset | required for the webhook, the review API and the outbox |
| `TRIAGE_DB` | `triage.sqlite3` | review queue and outbox (SQLite) |

## Limits

- **Redaction is pattern-based.** Names are found only after "my name is" and on the line after
  a sign-off ("Kind regards,"). Names elsewhere in the text, postal addresses, dates of birth,
  customer numbers and national id numbers are not redacted. Over-redaction is accepted: a long
  digit run that fails the card and IBAN checks may still be redacted as a phone number.
- **English only.** Rules, mock keywords and draft checks are English patterns.
- **Small eval set**, written by the author of the code. The live numbers are one run each on
  30 tickets; model output varies between runs.
- **The n8n workflows were validated as JSON** (structure, connections, routes), not imported into
  a running n8n. They read `$env`; if your n8n blocks environment access in nodes, use n8n
  variables instead, and prefer n8n's own header-auth credential over the token check node.
  Only n8n exports are included.
- **Tokens, not accounts.** One token for all reviewers and one for the integration; the
  reviewer name is recorded as the caller gives it. No rate limiting. SQLite, single process.
  Put it behind your own authentication before real use.
- **The draft checks look for commitments, not facts.** They over-flag on purpose ("we cannot
  promise a refund" is flagged; a person rewrites it). They do not catch invented facts: one
  recorded live draft states weekend phone hours the ticket never mentioned.
- **The draft policy is an example**: which commitments need a person is a business decision.

## License

Apache-2.0. See [LICENSE](LICENSE). Copyright 2026 Hossam Afifi.
