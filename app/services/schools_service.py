"""
app/services/schools_service.py

Loads app/data/schools.json and attaches ratings to listings. Kept separate
from listings_service so a real ratings API (GreatSchools, etc.) can be
swapped in later without touching the listings pipeline.
"""

import json
from app.config import settings

with open(settings.SCHOOLS_PATH) as f:
    _SCHOOLS_DB = json.load(f)
    _SCHOOLS_DB.pop("_note", None)


def lookup_school(name: str) -> dict | None:
    return _SCHOOLS_DB.get(name)


def attach_school_ratings(listing: dict, schools: dict) -> dict:
    ratings = {}
    for level, school_name in (schools or {}).items():
        info = lookup_school(school_name)
        ratings[level] = {"name": school_name, "rating": info["rating"] if info else None}
    return {**listing, "school_ratings": ratings}
