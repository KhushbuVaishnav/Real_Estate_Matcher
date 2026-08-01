"""
app/services/matching_service.py

Scores listings against a buyer's freeform preferences using whichever AI
provider is configured (settings.AI_PROVIDER). Previously this was two
separate files (semantic_match.py / semantic_match_openai.py) that required
commenting/uncommenting an import line in app.py to switch — that's fragile
and easy to forget. Now it's one env var: AI_PROVIDER=anthropic|openai.
"""

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from app.config import settings, VALID_AI_PROVIDERS

SYSTEM_PROMPT = """You are a real estate matching assistant. You will be given:
1. A buyer's freeform description of what they want in a home.
2. A batch of listings, each with an id, basic specs (including stories,
   property_type, and hoa_fee when available), a free-text description, and
   school_ratings — a 1-10 rating for the assigned elementary/middle/high
   school, when available.

For EACH listing, score how well it matches what the buyer is looking for.
Weigh the free-text description primarily (since specs like beds/baths/price
were already filtered upstream), but factor in structured fields when
relevant: school_ratings when the buyer mentions schools/kids/family; stories
and description details like "no stairs," "main-floor bedroom," or
"walk-in shower" when the buyer mentions accessibility, elderly family
members, or single-level living; hoa_fee and property_type when the buyer
mentions condos, HOA costs, or low-maintenance living. Look for things a
keyword search would miss: implied features, phrasing, tone, lifestyle fit,
dealbreakers mentioned or conspicuously absent.

When the buyer names multiple distinct requirements (e.g. "walkable to
Caltrain and a quiet street," or "home office and a pool"), first silently
count N = the number of distinct requirements named, and M = how many of
them this specific listing's description clearly satisfies. Use this as a
hard ceiling on the score, then adjust within it based on overall fit:
  - M = N (all requirements met): score can range up to 100.
  - M = N-1 (all but one met): score must not exceed 55, even if the listing
    is otherwise excellent. A missing explicit requirement is a real gap,
    not a minor deduction.
  - M <= N-2 (missing two or more): score must not exceed 30.
This ceiling applies even when your reason explains the miss sympathetically
("close, but doesn't mention X") — the numeric score must still respect the
ceiling. Do not let a strong match on the requirements that ARE met pull the
score above its ceiling.

Worked example: buyer says "walkable to Caltrain and a quiet street" (N=2).
A listing describes a peaceful cul-de-sac (quiet: met) but never mentions
Caltrain, transit, or downtown walkability (Caltrain: not met). M=1, N=2, so
M=N-1 applies: score must be 55 or below, e.g. 45, with a reason like "Meets
the quiet-street requirement but doesn't mention Caltrain access, an explicit
requirement — capped below 55 despite otherwise fitting well." A listing
meeting neither (M=0, N=2) should score 30 or below.

Respond with ONLY a JSON array, no other text, in this exact shape:
[
  {"mls_id": "...", "score": 0-100, "reason": "one sentence, specific, citing what in the listing drove the score"}
]
"""


def _build_listing_payload(listings_batch: list[dict]) -> list[dict]:
    return [
        {
            "mls_id": l["mls_id"],
            "price": l["price"],
            "beds": l["beds"],
            "baths": l["baths"],
            "sqft": l["sqft"],
            "stories": l.get("stories"),
            "property_type": l.get("property_type"),
            "hoa_fee": l.get("hoa_fee"),
            "description": l["description"],
            "school_ratings": l.get("school_ratings"),
        }
        for l in listings_batch
    ]


def _parse_response_text(text: str) -> list[dict]:
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print("Warning: failed to parse model output, skipping batch:\n", text)
        return []


