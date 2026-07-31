"""
main.py — JAMIE v6 mission runner.

Flow:
  1. Take a plain-English mission from you.
  2. mission_planner.py turns it into a concrete, inspectable plan
     (which real tool, what query, why) — you see the plan printed
     before anything runs.
  3. Execute each step LITERALLY against real tools. Every candidate
     name found gets recorded in a LeadTracker along with exactly
     which page it came from.
  4. A lead is CONFIRMED when it's been found on 2+ independent
     sources — that cross-referencing is the verification, not
     Apollo. Single-source hits are kept and shown, just flagged
     lower-confidence.
  5. Apollo is a SEPARATE, optional enrichment step you run against
     any lead afterward to pull contact/employee/funding data — it
     never gates whether a lead counts, and a lead never scores lower
     for not having been enriched.
  6. Print the final list with each lead's confidence tier and full
     source trail (where it was found, where it was cross-referenced).

Scoring (scorer.py) and the Supabase push (supabase_sync.py) build on
top of the LeadTracker output this file produces.
"""

import sys

from mission_planner import plan_mission, print_plan
from browser_navigator import BrowserNavigator
from apollo_client import ApolloClient, ApolloError
from lead_tracker import LeadTracker
from supabase_sync import push_mission_results, SupabaseSyncError


def execute_step(step: dict, mission: str, nav: BrowserNavigator, tracker: LeadTracker):
    """
    Execute exactly one plan step against the real tool it names, and
    record every candidate found straight into the tracker with its
    source URL. No candidate is held in a side list waiting on Apollo
    — it's part of the evidence trail the moment it's found.
    """
    tool = step["tool"]
    params = step["params"]

    try:
        if tool == "browser.hunt_wikipedia":
            found = nav.hunt_wikipedia(mission)
            for f in found:
                tracker.record(f["name"], tool, f["source_url"])

        elif tool == "browser.hunt_companies_house":
            search_term = params.get("search_term", mission)
            found = nav.hunt_companies_house(mission, search_term)
            for f in found:
                tracker.record(f["name"], tool, f["source_url"])

        elif tool == "browser.hunt_generic_search":
            query = params.get("query", mission)
            found = nav.hunt_generic_search(mission, query)
            for f in found:
                tracker.record(f["name"], tool, f["source_url"])

        elif tool == "apollo.search_organizations":
            # Even when the plan calls Apollo's own database directly,
            # it's recorded as one source among others — Apollo isn't
            # given special "automatically confirmed" status here.
            apollo = ApolloClient()
            data = apollo.search_organizations(
                q_organization_name=params.get("q_organization_name"),
                organization_locations=params.get("organization_locations"),
                organization_num_employees_ranges=params.get("organization_num_employees_ranges"),
                organization_industries=params.get("organization_industries"),
            )
            orgs = data.get("organizations", []) or []
            for org in orgs:
                name = org.get("name")
                tracker.record(name, tool, org.get("website_url") or f"apollo://{name}")
                # Stash the Apollo record directly since we already have it —
                # no need to re-enrich later.
                lead = tracker.get(name)
                if lead:
                    lead.apollo_record = org
                    lead.apollo_checked = True

        else:
            print(f"  ⚠️  Step referenced unknown tool '{tool}' — skipping (this is a bug, report it)")

    except ApolloError as e:
        print(f"  ⚠️  Apollo step failed: {e}")
    except Exception as e:
        print(f"  ⚠️  Step failed unexpectedly: {e}")


def enrich_with_apollo(tracker: LeadTracker, leads_to_enrich: list = None, pull_contacts: bool = False):
    """
    OPTIONAL, on-demand: pull Apollo company (and optionally contact)
    data for specific confirmed leads. Call this only when you
    actually want outreach-ready contact info for a lead — it's never
    required to reach the final list.

    leads_to_enrich: list of Lead objects, or None to enrich every
    confirmed lead that hasn't been checked yet.
    """
    apollo = ApolloClient()
    targets = leads_to_enrich if leads_to_enrich is not None else \
        [l for l in tracker.confirmed() if not l.apollo_checked]

    for lead in targets:
        try:
            org = apollo.enrich_organization(name=lead.name)
        except ApolloError as e:
            print(f"  ⚠️  Apollo enrichment failed for '{lead.name}': {e}")
            org = None

        lead.apollo_checked = True
        lead.apollo_record = org  # None is a valid, honest outcome — Apollo has no record

        if org and pull_contacts:
            try:
                people = apollo.search_people(organization_id=org.get("id"), per_page=5)
                lead.apollo_contacts = people.get("people", []) or []
            except ApolloError as e:
                print(f"  ⚠️  Contact pull failed for '{lead.name}': {e}")


def run_mission(mission: str, enrich_contacts: bool = False) -> LeadTracker:
    steps = plan_mission(mission)
    print_plan(steps)

    tracker = LeadTracker()

    if not steps:
        print("Nothing to execute — planning failed and produced no valid steps.")
        return tracker

    with BrowserNavigator() as nav:
        for i, step in enumerate(steps, 1):
            print(f"\n▶ Step {i}/{len(steps)}: {step['tool']}")
            execute_step(step, mission, nav, tracker)
            print(f"  → {len(tracker.all_leads())} distinct lead(s) so far")

    tracker.print_summary()

    confirmed = tracker.confirmed()
    single = tracker.single_source()
    print(f"\n{len(confirmed)} confirmed (2+ sources) / "
          f"{len(single)} single-source / {len(tracker.all_leads())} total")

    if enrich_contacts and confirmed:
        print(f"\n📇 Pulling Apollo contact data for {len(confirmed)} confirmed lead(s)...")
        enrich_with_apollo(tracker, leads_to_enrich=confirmed, pull_contacts=True)
        for lead in confirmed:
            if lead.apollo_record:
                n = len(lead.apollo_contacts)
                print(f"  ✅ {lead.name}: Apollo record found, {n} contact(s) pulled")
            else:
                print(f"  ❔ {lead.name}: no Apollo record — contact info unavailable")

    print(f"\n📤 Pushing scored results to Supabase...")
    try:
        push_mission_results(mission, tracker)
    except SupabaseSyncError as e:
        print(f"  ⚠️  Skipped Supabase push: {e}")

    return tracker


if __name__ == "__main__":
    args = sys.argv[1:]
    enrich_flag = "--enrich" in args
    args = [a for a in args if a != "--enrich"]

    mission_text = " ".join(args) if args else input("Mission: ").strip()
    if not mission_text:
        print("No mission given — exiting.")
        sys.exit(1)

    run_mission(mission_text, enrich_contacts=enrich_flag)
