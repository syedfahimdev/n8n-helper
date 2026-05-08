# Telegram Job Scorer — Design Spec

**Status:** Approved 2026-05-08, awaiting implementation plan
**Owner:** Syed Fahim
**Repo:** `github.com/syedfahimdev/n8n-helper`

## 1. Goal

Paste a job URL into Telegram → get an ATS-style match score against the user's master resume → if the score is below the apply threshold, the bot offers to tailor the resume → user approves → bot delivers a polished, tailored PDF and saves a copy alongside the master.

## 2. Locked decisions

These were resolved through brainstorming and are not open for re-litigation in the implementation plan unless new evidence emerges.

| Decision | Choice | Rationale |
|---|---|---|
| Storage backend | Supabase Storage + Postgres | Free tier, Python client, bundled DB for state |
| Scoring strategy | ATS deterministic on every URL; AI recruiter take on demand | ATS keyword score is the gate at most companies; LLM-based screeners are secondary |
| Score thresholds | ≥90% apply, 65–89% tailor offered, <65% stretch warning | Tailoring lifts ~20–30 points; below 65% can't reach 90% even after tailoring |
| Routing | Approach 3: deterministic n8n SWITCH + LLM only for tailoring/recruiter take | Most messages are URLs or button taps — no LLM needed to route |
| PDF rendering | Gotenberg (OSS, self-hosted Docker, alongside n8n) | Free, no rate limits, identical Chromium quality, no vendor lock-in |
| Approval UX | Telegram inline buttons with `tailor:<uuid>` callback_data | Within Telegram's 64-byte callback limit; uuid → Postgres lookup for the URL |
| Resume artifact | Always PDF (markdown is internal intermediate, not user-visible) | User explicitly requested polished PDFs matching the manual builds |

## 3. Architecture

```
┌─────────────────────────────────────────────────────────────┐
│ Telegram Bot                                                │
│  ▲ replies                                                  │
│  │                                                          │
└──┼──────────────────────────────────────────────────────────┘
   │ messages, callbacks
   ▼
┌─────────────────────────────────────────────────────────────┐
│ n8n (self-hosted)                                           │
│   ├── Telegram Message Trigger    ─→ SWITCH by message type │
│   └── Telegram Callback Trigger   ─→ button-tap branch      │
│                                                             │
│   Each branch calls MCP tools, then formats Telegram reply  │
└──────┬─────────────────────────────────────────┬────────────┘
       │ HTTP                                    │ HTTP
       ▼                                         ▼
┌────────────────────┐                ┌──────────────────────┐
│ n8n-helper MCP     │                │ Gotenberg (Docker)   │
│  (Vercel)          │                │  HTML → PDF          │
│   - Tools layer    │                └──────────────────────┘
│   - Bearer auth    │
└────────┬───────────┘
         │
         ▼
┌─────────────────────────────────────────────┐
│ Supabase                                    │
│   - Storage bucket "resumes"                │
│   - Postgres tables: master_resume,         │
│     pending_tailor_offers                   │
└─────────────────────────────────────────────┘
```

Three layers, three concerns:

- **Telegram** owns the chat surface and inline-button UX.
- **n8n** owns orchestration, routing, and presentation (sending replies + attachments).
- **n8n-helper MCP** owns the data and logic — every capability that requires reading the master resume, calling Jina, calling Anthropic, or writing to Supabase lives here.

## 4. MCP tool catalog

All tools follow the project conventions: flat scalar inputs, plain-language descriptions, `find_tool`-discoverable.

### 4.1 Existing (no change)

- `find_tool(query, limit)`
- `health()`

### 4.2 New tools (this spec)

#### `upload_master_resume(pdf_url: str) -> dict`

Save the user's uploaded PDF as the master resume.

- Downloads the PDF from `pdf_url` (typically a Telegram-hosted file URL).
- Extracts text via PyMuPDF; rejects PDFs with no extractable text (image-only).
- Uploads PDF to Supabase Storage at `resumes/master.pdf`, replacing any prior master.
- UPSERTs the `master_resume` singleton row in Postgres with the extracted text.
- Returns `{ ok, length, replaced_previous }`.

#### `get_master_resume_status() -> dict`

Cheap status check used by other flows before they assume a master exists.

- Returns `{ exists, uploaded_at, length }` from the `master_resume` row.
- `exists: false` triggers the "upload your resume first" reply in n8n.

#### `score_job_url(url: str) -> dict`

Fetch a JD and score the master resume against it.

- Fetches JD via Jina Reader (`r.jina.ai/<url>`); response cached in-process for 5 min so repeat calls (e.g., re-score after tailoring is internal to `tailor_resume_for_jd`, but a curious user might re-paste) are cheap.
- Loads cached master text from Postgres.
- Computes deterministic ATS score (port of the scoring algorithm in `score_resume.py`).
- Returns `{ score, verdict, missing_terms[5], jd_title, company, fetched_url }`.
- `verdict` ∈ `"apply"` (≥90), `"tailor"` (65–89), `"stretch"` (<65).

