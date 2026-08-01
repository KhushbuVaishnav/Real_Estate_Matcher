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
from app.config import settings

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


def _score_batch_anthropic(user_preferences: str, listings_batch: list[dict]) -> list[dict]:
    import anthropic

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    user_message = f"Buyer wants: {user_preferences}\n\nListings:\n{json.dumps(_build_listing_payload(listings_batch), indent=2)}"

    try:
        response = client.messages.create(
            model=settings.ANTHROPIC_MODEL,
            max_tokens=settings.MAX_TOKENS,
            temperature=settings.TEMPERATURE,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
    except anthropic.AuthenticationError as e:
        raise RuntimeError(
            "Anthropic authentication failed — check ANTHROPIC_API_KEY in .env and that billing is set up."
        ) from e
    except anthropic.NotFoundError as e:
        raise RuntimeError(f"Model '{settings.ANTHROPIC_MODEL}' not found or not available on your account.") from e
    except anthropic.APIStatusError as e:
        raise RuntimeError(f"Anthropic API error ({e.status_code}): {e.message}") from e

    return _parse_response_text(response.content[0].text)


def _score_batch_openai(user_preferences: str, listings_batch: list[dict]) -> list[dict]:
    from openai import OpenAI, AuthenticationError, NotFoundError, APIStatusError

    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    user_message = f"Buyer wants: {user_preferences}\n\nListings:\n{json.dumps(_build_listing_payload(listings_batch), indent=2)}"

    try:
        response = client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            temperature=settings.TEMPERATURE,
            max_tokens=settings.MAX_TOKENS,
        )
    except AuthenticationError as e:
        raise RuntimeError(
            "OpenAI authentication failed — check OPENAI_API_KEY in .env and that billing is set up."
        ) from e
    except NotFoundError as e:
        raise RuntimeError(f"Model '{settings.OPENAI_MODEL}' not found or not available on your account.") from e
    except APIStatusError as e:
        raise RuntimeError(f"OpenAI API error ({e.status_code}): {e.message}") from e

    return _parse_response_text(response.choices[0].message.content)


def score_batch(user_preferences: str, listings_batch: list[dict]) -> list[dict]:
    if settings.AI_PROVIDER == "openai":
        return _score_batch_openai(user_preferences, listings_batch)
    return _score_batch_anthropic(user_preferences, listings_batch)


def rank_listings(user_preferences: str, listings: list[dict]) -> list[dict]:
    """Batches, scores, merges, filters by threshold, sorts best-first.
    Synchronous, blocking, no cancellation — kept for the CLI script and
    anything that just wants a single call/response. The API's /match/start
    endpoint uses the job-based version below instead, which supports
    real mid-search cancellation.
    """
    scores_by_id = {}
    for i in range(0, len(listings), settings.BATCH_SIZE):
        batch = listings[i:i + settings.BATCH_SIZE]
        for r in score_batch(user_preferences, batch):
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


def start_match_job(user_preferences: str, listings: list[dict]) -> str:
    job_id = str(uuid.uuid4())
    total_batches = (len(listings) + settings.BATCH_SIZE - 1) // settings.BATCH_SIZE if listings else 0

    job = {
        "status": "running",       # running | done | cancelled | error
        "results": [],
        "error": None,
        "cancel_event": threading.Event(),
        "total_batches": total_batches,
        "completed_batches": 0,
    }
    with _jobs_lock:
        _jobs[job_id] = job

    thread = threading.Thread(target=_run_job, args=(job_id, user_preferences, listings), daemon=True)
    thread.start()
    return job_id


def _run_job(job_id: str, user_preferences: str, listings: list[dict]):
    job = _jobs[job_id]
    cancel_event = job["cancel_event"]
    scores_by_id = {}

    try:
        for i in range(0, len(listings), settings.BATCH_SIZE):
            if cancel_event.is_set():
                break
            batch = listings[i:i + settings.BATCH_SIZE]
            for r in score_batch(user_preferences, batch):
                scores_by_id[str(r["mls_id"])] = r
            job["completed_batches"] += 1

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
