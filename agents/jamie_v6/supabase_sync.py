"""
supabase_sync.py — pushes a scored lead list (from scorer.py) into
Supabase, so it shows up on the Render dashboard.

Every row carries its full evidence trail (sources, confidence tier,
score breakdown, and Apollo data if enrichment was run) — the
dashboard shows the same provenance you see in the terminal, not just
a bare name and number.

One-time setup — run this in the Supabase SQL editor before first use:

    create table jamie_leads (
        id bigint generated always as identity primary key,
        mission text not null,
        name text not null,
        confidence text not null,
        total_score int not null,
        breakdown jsonb not null,
        sources jsonb not null,
        apollo_checked boolean not null default false,
        has_apollo_record boolean not null default false,
        apollo_contacts_count int not null default 0,
        created_at timestamptz not null default now(),
        unique (mission, name)
    );

The unique constraint on (mission, name) means re-running the same
mission updates each lead's row instead of duplicating it.
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")


class SupabaseSyncError(Exception):
    pass


def push_leads(mission: str, scored_leads: list) -> dict:
    """
    scored_leads: output of scorer.score_all() — a list of dicts with
    name, confidence, total, breakdown, sources, apollo_checked,
    has_apollo_record.

    Returns {"pushed": int, "failed": int}. Never raises on a partial
    failure — reports it so you can see exactly what did and didn't
    make it to the dashboard, rather than silently losing rows.
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise SupabaseSyncError("SUPABASE_URL / SUPABASE_KEY not set in .env")

    if not scored_leads:
        print("  (nothing to push — empty lead list)")
        return {"pushed": 0, "failed": 0}

    endpoint = f"{SUPABASE_URL}/rest/v1/jamie_leads"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",  # upsert on (mission, name)
    }

    rows = []
    for lead in scored_leads:
        rows.append({
            "mission": mission,
            "name": lead["name"],
            "confidence": lead["confidence"],
            "total_score": lead["total"],
            "breakdown": lead["breakdown"],
            "sources": lead.get("sources", []),
            "apollo_checked": lead.get("apollo_checked", False),
            "has_apollo_record": lead.get("has_apollo_record", False),
            "apollo_contacts_count": lead.get("apollo_contacts_count", 0),
        })

    try:
        res = requests.post(endpoint, headers=headers, json=rows, timeout=20)
        if res.status_code in (200, 201, 204):
            print(f"  📡 Pushed {len(rows)} lead(s) to Supabase for mission \"{mission}\"")
            return {"pushed": len(rows), "failed": 0}
        else:
            print(f"  ⚠️  Supabase push failed: HTTP {res.status_code} — {res.text[:300]}")
            return {"pushed": 0, "failed": len(rows)}
    except requests.exceptions.RequestException as e:
        print(f"  ⚠️  Supabase push failed: {e}")
        return {"pushed": 0, "failed": len(rows)}


def push_mission_results(mission: str, tracker, mission_keywords: list = None):
    """
    Convenience wrapper: scores every lead in a LeadTracker and pushes
    the result straight to Supabase in one call. This is what main.py
    should call at the end of run_mission().
    """
    from scorer import score_all

    scored = score_all(tracker, mission_keywords=mission_keywords)
    for s in scored:
        s["apollo_contacts_count"] = 0  # filled in below if available

    # Attach contact counts from the tracker (scorer doesn't carry this)
    by_name = {lead.name.lower(): lead for lead in tracker.all_leads()}
    for s in scored:
        lead = by_name.get(s["name"].lower())
        if lead:
            s["apollo_contacts_count"] = len(lead.apollo_contacts)

    return push_leads(mission, scored)


if __name__ == "__main__":
    # Manual smoke test against a fake scored list — does NOT require
    # a live mission run, just confirms the Supabase call shape works.
    fake_scored = [{
        "name": "Example Robotics Ltd",
        "confidence": "confirmed",
        "total": 57,
        "breakdown": {
            "source_corroboration": 18, "source_quality": 19,
            "mission_relevance": 20, "apollo_bonus": 0, "contactability": 0,
        },
        "sources": ["en.wikipedia.org", "find-and-update.company-information.service.gov.uk"],
        "apollo_checked": False,
        "has_apollo_record": False,
    }]
    result = push_leads("smoke test mission", fake_scored)
    print(result)