#### `tailor_resume_for_jd(url: str) -> dict`

LLM-tailor the master resume to a specific JD and prepare HTML for PDF rendering.

- Fetches JD (cached or refetch via Jina).
- Loads master MD from Postgres.
- Calls Anthropic (Haiku by default; model overridable via env) with a prompt that:
  - Preserves all factual claims from the master (no fabrication).
  - Adjusts emphasis, phrasing, and keyword density to mirror JD's must-have terms.
  - Returns markdown.
- Renders MD → HTML using the existing template style (Calibri, navy section headers, 2-page layout).
- Re-scores the tailored MD against the JD before returning, so the caller does not have to make a second tool call to confirm the lift.
- Derives `jd_slug` from the JD's title (kebab-case lowercased, trimmed to 32 chars; collisions resolved by the unix-ts suffix the caller appends).
- Returns `{ html, jd_slug, predicted_score, missing_after }`.

#### `ai_recruiter_take(url: str) -> dict`

Holistic LLM judgment of fit, separate from ATS scoring.

- Fetches JD; reads master text.
- Calls Anthropic with a "you are a senior recruiter, judge fit" prompt.
- Returns `{ verdict, why }` where verdict ∈ `"strong"`, `"borderline"`, `"stretch"`.

#### `list_tailored_resumes(limit: int = 10) -> list[dict]`

Recent tailored copies (used for review / re-download).

- Lists files in `resumes/tailored/` newest first.
- Returns `[{ filename, jd_slug, created_at, signed_download_url }, ...]`.

### 4.3 Tool surface size

After this spec: 8 MCP tools (2 existing + 6 new). Within the discovery threshold; `find_tool` keeps them findable but n8n can also list them all in its dropdown.

## 5. Data model

### 5.1 Supabase Storage layout

```
resumes/
  master.pdf
  tailored/
    <jd_slug>-<unix_ts>.pdf
    junction-csm-1715146834.pdf
    dailypay-csm-1715146900.pdf
    ...
```

`jd_slug` is derived from the JD title (kebab-case, lowercased, max 32 chars) plus the company prefix when present. Collisions are resolved by the timestamp suffix.

### 5.2 Postgres tables

```sql
CREATE TABLE master_resume (
  id            int PRIMARY KEY DEFAULT 1 CHECK (id = 1),  -- single-row table
  text          text NOT NULL,
  pdf_path      text NOT NULL DEFAULT 'resumes/master.pdf',
  uploaded_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE pending_tailor_offers (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  chat_id       text NOT NULL,
  url           text NOT NULL,
  score         real NOT NULL,
  missing_terms text[] NOT NULL DEFAULT '{}',
  created_at    timestamptz NOT NULL DEFAULT now(),
  expires_at    timestamptz NOT NULL DEFAULT (now() + interval '1 hour'),
  status        text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','approved','expired'))
);

CREATE INDEX ON pending_tailor_offers (chat_id, created_at DESC);
```

A `pg_cron` job runs daily to delete rows where `expires_at < now()`.

## 6. Message flows

### 6.1 Flow A — Upload master resume

```
Telegram (PDF attachment)
  → n8n SWITCH detects document, mime_type=application/pdf
  → Telegram getFile API → file URL
  → MCP.upload_master_resume(file_url)
  → Telegram reply: "✅ Master saved. Paste a job URL to score."
```

Edge: non-PDF attachment → "PDF only please." Image-only PDF → "Text-based PDF only please."

### 6.2 Flow B — Paste a job URL

```
Telegram (text containing https://…)
  → n8n SWITCH: URL regex matches
  → MCP.get_master_resume_status() → if no master, reply "Upload your resume first" → STOP
  → MCP.score_job_url(url)
  → SWITCH on verdict:
     "apply"   → "✅ <score>% — strong ATS match. <title>. Top missing: […]"
     "tailor"  → INSERT pending_tailor_offers; reply with inline buttons
                 [Tailor it] (callback_data="tailor:<uuid>")  [Skip]
     "stretch" → reply "🚫 <score>% — stretch role." [See gaps] [Skip]
```

### 6.3 Flow C — Tap "Tailor it" button

```
Telegram callback_query data="tailor:<uuid>"
  → n8n Callback Trigger
  → Postgres SELECT pending_tailor_offers WHERE id = uuid AND status = 'pending'
  → if not found / expired → reply "Offer expired. Resend the URL." → STOP
  → MCP.tailor_resume_for_jd(url)            # returns html, jd_slug, predicted_score
  → HTTP POST html → Gotenberg → PDF binary
  → Supabase Storage PUT resumes/tailored/<jd_slug>-<ts>.pdf
  → Postgres UPDATE pending_tailor_offers SET status='approved'
  → Telegram reply + PDF attachment:
      "🎯 Tailored. New ATS score: <predicted_score>% (was <old>%). PDF attached."
```

