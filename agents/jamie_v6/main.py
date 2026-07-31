"""
main.py — JAMIE v6 mission runner.

Flow:
  1. Take a plain-English mission from you.
  2. mission_planner.py turns it into a concrete, inspectable plan
     (which real tool, what query, why) — you see the plan printed
     before anything runs.
  3. Execute each step LITERALLY. A step that says
     "browser.hunt_wikipedia" calls that exact function with those
     exact params — nothing is reinterpreted or improvised at
     execution time.
  4. Every candidate name that comes out of a browser step is
     unverified by definition — it's just "this string appeared on
     this page". Before it goes on the final list, it's checked
     against Apollo. Found → real data attached. Not found → kept
     but clearly marked unverified, never dropped silently and never
     invented into a fake Apollo record.
  5. Print the final list with verification status for each entry.

This does NOT yet do lead scoring (that's scorer.py, next) or push
results to Supabase (that's supabase_sync.py, after scorer.py) — this
file is the orchestration core those two will plug into.
"""

import sys
import time

from mission_planner import plan_mission, print_plan, AVAILABLE_TOOLS
from browser_navigator import BrowserNavigator
from apollo_client import ApolloClient, ApolloError


def execute_step(step: dict, mission: str, nav: BrowserNavigator, apollo: ApolloClient) -> list:
    """
    Execute exactly one plan step. Returns a list of candidate dicts:
      {name, source_url, source_step}
    for browser steps, or {name, apollo_record, source_step} for a
    direct Apollo step (already verified, since it came from Apollo
    itself).
    """
    tool = step["tool"]
    params = step["params"]
    results = []

    try:
        if tool == "browser.hunt_wikipedia":
            query = params.get("query", mission)
            found = nav.hunt_wikipedia(mission, direct_pages=None) if not query else \
                    nav.hunt_wikipedia(mission)
            for f in found:
                results.append({"name": f["name"], "source_url": f["source_url"], "source_step": tool})

        elif tool == "browser.hunt_companies_house":
            search_term = params.get("search_term", mission)
            found = nav.hunt_companies_house(mission, search_term)
            for f in found:
                results.append({"name": f["name"], "source_url": f["source_url"], "source_step": tool})

        elif tool == "browser.hunt_generic_search":
            query = params.get("query", mission)
            found = nav.hunt_generic_search(mission, query)
            for f in found:
                results.append({"name": f["name"], "source_url": f["source_url"], "source_step": tool})

        elif tool == "apollo.search_organizations":
            data = apollo.search_organizations(
                q_organization_name=params.get("q_organization_name"),
                organization_locations=params.get("organization_locations"),
                organization_num_employees_ranges=params.get("organization_num_employees_ranges"),
                organization_industries=params.get("organization_industries"),
            )
            orgs = data.get("organizations", []) or []
            for org in orgs:
                results.append({
                    "name": org.get("name"),
                    "apollo_record": org,
                    "source_step": tool,
                })

        else:
            # Should never happen — mission_planner already validated
            # against AVAILABLE_TOOLS — but fail loudly if it does.
            print(f"  ⚠️  Step referenced unknown tool '{tool}' — skipping (this is a bug, report it)")

    except ApolloError as e:
        print(f"  ⚠️  Apollo step failed: {e}")
    except Exception as e:
        print(f"  ⚠️  Step failed unexpectedly: {e}")

    return results


def verify_candidates(candidates: list, apollo: ApolloClient) -> list:
    """
    For every candidate not already sourced directly from Apollo,
    check it against Apollo's records. A miss is marked 'unverified',
    never dropped and never invented into a fake match.
    """
    verified = []
    seen_names = set()

    for c in candidates:
        name = (c.get("name") or "").strip()
        if not name or name.lower() in seen_names:
            continue
        seen_names.add(name.lower())

        if "apollo_record" in c:
            # Came straight from Apollo — already a real record.
            verified.append({
                "name": name,
                "status": "verified",
                "apollo_record": c["apollo_record"],
                "source": c.get("source_step"),
            })
            continue

        # Browser-found candidate — check it against Apollo.
        try:
            org = apollo.enrich_organization(name=name)
        except ApolloError as e:
            print(f"  ⚠️  Verification failed for '{name}': {e}")
            org = None

        if org:
            verified.append({
                "name": name,
                "status": "verified",
                "apollo_record": org,
                "source": c.get("source_step"),
                "source_url": c.get("source_url"),
            })
        else:
            verified.append({
                "name": name,
                "status": "unverified",
                "apollo_record": None,
                "source": c.get("source_step"),
                "source_url": c.get("source_url"),
            })

        time.sleep(0.2)  # be polite to Apollo's rate limits

    return verified


def run_mission(mission: str):
    steps = plan_mission(mission)
    print_plan(steps)

    if not steps:
        print("Nothing to execute — planning failed and produced no valid steps.")
        return []

    apollo = ApolloClient()
    all_candidates = []

    with BrowserNavigator() as nav:
        for i, step in enumerate(steps, 1):
            print(f"\n▶ Step {i}/{len(steps)}: {step['tool']}")
            found = execute_step(step, mission, nav, apollo)
            print(f"  → {len(found)} raw candidate(s)")
            all_candidates.extend(found)

    print(f"\n🔎 Verifying {len(all_candidates)} candidate(s) against Apollo...")
    final_list = verify_candidates(all_candidates, apollo)

    verified_count = sum(1 for r in final_list if r["status"] == "verified")
    print(f"\n{'─'*60}")
    print(f"JAMIE v6 — Results for: \"{mission}\"")
    print(f"{'─'*60}")
    print(f"{verified_count} verified / {len(final_list)} total candidates\n")

    for r in final_list:
        mark = "✅" if r["status"] == "verified" else "❔"
        extra = ""
        if r["status"] == "verified" and r.get("apollo_record"):
            website = r["apollo_record"].get("website_url", "")
            employees = r["apollo_record"].get("estimated_num_employees", "")
            extra = f"  ({website}, {employees} employees)" if website else ""
        print(f"  {mark} {r['name']}{extra}")

    return final_list


if __name__ == "__main__":
    if len(sys.argv) > 1:
        mission_text = " ".join(sys.argv[1:])
    else:
        mission_text = input("Mission: ").strip()

    if not mission_text:
        print("No mission given — exiting.")
        sys.exit(1)

    run_mission(mission_text)
