"""
scorer.py — turns a Lead (from lead_tracker.py) into a 0-100 score.

The scoring foundation is now the evidence trail itself, NOT Apollo:
  - Source Corroboration (30 pts) — how many independent sources
    confirmed this lead. This is the primary signal, since it comes
    straight from real pages you can click through to.
  - Source Quality (20 pts)       — weight by how authoritative those
    sources are (an official registry beats a generic search hit).
  - Mission Relevance (20 pts)    — keyword overlap between the
    mission and the page context each source was found in.
  - Apollo Bonus (20 pts)         — ONLY awarded if Apollo enrichment
    was actually run and found a real record. Skipped or "no match"
    both score 0 here — never guessed, never penalized beyond that.
  - Contactability (10 pts)       — only if Apollo contact-pull was
    run and found real people records.

A lead never NEEDS Apollo to score well — a strongly corroborated,
high-relevance lead can hit 70/100 on browser evidence alone before
Apollo enters the picture at all.
"""

# Rough authority tiers for common domains — extend as you find more
# sources worth trusting more (or less).
SOURCE_AUTHORITY = {
    "find-and-update.company-information.service.gov.uk": 20,  # UK Companies House
    "sec.gov": 20,
    "en.wikipedia.org": 12,
    "crunchbase.com": 12,
    "techcrunch.com": 10,
}
DEFAULT_SOURCE_AUTHORITY = 6  # unranked/generic source (e.g. duckduckgo hit)


def _score_corroboration(lead) -> int:
    n = len(lead.distinct_domains)
    if n >= 4:
        return 30
    if n == 3:
        return 25
    if n == 2:
        return 18
    if n == 1:
        return 8
    return 0


def _score_source_quality(lead) -> int:
    if not lead.sources:
        return 0
    scores = [SOURCE_AUTHORITY.get(s["domain"], DEFAULT_SOURCE_AUTHORITY) for s in lead.sources]
    # Best source found sets the ceiling, averaged slightly down by
    # weaker ones so one great source plus junk isn't a free 20.
    best = max(scores)
    avg = sum(scores) / len(scores)
    return round(min(20, (best * 0.7 + avg * 0.3)))


def _score_mission_relevance(lead, mission_keywords: list) -> int:
    if not mission_keywords:
        return 0
    text = " ".join(s.get("source_url", "") for s in lead.sources).lower()
    text += " " + lead.name.lower()
    hits = sum(1 for kw in mission_keywords if kw.lower() in text)
    if hits == 0:
        return 0
    return min(20, round(20 * hits / max(len(mission_keywords), 1)) + 4)


def _score_apollo_bonus(lead) -> int:
    if not lead.apollo_checked or not lead.apollo_record:
        return 0
    record = lead.apollo_record
    score = 0
    if record.get("website_url"):
        score += 5
    if record.get("estimated_num_employees"):
        score += 5
    if record.get("total_funding"):
        score += 5
    if record.get("industry"):
        score += 5
    return min(score, 20)


def _score_contactability(lead) -> int:
    if not lead.apollo_contacts:
        return 0
    n = len(lead.apollo_contacts)
    return min(10, round(10 * n / 5))


def score_lead(lead, mission_keywords: list = None) -> dict:
    """
    lead: a Lead object from lead_tracker.py (confirmed OR
    single_source — both are scoreable, corroboration is just part
    of the score rather than a pass/fail gate).
    """
    mission_keywords = mission_keywords or []

    breakdown = {
        "source_corroboration": _score_corroboration(lead),
        "source_quality": _score_source_quality(lead),
        "mission_relevance": _score_mission_relevance(lead, mission_keywords),
        "apollo_bonus": _score_apollo_bonus(lead),
        "contactability": _score_contactability(lead),
    }
    total = sum(breakdown.values())

    return {
        "name": lead.name,
        "confidence": lead.confidence,
        "total": total,
        "breakdown": breakdown,
        "sources": [s["domain"] or s["source_url"] for s in lead.sources],
        "apollo_checked": lead.apollo_checked,
        "has_apollo_record": bool(lead.apollo_record),
    }


def score_all(tracker, mission_keywords: list = None) -> list:
    """Scores every lead in the tracker, confirmed and single-source alike."""
    scored = [score_lead(lead, mission_keywords) for lead in tracker.all_leads()]
    scored.sort(key=lambda s: s["total"], reverse=True)
    return scored


if __name__ == "__main__":
    from lead_tracker import LeadTracker

    tracker = LeadTracker()
    tracker.record("Example Robotics Ltd", "browser.hunt_wikipedia",
                    "https://en.wikipedia.org/wiki/List_of_private_equity_firms")
    tracker.record("Example Robotics Ltd", "browser.hunt_companies_house",
                    "https://find-and-update.company-information.service.gov.uk/search?q=Example+Robotics")
    tracker.record("Single Mention Corp", "browser.hunt_generic_search",
                    "https://duckduckgo.com/html/?q=Single+Mention+Corp")

    results = score_all(tracker, mission_keywords=["robotics"])
    for r in results:
        print(f"{r['name']}: {r['total']}/100  [{r['confidence']}]")
        for dim, val in r["breakdown"].items():
            print(f"    {dim}: {val}")