def _retry_with_backoff(fn, max_attempts=4, base_delay=2.0, on_retry=None):
    """
    Retries fn() on rate-limit errors with exponential backoff (2s, 4s, 8s...).
    Rate limits (too many requests/tokens per minute) are expected, temporary
    conditions — especially now that batches run concurrently
    (MAX_CONCURRENT_BATCHES) — not a real failure, so retrying briefly is the
    standard approach rather than immediately surfacing an error to the user.
    Any other exception (auth, not-found, etc.) is raised immediately, since
    those won't resolve by waiting.

    on_retry(attempt, delay), if given, is called each time a retry is about
    to happen — used by the job runner to surface "this is retrying" to the
    frontend via the poll endpoint, not just as a terminal print.
    """
    import time
    import anthropic
    import openai

    for attempt in range(max_attempts):
        try:
            return fn()
        except (anthropic.RateLimitError, openai.RateLimitError):
            if attempt == max_attempts - 1:
                raise
            delay = base_delay * (2 ** attempt)
            print(f"[rate limit] Hit 429, retrying in {delay:.0f}s (attempt {attempt + 1}/{max_attempts})...")
            if on_retry:
                on_retry(attempt + 1, delay)
            time.sleep(delay)


def _log_rate_limit_headers(provider: str, headers) -> None:
    """
    Prints the current rate-limit standing after every API call, straight
    to the uvicorn terminal — the most precise, real-time source for this
    (no Console dashboard lag). Anthropic and OpenAI use different header
    names for the same concepts, so each is read separately; missing
    headers are shown as '?' rather than crashing, since header sets can
    change between SDK/API versions.
    """
    if provider == "Anthropic":
        req_remaining = headers.get("anthropic-ratelimit-requests-remaining", "?")
        req_limit = headers.get("anthropic-ratelimit-requests-limit", "?")
        tok_remaining = headers.get("anthropic-ratelimit-tokens-remaining", "?")
        tok_limit = headers.get("anthropic-ratelimit-tokens-limit", "?")
        reset = headers.get("anthropic-ratelimit-requests-reset", "?")
    else:  # OpenAI
        req_remaining = headers.get("x-ratelimit-remaining-requests", "?")
        req_limit = headers.get("x-ratelimit-limit-requests", "?")
        tok_remaining = headers.get("x-ratelimit-remaining-tokens", "?")
        tok_limit = headers.get("x-ratelimit-limit-tokens", "?")
        reset = headers.get("x-ratelimit-reset-requests", "?")

    print(
        f"[{provider} rate limits] requests: {req_remaining}/{req_limit} remaining  |  "
        f"tokens: {tok_remaining}/{tok_limit} remaining  |  resets: {reset}"
    )


