"""
app/models.py

Pydantic request/response models. Separated from routers so the shape of
the API is defined in one place, independent of the handler logic.
"""

from pydantic import BaseModel, Field
from typing import Optional


class ListingQuery(BaseModel):
    min_price: Optional[int] = Field(None, example=None)
    max_price: Optional[int] = Field(None, example=None)
    min_beds: Optional[int] = Field(None, example=None)
    min_baths: Optional[float] = Field(None, example=None)
    min_sqft: Optional[int] = Field(None, example=None)
    cities: Optional[list[str]] = Field(None, example=["Redwood City"])
    min_school_rating: Optional[int] = Field(
        None, example=None,
        description="1-10. Only has effect on listings with school_ratings data. By default filters by AVERAGE of elementary/middle/high ratings — see strict_school_rating for an all-must-pass alternative."
    )
    strict_school_rating: Optional[bool] = Field(
        None, example=None,
        description="If true, every assigned school (not just the average) must individually meet min_school_rating."
    )
    property_types: Optional[list[str]] = Field(
        None, example=None,
        description='e.g. ["SingleFamilyResidence"] or ["Condominium"].'
    )
    max_hoa: Optional[int] = Field(
        None, example=None,
        description="Monthly HOA fee ceiling in dollars. Listings with no HOA always pass."
    )
    min_stories: Optional[int] = Field(
        None, example=None,
        description="e.g. 2 to require 2+ stories."
    )
    max_stories: Optional[int] = Field(
        None, example=None,
        description="e.g. 1 to require single-story (no stairs)."
    )
    exclude_styles: Optional[list[str]] = Field(
        None, example=None,
        description='e.g. ["Ranch"] — excludes listings with that architectural style.'
    )
    data_source: Optional[str] = Field(
        None, example=None,
        description='Overrides DATA_SOURCE from .env for this request only. One of: "live", "sample", "realistic", "generated".'
    )


class MatchRequest(BaseModel):
    filters: ListingQuery
    preferences: str = Field(
        ...,
        example="Quiet street, updated kitchen, a spare room for a home office, not near a busy road."
    )
    ai_provider: Optional[str] = Field(
        None, example=None,
        description='Overrides AI_PROVIDER from .env for this request only. One of: "anthropic", "openai".'
    )
