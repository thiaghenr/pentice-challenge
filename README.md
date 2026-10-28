# Pentice Pre-Arrival Quiz

A small, production-minded LLM agent. It reads a building's house rules (Markdown at a URL), generates a 5–8 question quiz **grounded in the text**, scores guest attempts deterministically, and shows the front desk which rules guests get wrong most often.

- Specification: [`PRD.md`](PRD.md)
- Guide for AI coding agents: [`CLAUDE.md`](CLAUDE.md)
- Full HTTP flow: [`requests.http`](requests.http) and Swagger at `/docs`

## Setup

Requirements: [uv](https://docs.astral.sh/uv/) (it installs Python 3.12 if needed) and Docker, used only for PostgreSQL.

```bash
cp .env.example .env          # fill in OPS_API_KEY and, if you have one, OPENAI_API_KEY
make install                  # uv sync
make serve-rules              # (another terminal) serves data/house-rules at http://localhost:8001
make dev                      # starts Postgres (make db), runs migrations, starts the API at http://localhost:8000
```

**Database:** PostgreSQL 16. `make db` starts only the Compose `db` service on `localhost:5432` (user/password/database `pentice`, local use only), and the default `DATABASE_URL` already points to it. On first start, Compose also creates the `pentice_test` database used by the tests (`make test` starts `db` first). To use another Postgres, set `DATABASE_URL` (and `TEST_DATABASE_URL` for the tests).

In `.env`, point `HOUSE_RULES_URL` at a raw file/Gist or at the local server:
`HOUSE_RULES_URL=http://localhost:8001/house-rules-harbor-lofts.md`.

**The system still works without an LLM key.** The quiz then comes from the deterministic fallback, with `generation_mode: "fallback_template"`. This makes an offline demo easy.

Other commands: `make test`, `make cov`, `make lint`, `make typecheck`, `make check` (all of them), `make migrate`.

### With Docker

Requirements: Docker with Compose v2. No uv or Python needed on the machine.

```bash
cp .env.example .env          # fill in OPENAI_API_KEY and OPS_API_KEY
make up                       # docker compose up --build -d
make logs                     # follows the API logs
make down                     # stops everything (data stays in the pg-data volume)
```

Compose starts three services:

| Service | Port | What it does |
| --- | --- | --- |
| `db` | 5432 | PostgreSQL 16, data in the `pg-data` volume. Creates `pentice` and `pentice_test` |
| `api` | 8000 | Waits for `db` to be healthy, runs `alembic upgrade head`, starts the API (non-root user, healthcheck on `/healthz`) |
| `rules` | 8001 | Serves `data/house-rules/` (mounted read-only: editing a file changes the next generation) |

Inside Compose, `HOUSE_RULES_URL` is pinned to `http://rules:8001/house-rules-harbor-lofts.md`, overriding the one in `.env`. For Pine Court, send `"source_url": "http://rules:8001/house-rules-pine-court.md"` in `POST /quizzes`. To use a Gist, change the URL in `docker-compose.yml` or send `source_url`. To reset the database: `docker compose down -v` (deletes the `pg-data` volume).

### Running another building without code changes

```bash
curl -X POST localhost:8000/api/v1/quizzes -H "X-API-Key: $OPS_API_KEY" -H "Content-Type: application/json" \
  -d '{"building_slug": "pine-court", "source_url": "http://localhost:8001/house-rules-pine-court.md"}'
```

Changing `HOUSE_RULES_URL` also works. The parser only splits the text on `##` headings, so it behaves the same with bullets (Harbor Lofts) and prose (Pine Court).

## End-to-end flow

```
POST /api/v1/quizzes {"building_slug":"harbor-lofts"}              → 201 quiz without answer key + generation_mode
POST /api/v1/quizzes/{quiz_id}/attempts {"guest_ref":"g-123"}      → 201 attempt (200 if it already existed: reload)
PUT  /api/v1/attempts/{id}/answers/{question_id} {"option_ids":[…]} → 200 (upsert; 409 after submit)
GET  /api/v1/attempts/{id}                                          → state + saved answers
POST /api/v1/attempts/{id}/submit                                   → per-question score, weighted final, passed
GET  /api/v1/ops/quizzes/{quiz_id}/missed-questions                 → ranking of the most missed questions
GET  /api/v1/ops/buildings/{slug}/missed-topics                     → aggregated by topic
GET  /api/v1/ops/guests/{guest_ref}                                 → latest attempt, passed, missed topics
```

Errors always use the format `{"error": {"code": "...", "message": "..."}}`. Every response carries `X-Request-ID`.

## Decisions (PRD §9)

1. **Unsupported question:** every question must quote the rules verbatim (`source_quote`); if the quote is not in the Markdown, the question is discarded and regenerated, so it never reaches a guest.
2. **What "passed" means and who sees it:** weighted score ≥ 75% (`PASS_THRESHOLD_PCT`); the check-in agent looks up the guest before arrival and sees pass/fail, the score and the topics to reinforce (in v1 a JSON response from `/ops/guests/{guest_ref}`; there is no screen yet).
3. **Retakes:** allowed; each retake is a new attempt (`attempt_number + 1`, `previous_attempt_id`) and the stored attempt is never changed.
4. **Immutable after answering:** questions, options, answer key and weights are frozen at publish (an ORM listener blocks `UPDATE`); answers and score are frozen at submit; new rules produce a new quiz, never an edit.
5. **"Most missed" list:** the front desk reinforces those rules at check-in and in the welcome message, and a rule that stays highly missed is flagged to the building as ambiguous wording to rewrite.

### Other decisions of ours

- **Multi-answer penalty:** `score = 4 × max(0, hits − wrong_picks) / total_correct`, so selecting everything never pays off (the brief only said "proportional"). Tests cover it, including hypothesis properties.
- **Weights:** `w_i = 1.1^(i−1)` in the order shown. In v1 questions are not shuffled, so the attempt order is the published order and the weights stored in `question.weight` are the ones used for the score.
- **`POST /quizzes` requires `X-API-Key`:** the route calls the LLM (cost) and fetches an arbitrary URL (SSRF risk), so it cannot be open.
- **Unchanged rules (same `content_hash`):** the existing quiz is reused (`200` with `reused: true`) and the LLM is not called. Send `"force": true` to regenerate.
- **LLM robustness chain:** timeout → retry with backoff (max 2) and a repair prompt carrying the validation errors, asking only for the missing questions → secondary model → cached quiz for the same hash (`fallback_cache`) → template generator (`fallback_template`) → `503`. A quiz is stored only after validation, so a partial quiz is never published. Every call is recorded in `llm_generation_log`.
- **Pass/fail is stored at submit** with the threshold in force at that moment, so changing `PASS_THRESHOLD_PCT` later does not rewrite past results.
- **IDs are UUIDs:** the `attempt_id` works as the guest's access token.

## Data model

`building` → `source_snapshot` (content + sha256) → `quiz` (published, immutable) → `question` / `option`.
`attempt` (per `guest_ref`, freezes `question_order_json`) → `answer` (one per question, upserted until submit).
`llm_generation_log` records every LLM call: provider, model, prompt version, latency, status and validation errors.

- **Reload:** `POST /quizzes/{id}/attempts` is idempotent while there is an `in_progress` attempt for `(quiz_id, guest_ref)` and returns the same attempt with its saved answers. A unique constraint guards against races.
- **Submit:** idempotent. After it, any answer `PUT` gets `409`.

## Architecture

```
src/pentice_quiz/
  main.py          create_app(): injects settings, DB, HTTP client and LLMs (all swappable in tests)
  config.py        pydantic-settings
  errors.py        AppError → HTTP status + code
  observability.py JSON logs with request_id
  domain/          scoring.py (pure), schemas.py, validation.py (schema + grounding + duplicates)
  services/        ingestion, generation (QuizAgent + publish), fallback, attempts, analytics
  llm/             client.py (Protocol + OpenAI-compatible client), prompts.py (versioned)
  api/             routes, response schemas (public and admin kept separate), deps, error handlers
```

## Tests

`make cov`: 133 tests, ~96% total coverage and **100% on `domain/scoring.py`**. No test calls a real LLM or the network. The LLM is a `FakeLLMClient` with scripted replies, the rules come from an `httpx.MockTransport`, and database tests run against a real PostgreSQL. Covered cases include:

- **Scoring:** single answer right and wrong, partial multi-answer, multi-answer with a wrong option picked, "select everything", blank, weighted score, and properties (hypothesis).
- **Validation:** truncated JSON, prose instead of JSON, 3 and 5 options, 0 and 4 correct, duplicate options, unknown section, invented quote, quote from another section or another building, duplicate questions.
- **Fallback:** timeout then success, repair, secondary model, cache, template, and 503 with nothing published.
- **Attempts:** reload, double submit, 409 after submit, retake, single-select with 2 options, option from another question.
- **API:** the full flow for both buildings.

## What is a PoC

- **Authentication:** one shared `X-API-Key` for admin and ops. Guests identify with an opaque `guest_ref`, and the `attempt_id` (UUID) works as a token. No SSO and no rate limiting.
- **Grounding:** the system guarantees the quote exists verbatim in the section, but does not prove it supports the option marked correct. That still depends on the LLM (mitigated by low temperature and the prompt).
- **Template generator:** "fill in the blank" questions about **bold** values that contain numbers, with derived numeric distractors. They are correct and grounded, but less rich than the LLM's. The `is_fallback` flag is exposed in the API.
- **SSRF:** `source_url` accepts any http(s) URL for whoever holds the API key. Production needs a host allowlist.
- **Analytics:** aggregation runs in Python over submitted answers, which is enough at this scale. At high volume it would move to SQL `GROUP BY`.
- **Langfuse:** not integrated. Observability today is JSON logs plus `llm_generation_log`.

## Plan for the rest

| Priority | Item | Status / next step |
|---|---|---|
| P1 | Retake without repeating questions already answered correctly | In `start_attempt`, filter `question_order_json` by the questions with `score == 4` in the previous attempt. The final score uses weights by attempt order, which the code already does. |
| P1 | `by_risk` / `by_past_mistakes` strategies | **Done** (prompt instructions; `by_past_mistakes` uses miss rates by topic). Quality still needs evaluating with a real LLM. |
| P2 | Langfuse | Wrap `QuizAgent._log` and the `OpenAICompatibleClient` calls in a trace. |
| P2 | Jinja2 + HTMX UI | `/ui/attempts/{id}` using the same services. |
| P2 | Dockerfile + Postgres | **Done.** Postgres 16 in Compose; tests run against a real Postgres. For production: backups and a sized connection pool. |
| — | Shuffle questions per attempt | Already supported by the model (`question_order_json`). Generate the order with a seed derived from the `attempt_id`. |