### 6.4 On-demand — `/recruiter <url>`

```
Telegram text matches /recruiter prefix
  → n8n SWITCH
  → MCP.ai_recruiter_take(url)
  → reply "Borderline — <one-line reason>"
```

## 7. Error handling

| Failure | Behavior |
|---|---|
| Jina Reader 4xx/5xx | "Couldn't fetch the JD. Try again or paste the JD text directly." |
| Anthropic tailor call fails | "Tailoring failed: <reason>. Tap Tailor it again to retry." Offer stays `pending`. |
| Tailored re-score still <90% | "Tailored to X%. Still below 90% — retry with different focus? [Retry] [Accept anyway]" |
| Pending offer >1h old | Soft-expire on read; reply "Offer expired. Resend the URL." Hard-delete via `pg_cron` daily. |
| Supabase upload fails | "Saved locally but storage upload failed. PDF attached anyway." The file is the deliverable. |
| User pastes 2 URLs fast | Each is an independent row in `pending_tailor_offers`; uuids never collide. |
| PDF >50MB (theoretical, not real for resumes) | Send a Supabase signed download URL instead of the binary. |
| Bad MCP bearer token | n8n surfaces the 401 directly; user-side fix is rotating `N8N_HELPER_TOKEN`. |

Idempotency rule: every MCP tool returns deterministic outputs for the same inputs (modulo Jina cache). Re-running after a failure is safe.

## 8. Testing

### 8.1 Unit tests (`pytest`, in repo)

- Scoring algorithm: feed JD text + resume text, assert score in expected range
- `find_tool` ranking: known queries → expected top match
- PDF text extraction: known PDF → expected text
- Slug derivation: known JD titles → expected slugs

### 8.2 Integration tests (mocked Supabase + Anthropic)

- `upload_master_resume`: given a Telegram file URL, asserts Postgres row inserted + Storage upload happened
- `tailor_resume_for_jd`: mocks Anthropic, asserts HTML output structure + correct storage path
- `score_job_url`: mocks Jina, asserts score envelope shape

### 8.3 Smoke tests (manual, post-deploy)

Documented in `docs/smoke-tests.md`:

1. Telegram → upload PDF → confirm "Master saved" reply
2. Telegram → paste a known job URL → confirm score reply with correct verdict band
3. Tap Tailor it → confirm tailored PDF arrives + new score reply
4. Wait 70 minutes, tap an old Tailor it button → confirm "Offer expired"
5. Send `/recruiter <url>` → confirm AI take reply
6. Bad token → confirm 401 surfaced

### 8.4 CI

GitHub Actions runs unit + integration on every push to `main`. Smoke tests are manual.

## 9. Out of scope (deferred)

These are deliberately not part of v1:

- Job-board search (`search_jobs(query)` across Indeed / LinkedIn / ZipRecruiter via JobSpy) — relevant only after URL-paste path is solid.
- Cover-letter draft generation as a tool (we draft these manually for now via Telegram conversation).
- Multi-user / OAuth — bearer token only; a single user.
- Application tracking / outcomes log — a future `applications` table will record which jobs you actually applied to and whether you got a callback. Not in v1.
- Resume version diff — currently each tailored copy is independent, no relation to prior versions.
- Web UI — Telegram is the only interface.

## 10. Open questions for the implementation plan

The implementation plan (next step) should explicitly resolve:

1. **Anthropic model choice for tailoring** — Haiku 4.5 by default; verify quality on 3 sample JDs before locking. Fallback: Sonnet 4.6 if Haiku tailoring underscores frequently.
2. **Master-text refresh policy** — when the user uploads a new master, do tailored copies also need to be regenerated? Spec says no (each tailored copy is a snapshot). Confirm during testing.
3. **Slug collisions** — unix-ts suffix should make collisions impossible in practice; confirm by test.
4. **Vercel cold-start cost** — measure how long the first request after idle takes; if >5s, consider switching `tailor_resume_for_jd` to a non-Vercel runtime.

## 11. Acceptance criteria

This v1 ships when:

- A new user can upload a PDF resume via Telegram and see "Master saved."
- Pasting a known job URL returns a score within ±5% of the manual `score_resume.py` baseline.
- Score ≥90% returns the "apply" reply path with no tailor offer.
- Score 65–89% returns inline buttons; tapping "Tailor it" produces a polished PDF on Telegram with a new score.
- Score <65% returns the stretch warning.
- A pending offer older than 1h replies "expired" when tapped.
- All four smoke tests in §8.3 pass on a clean Vercel deploy.
