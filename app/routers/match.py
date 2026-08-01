"""
app/routers/match.py

/match          — full pipeline, blocking single call. No cancellation once
                  it starts (kept for simple/scripted use — the CLI and
                  Swagger docs use this one).
/match/start    — same pipeline, but runs in a background thread and returns
                  a job_id immediately. This is what the frontend uses, so it
                  can poll for progress and genuinely cancel mid-search.
/match/{id}       — poll for a job's current status/progress/results.
/match/{id}/cancel — request cancellation. Stops before the NEXT batch is
                    sent; a batch already in flight to Claude/OpenAI still
                    completes (can't recall a request already sent), but no
                    further batches after it will be queued.

These are the only files that import matching_service — /listings never
does, which is what guarantees "Browse all" can never call the AI.
"""

from fastapi import APIRouter, HTTPException

from app.models import MatchRequest
from app.services.listings_service import (
    HardFilters, fetch_listings, normalize_listing, filter_by_school_rating,
)
from app.services import matching_service

router = APIRouter()


def _build_filters(request: MatchRequest) -> HardFilters:
    return HardFilters(
        min_price=request.filters.min_price,
        max_price=request.filters.max_price,
        min_beds=request.filters.min_beds,
        min_baths=request.filters.min_baths,
        min_sqft=request.filters.min_sqft,
        cities=request.filters.cities,
        property_types=request.filters.property_types,
        max_hoa=request.filters.max_hoa,
        min_stories=request.filters.min_stories,
        max_stories=request.filters.max_stories,
        exclude_styles=request.filters.exclude_styles,
    )


def _get_filtered_listings(request: MatchRequest) -> list[dict]:
    filters = _build_filters(request)
    try:
        raw = fetch_listings(filters)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Listings source request failed: {e}")

    listings = [normalize_listing(r) for r in raw]
    return filter_by_school_rating(listings, request.filters.min_school_rating, request.filters.strict_school_rating or False)


@router.post("/match")
def match_listings(request: MatchRequest):
    """Hard filters, then AI scores each listing against preferences. Blocking, no cancellation."""
    listings = _get_filtered_listings(request)
    if not listings:
        return {"count": 0, "matches": []}

    try:
        ranked = matching_service.rank_listings(request.preferences, listings)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))

    return {"count": len(ranked), "matches": ranked}


@router.post("/match/start")
def start_match(request: MatchRequest):
    """Kicks off matching in a background thread, returns immediately with a job_id to poll."""
    listings = _get_filtered_listings(request)
    job_id = matching_service.start_match_job(request.preferences, listings)
    job = matching_service.get_job(job_id)
    return {"job_id": job_id, "total_batches": job["total_batches"]}


@router.get("/match/{job_id}")
def get_match_status(job_id: str):
    """Poll this while status is 'running'. Once 'done' or 'cancelled', 'matches' holds the results
    (partial, if cancelled)."""
    job = matching_service.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found — it may have completed and been cleaned up, or the id is wrong.")

    if job["status"] == "error":
        raise HTTPException(status_code=502, detail=job["error"])

    return {
        "status": job["status"],
        "completed_batches": job["completed_batches"],
        "total_batches": job["total_batches"],
        "count": len(job["results"]),
        "matches": job["results"],
    }


@router.post("/match/{job_id}/cancel")
def cancel_match(job_id: str):
    """Requests cancellation. Doesn't stop a batch already in flight, but no further batches
    after it will be sent. Poll /match/{job_id} afterward — status becomes 'cancelled' once the
    in-flight batch (if any) finishes, with whatever was already scored returned as partial results."""
    ok = matching_service.cancel_job(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Job not found.")
    return {"status": "cancel_requested"}
