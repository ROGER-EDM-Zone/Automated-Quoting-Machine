# Quoting Automation

Inbound RFQ email → structured, priced, flagged draft quote → human review →
sent reply, with everything logged.

**Nothing sends without human approval.** The AI drafts; a person approves.

Built to [`QUOTING_APP_BUILD_SPEC.md`](docs/BUILD_SPEC.md). One backend serving
two surfaces: an estimator web app and an Outlook triage add-in.

**Project documents** live in [`docs/`](docs/) and are indexed by
[`docs/index.html`](docs/index.html). Start with the
[setup worklist](docs/SETUP_WORKLIST.html) — what the business has to supply,
decide or switch on before this can price a real enquiry. Rates, the shared
mailbox and 20-30 already-quoted drawings for scoring are the three that block
everything else.

GitHub renders `.html` as source rather than as a page. Switching on GitHub
Pages (Settings → Pages → deploy from the default branch, `/docs` folder) makes
these documents open as pages instead; `docs/index.html` explains it, and notes
that Pages sites are public by default.

---

## The rule everything else follows

> **AI reads and reasons; code calculates.**

Every price in this system comes out of one function,
[`backend/app/pricing.py`](backend/app/pricing.py). That module imports no AI
client, makes no network calls, reads no clock and touches no database — the
caller resolves rates and rules and hands them in as plain data. Given the same
inputs it returns the same numbers every time, and there is a test that greps
the file for AI and network imports so it stays that way.

The AI's job is producing and correcting the *inputs*: reading drawings,
classifying jobs, and turning an estimator's note into a concrete change to an
operation. It never produces the output number.

Six more rules, and where each one actually lives:

| Rule | Where it is enforced |
|---|---|
| A missing value is safer than a confident wrong one | `services/confidence.py` — a field below its threshold is withheld from the part record entirely, so it *cannot* reach pricing |
| Calculator vs AI-estimated numbers look different, always | `SourcedTime` in `frontend/src/components/Primitives.tsx`, guarded by `npm run check:styles` |
| Nothing sends itself | `services/approval.py` — approval is refused while any `block` flag is unresolved, in the service layer where no caller can route around it |
| Rates and rules live in the database | `services/rates.py` has no default rate anywhere in it; a missing rate raises and becomes a blocking flag |
| Log every correction | `PATCH /parts/{id}` writes a `correction_log` row for every changed field, with the AI's confidence and whether the value had been withheld |
| Keep `operation` ERP-clean | Controlled `Process` enum, real op numbers, proper numeric fields. Adding to the enum is a schema decision |

---

## Getting it running

```bash
# Backend
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env

alembic upgrade head
python -m scripts.seed --example     # illustrative rates + a worked enquiry
uvicorn app.main:app --reload        # http://localhost:8000/docs
```

```bash
# Web app, in another terminal
cd frontend
npm install
npm run dev                          # http://localhost:5173
```

The seeded enquiry is already extracted and routed, so
`http://localhost:5173/enquiry/1` shows a full workspace immediately: a data
sheet, three operations with three different time sources, the cost build-up,
and the notes box.

**The seeded rates are invented placeholders** (spec section 9). So are the
margins, the stock prices and the cycle times. `scripts/seed.py` refuses to run
unless `AQM_ENVIRONMENT` is `development` or `test`, because seeding them into a
real database would mean quoting a customer at a number nobody in the business
chose. Real rates go in through `/admin/rates` before anything is quoted.

Extraction and classification need `AQM_ANTHROPIC_API_KEY`. Everything
deterministic — pricing, nesting, the build-up, approval, the reply — works
without one.

```bash
cd backend && pytest -q          # 126 tests
cd frontend && npm run build     # typecheck + style guard + build
```

---

## How an enquiry moves

```
Outlook (tagged mail)
   │  Graph subscription → POST /webhook/outlook
   ▼
Intake            services/intake.py      email + attachments stored, kinds
   │                                      classified, duplicates detected
   ▼
Extraction        services/extraction.py  one vision call per drawing, forced
   │                                      to a JSON schema, confidence per field
   ▼
Classification    services/classification.py  job type, process mix, operation
   │                                          skeleton, historical match
   ▼
Pricing           pricing.py              DETERMINISTIC. no AI.
   │
   ▼
Review            web app + Outlook card   notes → AI proposes an input change
   │                                       → the engine reprices
   ▼
Approve + send    services/approval.py     a person, recorded with who and when
   │              services/reply.py        email rendered from the record
   ▼
Outcome           won/lost + actual minutes → feeds the historical lanes
```

Every stage is its own endpoint and re-runnable in isolation, which is what
lets extraction be scored against real drawings before any UI depends on it —
the measurement the spec calls out as predicting whether this saves time or
just creates a new checking burden.

### Status