def _score_batch_anthropic(user_preferences: str, listings_batch: list[dict], on_retry=None) -> list[dict]:
    import anthropic

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    user_message = f"Buyer wants: {user_preferences}\n\nListings:\n{json.dumps(_build_listing_payload(listings_batch), indent=2)}"

    def call():
        # with_raw_response gives access to the actual HTTP headers (rate
        # limit info) alongside the parsed response — .parse() below gets
        # you the normal Message object, same as client.messages.create()
        # would have returned directly.
        raw = client.messages.with_raw_response.create(
            model=settings.ANTHROPIC_MODEL,
            max_tokens=settings.MAX_TOKENS,
            temperature=settings.TEMPERATURE,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        _log_rate_limit_headers("Anthropic", raw.headers)
        return raw.parse()

    try:
        response = _retry_with_backoff(call, on_retry=on_retry)
    except anthropic.RateLimitError as e:
        raise RuntimeError(
            "Anthropic rate limit hit repeatedly even after retrying — you're sending requests "
            "faster than your account's current limit allows. Try lowering MAX_CONCURRENT_BATCHES "
            "in .env, or check your rate limits in the Anthropic Console."
        ) from e
    except anthropic.AuthenticationError as e:
        raise RuntimeError(
            "Anthropic authentication failed — check ANTHROPIC_API_KEY in .env and that billing is set up."
        ) from e
    except anthropic.NotFoundError as e:
        raise RuntimeError(f"Model '{settings.ANTHROPIC_MODEL}' not found or not available on your account.") from e
    except anthropic.APIStatusError as e:
        raise RuntimeError(f"Anthropic API error ({e.status_code}): {e.message}") from e

    return _parse_response_text(response.content[0].text)


def _score_batch_openai(user_preferences: str, listings_batch: list[dict], on_retry=None) -> list[dict]:
    import openai
    from openai import OpenAI, AuthenticationError, NotFoundError, APIStatusError

    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    user_message = f"Buyer wants: {user_preferences}\n\nListings:\n{json.dumps(_build_listing_payload(listings_batch), indent=2)}"

    def call():
        raw = client.chat.completions.with_raw_response.create(
            model=settings.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            temperature=settings.TEMPERATURE,
            max_tokens=settings.MAX_TOKENS,
        )
        _log_rate_limit_headers("OpenAI", raw.headers)
        return raw.parse()

    try:
        response = _retry_with_backoff(call, on_retry=on_retry)
    except openai.RateLimitError as e:
        raise RuntimeError(
            "OpenAI rate limit hit repeatedly even after retrying — you're sending requests faster "
            "than your account's current limit allows. Try lowering MAX_CONCURRENT_BATCHES in .env, "
            "or check your rate limits at platform.openai.com."
        ) from e
    except AuthenticationError as e:
        raise RuntimeError(
            "OpenAI authentication failed — check OPENAI_API_KEY in .env and that billing is set up."
        ) from e
    except NotFoundError as e:
        raise RuntimeError(f"Model '{settings.OPENAI_MODEL}' not found or not available on your account.") from e
    except APIStatusError as e:
        raise RuntimeError(f"OpenAI API error ({e.status_code}): {e.message}") from e

    return _parse_response_text(response.choices[0].message.content)


def score_batch(user_preferences: str, listings_batch: list[dict], ai_provider: str = None, on_retry=None) -> list[dict]:
    provider = ai_provider or settings.AI_PROVIDER
    if provider not in VALID_AI_PROVIDERS:
        raise ValueError(f"ai_provider must be one of {VALID_AI_PROVIDERS}, got '{provider}'")
    if provider == "openai":
        return _score_batch_openai(user_preferences, listings_batch, on_retry)
    return _score_batch_anthropic(user_preferences, listings_batch, on_retry)


def rank_listings(user_preferences: str, listings: list[dict], ai_provider: str = None) -> list[dict]:
    """Batches, scores, merges, filters by threshold, sorts best-first.
    Synchronous, blocking, no cancellation — kept for the CLI script and
    anything that just wants a single call/response. The API's /match/start
    endpoint uses the job-based version below instead, which supports
    real mid-search cancellation.

    Batches run concurrently (up to settings.MAX_CONCURRENT_BATCHES at once)
    rather than one at a time — for a large search this is the single
    biggest lever on total wall-clock time, since network/API latency per
    batch otherwise adds up linearly.
    """
    batches = [listings[i:i + settings.BATCH_SIZE] for i in range(0, len(listings), settings.BATCH_SIZE)]
    scores_by_id = {}

    with ThreadPoolExecutor(max_workers=settings.MAX_CONCURRENT_BATCHES) as executor:
        futures = [executor.submit(score_batch, user_preferences, batch, ai_provider) for batch in batches]
        for future in as_completed(futures):
            for r in future.result():
                scores_by_id[str(r["mls_id"])] = r  # str() — model may return ids as strings even when source has ints

    ranked = []
    for listing in listings:
        result = scores_by_id.get(str(listing["mls_id"]))
        if not result:
            continue
        if result["score"] >= settings.SCORE_THRESHOLD:
            ranked.append({**listing, "match_score": result["score"], "match_reason": result["reason"]})

    ranked.sort(key=lambda x: x["match_score"], reverse=True)
    return ranked


# ---------------------------------------------------------------------------
# Job-based matching with real mid-search cancellation.
#
# Each search runs in a background thread. Between batches (not mid-batch —
# an already-sent API call can't be recalled), the thread checks a
# threading.Event. If it's set, the loop stops immediately: no further
# batches get sent, and whatever was already scored is returned as partial
# results. Jobs live in an in-memory dict — fine for a single-user local
# app; a real multi-user deployment would use Redis or a proper task queue
# (Celery, RQ) instead, since this dict is lost on server restart and
# doesn't work across multiple server processes.
# ---------------------------------------------------------------------------

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def start_match_job(user_preferences: str, listings: list[dict], ai_provider: str = None) -> str:
    job_id = str(uuid.uuid4())
    total_batches = (len(listings) + settings.BATCH_SIZE - 1) // settings.BATCH_SIZE if listings else 0

    job = {
        "status": "running",       # running | done | cancelled | error
        "results": [],
        "error": None,
        "cancel_event": threading.Event(),
        "total_batches": total_batches,
        "completed_batches": 0,
        "in_flight_count": 0,      # how many batches are actively running right now, at this instant
        "retry_count": 0,          # cumulative retries triggered so far across the whole job
        "job_lock": threading.Lock(),  # protects retry_count from concurrent batch threads
    }
    with _jobs_lock:
        _jobs[job_id] = job

    thread = threading.Thread(target=_run_job, args=(job_id, user_preferences, listings, ai_provider), daemon=True)
    thread.start()
    return job_id


def _run_job(job_id: str, user_preferences: str, listings: list[dict], ai_provider: str = None):
    """
    Runs up to settings.MAX_CONCURRENT_BATCHES batches at once instead of
    one-at-a-time — the sliding-window pattern below submits a fresh batch
    each time one finishes, keeping that many in flight simultaneously.
    Cancellation still works the same as before: once cancel_event is set,
    no NEW batches are submitted, but whichever are already in flight (up to
    MAX_CONCURRENT_BATCHES of them) are allowed to finish rather than
    abandoned mid-request.
    """
    job = _jobs[job_id]
    cancel_event = job["cancel_event"]
    scores_by_id = {}
    batches = [listings[i:i + settings.BATCH_SIZE] for i in range(0, len(listings), settings.BATCH_SIZE)]
    batch_iter = iter(batches)

    def on_retry(attempt, delay):
        with job["job_lock"]:
            job["retry_count"] += 1

    try:
        with ThreadPoolExecutor(max_workers=settings.MAX_CONCURRENT_BATCHES) as executor:
            in_flight = {}  # future -> True, just used as a set with quick membership ops

            def submit_next():
                if cancel_event.is_set():
                    return
                batch = next(batch_iter, None)
                if batch is not None:
                    in_flight[executor.submit(score_batch, user_preferences, batch, ai_provider, on_retry)] = True
                job["in_flight_count"] = len(in_flight)

            for _ in range(settings.MAX_CONCURRENT_BATCHES):
                submit_next()

            while in_flight:
                done, _ = wait(in_flight.keys(), return_when=FIRST_COMPLETED)
                for future in done:
                    for r in future.result():
                        scores_by_id[str(r["mls_id"])] = r
                    job["completed_batches"] += 1
                    del in_flight[future]
                    submit_next()  # keep the window full, unless cancelled

        ranked = []
        for listing in listings:
            result = scores_by_id.get(str(listing["mls_id"]))
            if not result:
                continue
            if result["score"] >= settings.SCORE_THRESHOLD:
                ranked.append({**listing, "match_score": result["score"], "match_reason": result["reason"]})
        ranked.sort(key=lambda x: x["match_score"], reverse=True)

        job["results"] = ranked
        job["status"] = "cancelled" if cancel_event.is_set() else "done"
    except RuntimeError as e:
        job["status"] = "error"
        job["error"] = str(e)


def cancel_job(job_id: str) -> bool:
    """Signals the background thread to stop before its next batch. Returns
    False if the job doesn't exist (already cleaned up, or bad id)."""
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return False
    job["cancel_event"].set()
    return True


def get_job(job_id: str) -> dict | None:
    with _jobs_lock:
        return _jobs.get(job_id)
