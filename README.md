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

Two ways to do this, depending on what you're after:

**Live, per-search toggle (no restart needed)** — the "Source" and "Matched
by" dropdowns in the app header let you switch on the fly and apply
immediately to your very next search. This only affects that one request —
it doesn't change any file, and resets back to the `.env` default the next
time the server restarts. Good for quickly comparing sources or providers
side by side.

**Permanent default** — change `.env`:
```
DATA_SOURCE=realistic     # or: live, sample, generated
AI_PROVIDER=openai        # or: anthropic
```
Restart `uvicorn` (or let `--reload` pick it up) after changing `.env`. This
is what the header dropdowns default to on page load, and what any request
uses if it doesn't specify an override.

**One practical gotcha when switching to `live`:** the City filter defaults
to "Redwood City" (correct for `sample`/`realistic`/`generated`, which are
our own fixed data), but `live` is SimplyRETS' real sandbox and its listings
are in Houston, not Redwood City — clear or change the City field when
testing `live`, or you'll get zero results for reasons that have nothing to
do with your other filters.

## Data sources, what each is for

- **`live`** — real SimplyRETS sandbox API. Small (tens of listings, not
  hundreds), and its `remarks` field is identical boilerplate text on every
  listing — not useful for testing AI matching quality, only for seeing what
  a real MLS feed's response shape looks like.
  **Nothing about this source is fixed** — it's a third-party demo service,
  and its contents can and do change without any action on our end. We've
  observed the total listing count change (65 one day, 45 the next) for
  the city of Houston. In case city could change as we don't own that data, don't
  hardcode an assumed city or count anywhere against this source — check
  what's actually in it first:
  ```bash
  curl -u simplyrets:simplyrets "https://api.simplyrets.com/properties?limit=5" \
    | python3 -c "import json,sys; d=json.load(sys.stdin); [print(l['address']['city']) for l in d]"
  ```
- **`sample`** — a handful of listings you can hand-edit directly for quick,
  controlled tests.
- **`realistic`** — 14 hand-written Redwood City listings with real pricing,
  varied descriptions, school assignments, HOA fees, and a mix of single/
  multi-story and condo/single-family — good for demoing specific scenarios.
- **`generated`** — large (500+ by default) dataset combining real
  neighborhoods with randomized-but-meaningful description templates. Run
  `python scripts/generate_listings.py 2000` to regenerate at any size.
  This is the one to use for realistic-scale filter and matching tests.

  **No fixed random seed:** every run of `generate_listings.py` re-rolls
  *everything* — prices, addresses, beds, stories, descriptions, schools,
  all of it — not just whatever you meant to change. If you're tracking
  specific test-case numbers (e.g. "min_price=2M returns 283 listings"),
  those numbers will drift the next time anyone regenerates the dataset.
  Re-verify against the current file rather than trusting old numbers.

**Cost/latency note at scale:** AI matching batches `BATCH_SIZE` (default 8)
listings per API call. At 500+ listings, that's 60+ calls per search — real
latency and cost, unlike testing against 14 listings. Use hard filters
(price/beds/city/etc.) to narrow the pool before it reaches AI scoring,
exactly like a real production system would — never send an entire
inventory to an LLM per search. Or use "Browse all (skip AI)" in the
frontend to test filters alone with zero AI cost.

**Making matching faster:**
- **`MAX_CONCURRENT_BATCHES`** (default 4) — batches run in parallel, not
  one at a time. Raising this speeds up total wall-clock time for a large
  search, but pushes more simultaneous requests at your AI provider.
  **Don't guess this — check the actual evidence.** Every API call prints a
  line like `[Anthropic rate limits] requests: 9999/10000 remaining` to your
  terminal. If that number stays close to your limit with no retry/429 lines
  showing up, you have real headroom and can raise this — try doubling it
  (e.g. 4 → 8), confirm it's still clean, and go from there. If you start
  seeing `[rate limit] Hit 429, retrying...` lines or the UI's retry-count
  warning, that's your signal you've gone too high for your current account
  tier. Cancellation still works correctly regardless of this setting — no
  new batches get submitted once cancelled, but whichever are already in
  flight are allowed to finish rather than abandoned mid-request.
- **`ANTHROPIC_MODEL` / `OPENAI_MODEL`** — swap to a smaller/faster tier
  (e.g. a Haiku-class Anthropic model) for quicker per-call responses, at
  some cost to reasoning depth. Worth A/B testing quality with
  `scripts/analyze_scores.py` before committing to a smaller model.

## Schools

All school names in this project (`app/data/schools.json`, and the school
assignments in `realistic_listings.json` and `generated_listings.json`) are
entirely fictional — invented names like Cedar Ridge Elementary, Warren
Middle School, and Charter High School. None correspond to real institutions,
and none of the ratings are real either. Treat all of it as synthetic test
data only.

School assignment is tied to each listing's (also fictional) neighborhood —
every listing in a given neighborhood shares the same three schools, similar
to how real school district boundaries work — rather than assigned randomly
per listing.

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
