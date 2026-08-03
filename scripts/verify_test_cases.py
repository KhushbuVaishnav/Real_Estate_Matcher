"""
scripts/verify_test_cases.py

Three different kinds of checks, deliberately not treated the same way:

1. HARD INVARIANTS — things guaranteed true by generate_listings.py's logic
   regardless of random seed (e.g. "every condo is single-story" is baked
   into the generator's code, not a coincidence of one random run). These
   get real pass/fail assertions.

2. LIVE FREQUENCY REPORT — keyword counts (quiet, Caltrain, etc.) depend on
   random generation with no fixed seed, so they WILL drift every time
   generate_listings.py runs again. These are measured fresh and printed,
   not compared against a hardcoded "should be 202" from an old run — do
   that comparison yourself against whatever this prints today.

3. AI SCORING SPOT-CHECK — calls the real Claude/OpenAI API and prints
   actual scores/reasons for manual review. LLM output isn't perfectly
   reproducible even at low temperature, so this is a spot-check for
   "does this look right," not a strict pass/fail assertion. Requires a
   real API key and costs a small amount of real usage — skipped unless
   you pass --with-ai.

Run:
    python scripts/verify_test_cases.py                # tiers 1 + 2 only
    python scripts/verify_test_cases.py --with-ai       # + tier 3 (real API calls)
"""

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.services.listings_service import HardFilters, fetch_listings, normalize_listing


def section(title):
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


# ---------------------------------------------------------------------------
# Tier 1 — hard invariants
# ---------------------------------------------------------------------------

def run_invariants():
    section("TIER 1: Hard invariants (guaranteed regardless of random seed)")
    failures = []

    def check(label, condition):
        status = "PASS" if condition else "FAIL"
        if not condition:
            failures.append(label)
        print(f"  [{status}] {label}")

    all_listings = fetch_listings(HardFilters())
    total = len(all_listings)
    check(f"Dataset loads and has listings (got {total})", total > 0)

    # Every condo is single-story by generator design (line: "is_single_story
    # = True if is_condo else ..."), so these two filters must produce the
    # exact same count no matter how the data was regenerated.
    condos = fetch_listings(HardFilters(property_types=["Condominium"]))
    condos_single_story = fetch_listings(HardFilters(property_types=["Condominium"], max_stories=1))
    check(
        f"Every condo is single-story ({len(condos)} condos == {len(condos_single_story)} single-story condos)",
        len(condos) == len(condos_single_story)
    )

    # Single-story + 2+ stories must partition the dataset exactly (every
    # listing has a stories value of 1 or 2, no overlap, no gaps).
    single = fetch_listings(HardFilters(max_stories=1))
    multi = fetch_listings(HardFilters(min_stories=2))
    check(
        f"Single-story + multi-story == total ({len(single)} + {len(multi)} == {total})",
        len(single) + len(multi) == total
    )

    # No listing should ever have more than 5 bedrooms — the generator's
    # random.choice pools cap at 5 for single-family, 4 for condos.
    over_5_beds = fetch_listings(HardFilters(min_beds=6))
    check(f"No listing has 6+ bedrooms (got {len(over_5_beds)})", len(over_5_beds) == 0)

    # Excluding ranch-style should reduce the count by exactly the number of
    # ranch listings that actually exist — internal self-consistency check.
    no_ranch = fetch_listings(HardFilters(exclude_styles=["Ranch"]))
    ranch_only = [l for l in all_listings if l.get("property", {}).get("style") == "Ranch"]
    check(
        f"Excluding Ranch removes exactly the Ranch listings ({total} - {len(ranch_only)} == {len(no_ranch)})",
        total - len(ranch_only) == len(no_ranch)
    )

    print(f"\n{len(failures)} failure(s)." if failures else "\nAll invariants passed.")
    return failures


# ---------------------------------------------------------------------------
# Tier 2 — live frequency report (measure, don't hardcode)
# ---------------------------------------------------------------------------

def run_frequency_report():
    section("TIER 2: Live keyword frequency report (measure fresh, don't trust old numbers)")

    with open(settings.GENERATED_DATA_PATH) as f:
        data = json.load(f)
    remarks = [l["remarks"] for l in data]

    def count(keyword):
        return sum(1 for r in remarks if keyword.lower() in r.lower())

    print(f"Total listings: {len(data)}\n")

    rows = [
        ("quiet", count("quiet")),
        ("busy", count("busy")),
        ("cul-de-sac", count("cul-de-sac")),
        ("updated kitchen (has 'kitchen', minus dated)", count("kitchen") - count("has not been updated") - count("opportunity to renovate")),
        ("dated/original kitchen", count("has not been updated") + count("opportunity to renovate")),
        ("home office", count("home office")),
        ("no office", count("no dedicated office") + count("no separate room")),
        ("single-level/no stairs/zero-step", count("single-level") + count("no interior stairs") + count("zero-step")),
        ("split across two levels/upstairs", count("split across two levels") + count("all located upstairs")),
        ("pool and spa", count("pool and spa")),
        ("neighborhood park", count("neighborhood park")),
        ("caltrain/shops", count("caltrain")),
        ("HOA landscaping", count("hoa covers landscaping")),
    ]
    for label, n in rows:
        print(f"  {label:<45} {n}")


# ---------------------------------------------------------------------------
# Tier 3 — real AI scoring spot-check (needs a real API key, costs real usage)
# ---------------------------------------------------------------------------

def run_ai_spotcheck():
    section("TIER 3: AI scoring spot-check (real API calls — review output manually)")
    from app.services.matching_service import rank_listings

    test_cases = [
        "quiet street, updated kitchen, home office, pool",
        "single-level home, no stairs, walk-in shower, small easy-care yard",
        "walkable to Caltrain and a quiet street",
    ]

    all_listings = [normalize_listing(r) for r in fetch_listings(HardFilters())]

    for prefs in test_cases:
        print(f"\n--- Preferences: \"{prefs}\" ---")
        try:
            ranked = rank_listings(prefs, all_listings)
        except RuntimeError as e:
            print(f"  ERROR: {e}")
            continue

        print(f"  {len(ranked)} listings passed SCORE_THRESHOLD={settings.SCORE_THRESHOLD}")
        for l in ranked[:3]:
            print(f"    [{l['match_score']}] {l['address']} — {l['match_reason'][:90]}")


if __name__ == "__main__":
    failures = run_invariants()
    run_frequency_report()

    if "--with-ai" in sys.argv:
        run_ai_spotcheck()
    else:
        print("\n(Skipped AI scoring spot-check — run with --with-ai to include it. "
              "This makes real API calls and uses real usage/cost.)")

    sys.exit(1 if failures else 0)
