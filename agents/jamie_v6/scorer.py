"""
scorer.py — turns a VERIFIED Apollo organization record into a 0-100
lead score across five dimensions.

Hard rule: every point awarded traces back to a real field Apollo
actually returned. A missing field scores 0 for that piece — it is
never estimated, guessed, or defaulted to a "reasonable-sounding"
mid-range number. An unverified candidate (no Apollo record) cannot
be scored at all; score_lead() refuses and tells you why, rather than
inventing a score for a company that was never confirmed to exist.

Five dimensions (20 points each):
  1. Size Fit        — employee count vs. a target range you specify
  2. Financial Signal — funding/revenue data Apollo actually has
  3. Industry Match   — overlap between Apollo's industry tags and
                         the mission's keywords
  4. Data Completeness — how many key identifying fields are present
                          (this doubles as a confidence signal — a
                          thin record is a weaker lead regardless of
                          the other three scores)
  5. Contactability    — whether Apollo has real people records at
                          this organization (requires a people search;
                          optional, degrades gracefully to 0 if skipped)
"""

from typing import Optional


class ScoringError(Exception):
    pass


def _score_size_fit(record: dict, target_min: int, target_max: int) -> int:
    count = record.get("estimated_num_employees")
    if not isinstance(count, int):
        return 0
    if target_min <= count <= target_max:
        return 20
    # Partial credit tapering off the further outside the range
    if count < target_min:
        ratio = count / target_min if target_min else 0
    else:
        ratio = target_max / count if count else 0
    return max(0, round(20 * ratio))


def _score_financial_signal(record: dict) -> int:
    score = 0
    funding = record.get("total_funding")
    if isinstance(funding, (int, float)) and funding > 0:
        score += 12
    latest_round = record.get("latest_funding_stage") or record.get("latest_funding_round")
    if latest_round:
        score += 8
    return min(score, 20)


def _score_industry_match(record: dict, mission_keywords: list) -> int:
    if not mission_keywords:
        return 0
    industries = record.get("industries") or record.get("industry_tag_ids") or []
    industry_text = " ".join(str(i) for i in industries).lower()
    keywords_text = record.get("industry", "")
    if keywords_text:
        industry_text += " " + str(keywords_text).lower()

    if not industry_text.strip():
        return 0

    hits = sum(1 for kw in mission_keywords if kw.lower() in industry_text)
    if hits == 0:
        return 0
    return min(20, round(20 * hits / max(len(mission_keywords), 1)) + 5)


def _score_data_completeness(record: dict) -> int:
    key_fields = [
        "name", "website_url", "estimated_num_employees",
        "industry", "founded_year", "linkedin_url",
    ]
    present = sum(1 for f in key_fields if record.get(f))
    return round(20 * present / len(key_fields))


def _score_contactability(record: dict, people_count: Optional[int]) -> int:
    if people_count is None:
        return 0  # not checked — degrades to 0, never guessed
    if people_count <= 0:
        return 0
    if people_count >= 5:
        return 20
    return round(20 * people_count / 5)


def score_lead(
    verified_result: dict,
    mission_keywords: list = None,
    target_employee_range: tuple = (1, 10000),
    people_count: Optional[int] = None,
) -> dict:
    """
    verified_result: one entry from main.py's verify_candidates() output.
    Must have status == "verified" and a non-empty apollo_record — an
    unverified candidate has nothing real to score.

    Returns:
      {
        "name": str,
        "total": int (0-100),
        "breakdown": {dimension_name: score, ...},
      }
    """
    if verified_result.get("status") != "verified" or not verified_result.get("apollo_record"):
        raise ScoringError(
            f"Cannot score '{verified_result.get('name')}' — it was never "
            f"verified against Apollo. Only real, confirmed records get scored."
        )

    record = verified_result["apollo_record"]
    mission_keywords = mission_keywords or []
    target_min, target_max = target_employee_range

    breakdown = {
        "size_fit": _score_size_fit(record, target_min, target_max),
        "financial_signal": _score_financial_signal(record),
        "industry_match": _score_industry_match(record, mission_keywords),
        "data_completeness": _score_data_completeness(record),
        "contactability": _score_contactability(record, people_count),
    }
    total = sum(breakdown.values())

    return {
        "name": verified_result["name"],
        "total": total,
        "breakdown": breakdown,
    }


def score_all(
    verified_results: list,
    mission_keywords: list = None,
    target_employee_range: tuple = (1, 10000),
) -> list:
    """
    Scores every verified result, skipping unverified ones with a
    printed note rather than silently dropping them — you should
    still see they exist, just clearly marked as un-scoreable.
    """
    scored = []
    for r in verified_results:
        if r.get("status") != "verified":
            print(f"  ❔ Skipping score for unverified candidate: {r.get('name')}")
            continue
        try:
            scored.append(score_lead(r, mission_keywords, target_employee_range))
        except ScoringError as e:
            print(f"  ⚠️  {e}")

    scored.sort(key=lambda s: s["total"], reverse=True)
    return scored


if __name__ == "__main__":
    # Manual smoke test with a fake-but-realistic Apollo-shaped record
    # (illustrating the scoring math only — not a real API call).
    fake_verified = {
        "name": "Example Robotics Ltd",
        "status": "verified",
        "apollo_record": {
            "name": "Example Robotics Ltd",
            "website_url": "https://example-robotics.example",
            "estimated_num_employees": 85,
            "industry": "robotics automation",
            "founded_year": 2019,
            "linkedin_url": "https://linkedin.com/company/example-robotics",
            "total_funding": 12000000,
            "latest_funding_stage": "Series A",
        },
    }
    result = score_lead(
        fake_verified,
        mission_keywords=["robotics", "automation"],
        target_employee_range=(20, 200),
        people_count=8,
    )
    print(f"{result['name']}: {result['total']}/100")
    for dim, val in result["breakdown"].items():
        print(f"  {dim}: {val}/20")
