# Real Estate Matcher

Search single-family/condo listings with structured filters (price, beds,
schools, HOA, accessibility), then have AI re-rank results by reading each
listing's actual description against your freeform preferences.

## Project structure

```
real-estate-matcher/
├── app/
│   ├── main.py              # FastAPI app assembly — run this with uvicorn
│   ├── config.py             # Settings, all driven by .env — no editing code to change behavior
│   ├── models.py              # Pydantic request/response schemas
│   ├── routers/
│   │   ├── listings.py        # POST /listings — hard filters only, never calls AI
│   │   └── match.py           # POST /match — hard filters + AI scoring
│   ├── services/
│   │   ├── listings_service.py   # Fetch + filter logic, all data sources
│   │   ├── matching_service.py   # AI scoring, Anthropic or OpenAI behind one switch
│   │   └── schools_service.py    # School ratings lookup
│   └── data/                  # JSON datasets (sample / realistic / generated / schools)
├── scripts/
│   ├── generate_listings.py   # Regenerate the large synthetic dataset
│   ├── analyze_scores.py      # See real AI score distributions (for tuning SCORE_THRESHOLD)
│   └── run_cli.py             # Standalone pipeline run, no API server needed
├── frontend/                  # Dependency-free React UI (React+Babel via CDN, no build step)
├── .env.example                # Copy to .env and fill in
└── requirements.txt
```

**Why this layout:** routers handle HTTP only (parse request, call a service,
shape the response); services hold the actual logic (fetching, filtering,
scoring) and have no idea they're being called from an API — you could call
them from a CLI script, a cron job, or a test with zero changes, which is
exactly what `scripts/run_cli.py` and `scripts/analyze_scores.py` already do.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
```

Open `.env` and set:
- Your real `ANTHROPIC_API_KEY` (or `OPENAI_API_KEY` if using `AI_PROVIDER=openai`)
- `DATA_SOURCE` — start with `generated` for a large, varied test set

## Running the API

```bash
uvicorn app.main:app --reload
```
(must be run from the project root — the `app.main:app` path depends on it)

Open **http://127.0.0.1:8000/docs** for interactive Swagger docs.

## Running the frontend

```bash
cd frontend
python3 -m http.server 5500
```
Open **http://127.0.0.1:5500**. It calls `http://127.0.0.1:8000` by default —
change `API_BASE` at the top of `frontend/app.jsx` if your API runs elsewhere.

## Switching data source or AI provider

No code edits needed — just change `.env`:
```
DATA_SOURCE=realistic     # or: live, sample, generated
AI_PROVIDER=openai        # or: anthropic
```
Restart `uvicorn` (or let `--reload` pick it up) after changing `.env`.

## Data sources, what each is for

- **`live`** — real SimplyRETS sandbox API. Small (65 listings), fixed,
  Redwood City-only, and its `remarks` field is identical boilerplate text
  on every listing — not useful for testing AI matching quality, only for
  seeing what a real MLS feed's response shape looks like.
- **`sample`** — a handful of listings you can hand-edit directly for quick,
  controlled tests.
- **`realistic`** — 14 hand-written Redwood City listings with real pricing,
  varied descriptions, school assignments, HOA fees, and a mix of single/
  multi-story and condo/single-family — good for demoing specific scenarios.
- **`generated`** — large (500+ by default) dataset combining real
  neighborhoods with randomized-but-meaningful description templates. Run
  `python scripts/generate_listings.py 2000` to regenerate at any size.
  This is the one to use for realistic-scale filter and matching tests.

**Cost/latency note at scale:** AI matching batches `BATCH_SIZE` (default 8)
listings per API call. At 500+ listings, that's 60+ calls per search — real
latency and cost, unlike testing against 14 listings. Use hard filters
(price/beds/city/etc.) to narrow the pool before it reaches AI scoring,
exactly like a real production system would — never send an entire
inventory to an LLM per search. Or use "Browse all (skip AI)" in the
frontend to test filters alone with zero AI cost.

## School ratings — important honesty note

`app/data/schools.json` contains **illustrative placeholder ratings**, not
real current data. Before this matters for a real decision, replace with
actual ratings from greatschools.org or their API — ratings change yearly.

## Tuning `SCORE_THRESHOLD`

Don't guess it — measure it. Run:
```bash
python scripts/analyze_scores.py "your test preferences"
```
This scores every listing with the threshold effectively disabled, prints
every score, and gives you min/max/average plus counts above 40/50/60/70 —
set `SCORE_THRESHOLD` in `.env` based on where a real quality gap appears in
your actual data's distribution, not a number that sounds reasonable.

## Moving to real MLS data

Swap `DATA_SOURCE=live` for a real feed later: SimplyRETS production,
Bridge Interactive (CoreLogic Trestle), or Spark API all use a similar
RESO-based response shape — `listings_service.py`'s `_fetch_from_simplyrets`
function is the only place you'd need to touch. Zillow/Redfin/Trulia do not
offer general-purpose listing APIs; scraping them violates their terms of
service.
