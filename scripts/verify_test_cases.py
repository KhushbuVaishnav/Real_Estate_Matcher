"""
scripts/verify_test_cases.py

Three different kinds of checks, deliberately not treated the same way:

1. HARD INVARIANTS — things guaranteed true by generate_listings.py's logic
   regardless of random seed (e.g. "every condo is single-story" is baked
   into the generator's code, not a coincidence of one random run). These
   get real pass/fail assertions.

2. SNAPSHOT TEST — keyword counts (quiet, Caltrain, etc.) depend on random
   generation, so they're only stable BETWEEN regenerations of
   generated_listings.json, not across them. This is snapshot/golden-file
   testing: the first run saves current counts to
   scripts/frequency_baseline.json; every run after that is a REAL pass/fail
   against that saved baseline. If nobody regenerates the dataset, these
   numbers must never drift — any difference is a genuine bug (e.g. someone
   edited generate_listings.py's sentence pools without realizing it shifted
   frequencies). When you DO intentionally regenerate the dataset, run with
   --update-baseline once to accept the new numbers going forward.

3. AI SCORING SPOT-CHECK — calls the real Claude/OpenAI API and prints
   actual scores/reasons for manual review. LLM output isn't perfectly
   reproducible even at low temperature, so this is a spot-check for
   "does this look right," not a strict pass/fail assertion. Requires a
   real API key and costs a small amount of real usage — skipped unless
   you pass --with-ai.

Run:
    python scripts/verify_test_cases.py                  # tiers 1 + 2 (snapshot check)
    python scripts/verify_test_cases.py --update-baseline # after intentionally regenerating data
    python scripts/verify_test_cases.py --with-ai         # + tier 3 (real API calls)

WHEN A CHECK FAILS, WHAT DO YOU ACTUALLY DO:

  Tier 1 (hard invariant) fails
    -> --update-baseline does NOTHING for this, it's not baseline-based.
       This means generate_listings.py's own logic broke a guarantee it's
       supposed to always hold (e.g. a condo somehow ended up multi-story).
       Go fix the generator's code. Never "fix" this by updating a baseline.

  Tier 2 (keyword snapshot) fails
    -> Ask yourself: did I just run generate_listings.py on purpose?
         YES -> --update-baseline is correct and expected. The numbers
                changing is the intended consequence of regenerating.
         NO  -> Investigate first. Don't reflexively run --update-baseline
                to "make the red go away" — that would silently accept a
                real regression (a bug, an accidental edit, something
                overwriting the data file) as if it were normal.
"""

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.services.listings_service import HardFilters, fetch_listings, normalize_listing

BASELINE_PATH = Path(__file__).resolve().parent / "frequency_baseline.json"


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
# Tier 2 — snapshot test (real pass/fail against a saved baseline, not a
# hardcoded number in source code)
# ---------------------------------------------------------------------------

def _compute_frequencies():
    with open(settings.GENERATED_DATA_PATH) as f:
        data = json.load(f)
    remarks = [l["remarks"] for l in data]

    def count(keyword):
        return sum(1 for r in remarks if keyword.lower() in r.lower())

    return {
        "total_listings": len(data),
        "quiet": count("quiet"),
        "busy": count("busy"),
        "cul_de_sac": count("cul-de-sac"),
        "updated_kitchen": count("kitchen") - count("has not been updated") - count("opportunity to renovate"),
        "dated_kitchen": count("has not been updated") + count("opportunity to renovate"),
        "home_office": count("home office"),
        "no_office": count("no dedicated office") + count("no separate room"),
        "single_level": count("single-level") + count("no interior stairs") + count("zero-step"),
        "multi_level": count("split across two levels") + count("all located upstairs"),
        "pool_and_spa": count("pool and spa"),
        "neighborhood_park": count("neighborhood park"),
        "caltrain": count("caltrain"),
        "hoa_landscaping": count("hoa covers landscaping"),
    }


def run_frequency_snapshot_test(update_baseline: bool):
    section("TIER 2: Keyword frequency snapshot test")
    current = _compute_frequencies()

    if update_baseline or not BASELINE_PATH.exists():
        with open(BASELINE_PATH, "w") as f:
            json.dump(current, f, indent=2)
        reason = "requested via --update-baseline" if update_baseline else "no baseline existed yet — creating one now"
        print(f"Baseline written to {BASELINE_PATH} ({reason}).")
        for label, n in current.items():
            print(f"  {label:<20} {n}")
        return []

    with open(BASELINE_PATH) as f:
        baseline = json.load(f)

    failures = []
    for label, expected in baseline.items():
        actual = current.get(label)
        status = "PASS" if actual == expected else "FAIL"
        if actual != expected:
            failures.append(f"{label}: expected {expected}, got {actual}")
        print(f"  [{status}] {label:<20} expected {expected}, got {actual}")

    if failures:
        print(f"\n{len(failures)} drift(s) detected.")
        print("  Did you just run generate_listings.py on purpose?")
        print("    YES -> re-run with --update-baseline to accept these as the new expected values.")
        print("    NO  -> don't run --update-baseline. Investigate first — this is a real")
        print("           regression (a bug, an accidental edit, something changed the data")
        print("           or generator unexpectedly), not something to silently accept.")
    else:
        print("\nNo drift — matches the saved baseline exactly.")

    return failures


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
            req_info = f" ({l['requirements_met']}/{l['requirements_total']} requirements met)" if l.get("requirements_total") else ""
            print(f"    [{l['match_score']}]{req_info} {l['address']} — {l['match_reason'][:90]}")


if __name__ == "__main__":
    failures = run_invariants()
    failures += run_frequency_snapshot_test(update_baseline="--update-baseline" in sys.argv)

    if "--with-ai" in sys.argv:
        run_ai_spotcheck()
    else:
        print("\n(Skipped AI scoring spot-check — run with --with-ai to include it. "
              "This makes real API calls and uses real usage/cost.)")

    sys.exit(1 if failures else 0)
