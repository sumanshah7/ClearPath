# ClearPath PA

Prior authorization workspace for real insurer PDFs (Evidence of Coverage, clinical policies, drug criteria). Documents are extracted with LLMs, a second model judges risk, and a human decides at every gate before anything goes live.

Synthetic patients are used for demos. Production rules always come from uploaded documents — never from a hardcoded service catalog.

## Repo layout

| Path | Role |
|------|------|
| `pa-engine/` | FastAPI extract / review / check / FHIR engine |
| `web/` | Next.js admin + doctor + insurer UI |
| `docs/pa-constitution/` | Product rules and decision log (`memory.md`) |
| `data/` | Local SQLite / JSON demo data (DBs and uploads are gitignored) |

## Quick start

```bash
# 1. Python env
python3.11 -m venv .venv
.venv/bin/pip install -r pa-engine/requirements.txt

# 2. Secrets (never commit .env)
cp .env.example .env
# edit .env — set OPENAI_API_KEY (and optional GROK_/GEMINI_ judge keys)

# 3. API
cd pa-engine && ../.venv/bin/uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

In another terminal:

```bash
cd web && npm install && npm run dev
```

Open [http://localhost:3000](http://localhost:3000).

## What you can do

1. **Policy library** — upload a text-layer EOC / clinical PDF, review extracted coverage or rules, Accept / Edit / Reject, then **Go Live**.
2. **PA listing reconcile** (benefit summaries) — attach a prior-auth category listing PDF so PA flags match the listing catalog (e.g. 41 required / 8 conditional / 41 not required of 90).
3. **Order desk** — pick patient + live plan + service; engine matches coverage and builds criteria / questionnaire.
4. **Clinician / insurer** — answer questions, submit packet; demo insurer never auto-denies.

## PA listing reconcile

```bash
curl -sS -X POST \
  "http://127.0.0.1:8000/policies/{POLICY_ID}/reconcile-pa" \
  -F "listing=@/path/to/prior_authorization.pdf" \
  -F "listing_alt=@/path/to/prior_authorization_fhir.pdf"
```

When two listings are uploaded, the engine keeps the parse with more complete service names. Listing rows become the Gate-1 catalog; unmatched EOC fragments are dropped from the review queue.

## Tests

```bash
cd /path/to/clearpath-pa
.venv/bin/pytest pa-engine/tests/ -q
```

## Environment

Copy `.env.example` → `.env`. Important variables:

- `OPENAI_API_KEY` / `OPENAI_MODEL` — extract + most LLM stages  
- `JUDGE_PROVIDER` — `gemini` \| `grok` \| etc.  
- `GROK_API_KEY` / `GEMINI_API_KEY` — judge provider keys  
- `TIMEOUT_EXTRACT_SECONDS` — raise for dense EOCs (e.g. `240`)  
- `DEMO_MODE` — `true` uses fixtures without live model calls  

**`.env` is gitignored.** Do not commit API keys.

## Deploy web (Vercel)

The Next.js app lives in `web/`. The FastAPI engine (`pa-engine/`) must be hosted separately (Railway, Render, Fly, etc.) — Vercel serves the UI.

```bash
cd web
npx vercel          # preview
npx vercel --prod   # production (confirm first)
```

In the Vercel project settings (Root Directory = `web`), set:

| Variable | Example |
|----------|---------|
| `PA_ENGINE_URL` | `https://your-pa-engine.example.com` |
| `NEXT_PUBLIC_PA_ENGINE_URL` | leave empty to keep using `/engine` proxy, or set the same public API URL |

Local rewrites still point at `http://127.0.0.1:8000` when those vars are unset.

## Safety / product notes

- No denial path in the demo insurer (`approved` / `info_requested` only).  
- Go Live requires a human decision on every active item.  
- Rejected rows stay out of the live catalog and FHIR InsurancePlan.