`received → extracting → extracted → classified → priced → in_review →
approved → sent → won|lost`, plus `needs_attention` (a blocking flag) and
`failed` (a pipeline error) off to the side.

---

## Design decisions worth knowing about

**An unread quantity is null, not 1.** `part.quantity` is nullable and pricing
refuses to run without it. Drawings frequently do not state a quantity, and a
silent default of 1 is precisely the confidently-wrong value the rest of the
design exists to prevent. Where a quantity does arrive, `quantity_source`
records whether it came from the drawing, the email or a person.

**Three states, not two.** A field is *accepted* (read confidently), *withheld*
(read, but below threshold — the reading is kept and offered back as a question,
never as a value) or *unread* (the model honestly returned null). The UI shows
all three differently, and `correction_log` records which kind a correction hit,
so reporting can separate "confidently wrong" from "flagged and duly corrected".
Those are different failures.

**Ambiguous stays ambiguous.** When the service-only vs full-supply signals
disagree, both cost paths are computed and neither is chosen. Choosing is a
blocking flag for a human.

**Rule values come from the database; rule selection is a human act.** The note
loop may cite an active `rules_table` row — the active keys are baked into the
response schema as an enum, so an invented percentage is *unrepresentable*
rather than merely discouraged. If no rule fits, the AI asks. `min_quote_value`
is the one rule that applies without anyone selecting it.

**Refused AI actions are recorded, not dropped.** When the note loop rejects
something the model proposed, the rejection and its reason are stored on the
note so an estimator can see what the AI wanted to do.

**Two history lanes, never blended.** Geometry match ("have we cut this shape
before") and problem match ("has this kind of job hurt us before") are separate
because an estimator acts on them differently. Both are plain arithmetic over
sent quotes — unsent drafts are not evidence of anything.

**Rate rows are end-dated, never deleted,** and `effective_to` is exclusive, so
a rate and its replacement are never both in force on the changeover day. A
quote sent last month has to stay explicable.

**The rounding residue is reported.** A line's unit price is rounded to 2dp and
the line total is `unit_price × quantity`, so the customer-facing arithmetic
adds up exactly. The few pence that leaves against the raw part value appears in
the build-up as its own line rather than being absorbed.

---

## Layout

```
backend/
  app/
    pricing.py          the deterministic engine — no AI, no I/O
    nesting.py          deterministic material nesting
    models.py           the spec section 2 schema
    enums.py            controlled vocabularies; Process is the ERP surface
    services/
      intake.py         stage 1   confidence.py  the withholding policy
      extraction.py     stage 2   history.py     the two match lanes
      classification.py stage 3   quoting.py     ORM ↔ engine bridge
      notes.py          stage 5   reply.py       stage 6, no AI in it
      approval.py       stage 6   rates.py       no default rate, anywhere
      ai.py             the single seam for every AI call
      graph.py          Microsoft Graph: reads mail, creates drafts, never sends
    prompts/            extraction, classification, note interpretation
    api/                the endpoints
  tests/                126 tests
  scripts/seed.py       development data (placeholders only)
frontend/               React + TypeScript estimator workspace
outlook-addin/          Office.js triage card — read-only by design
```

---

## Deferred, but not designed out

**STEP/3D CAD reading** and **direct ERP integration**, as the spec directs.
STEP files are recognised at intake, stored, and marked in the UI as held but
not read, so adding a reader is new work rather than a migration. The
`operation` table is kept ERP-clean — proper fields, controlled process enum,
real op numbers, never free text — so the ERP link is a data push later rather
than a rebuild. An admin retypes those same rows by hand today.

## Open decisions (spec section 8)

Two are already configurable: per-field confidence thresholds
(`AQM_CONFIDENCE_THRESHOLD_*`) and whether the AI may propose processes the
customer did not name (`AQM_PROPOSE_UNNAMED_PROCESSES` — note that when the
customer *did* name processes, the routing is constrained to those regardless).

Three still need the business: how many alternative routes to surface, where
cycle times come from per process, and who reviews promoted rules and how often.
`/admin/rules/promotion-candidates` lists recurring notes as suggestions; it
never creates a rule.

## Production notes

- Set `AQM_AUTH_REQUIRED=true`. With it false, approvals are recorded against an
  `X-User-Email` header, which is fine locally and nowhere else. The app logs a
  warning at startup when it is false.
- Set `AQM_GRAPH_WEBHOOK_CLIENT_STATE`. Anyone can POST to the webhook; the
  client state is what proves a notification came from your subscription.
- Graph mail subscriptions expire in under three days —
  `POST /webhook/subscription/{id}/renew` on a schedule.
- Point `AQM_STORAGE_BACKEND=azure` and `AQM_DATABASE_URL` at Postgres or Azure
  SQL. Alembic owns the schema; `init_db()` only runs in development.
