"""
mission_planner.py — turns a plain-English mission into a concrete,
inspectable research plan.

The planning step is where the local model is ALLOWED to be creative
— "companies that recently raised $100M" should make it reason about
Companies House filings, SEC Form D, funding-news roundups, etc. That
reasoning is a plan, not a fact, so creativity here is safe.

What is NOT allowed: the model cannot execute anything itself, and it
cannot invent a "source" that doesn't exist. Every step in the plan
must map onto one of a fixed whitelist of real functions in
browser_navigator.py / apollo_client.py. Anything the model proposes
that doesn't match the whitelist is dropped before execution, not
"interpreted charitably" — a plan step that can't be mapped to a real
tool call is worthless and potentially a hallucination risk.

The plan is printed in full before main.py executes a single step, so
you can see the strategy and cancel before any Apollo credits are
spent or Chrome starts clicking around.
"""

import os
import re
import json
import requests
from dotenv import load_dotenv

load_dotenv()

LM_STUDIO_URL = os.environ.get("LM_STUDIO_URL", "http://localhost:1234/v1")
LOCAL_MODEL = os.environ.get("LOCAL_MODEL", "qwen/qwen3.6-35b-a3b")

# ── Fixed whitelist of real, callable research steps ─────────
# Every plan step MUST map to exactly one of these. This is the hard
# boundary between "the model plans a strategy" and "the model
# invents a source" — nothing outside this list is executable.
AVAILABLE_TOOLS = {
    "browser.hunt_wikipedia": {
        "module": "browser_navigator",
        "description": "Visit Wikipedia list/category pages relevant to a topic.",
        "params": ["query"],
    },
    "browser.hunt_companies_house": {
        "module": "browser_navigator",
        "description": "Search the UK Companies House register directly.",
        "params": ["search_term"],
    },
    "browser.hunt_generic_search": {
        "module": "browser_navigator",
        "description": (
            "Run a search-engine query and read the results page. Use for "
            "sources with no dedicated method below, e.g. SEC EDGAR full-text "
            "search, Crunchbase, TechCrunch funding roundups, trade "
            "association member lists, filetype:xlsx/csv/pdf company lists."
        ),
        "params": ["query"],
    },
    "apollo.search_organizations": {
        "module": "apollo_client",
        "description": (
            "Search Apollo's own organization database directly by name, "
            "location, employee count range, or industry. Use this as a "
            "step in its own right when the mission maps cleanly onto "
            "Apollo's filters (e.g. industry + location + size), not just "
            "as a verification step for browser-found candidates."
        ),
        "params": ["q_organization_name", "organization_locations",
                    "organization_num_employees_ranges", "organization_industries"],
    },
}

TOOL_LIST_TEXT = "\n".join(
    f"- {name}: {info['description']} (params: {', '.join(info['params'])})"
    for name, info in AVAILABLE_TOOLS.items()
)


def ask_local_model(prompt: str, max_tokens: int = 900, temperature: float = 0.6) -> str:
    """Planning is the one place a higher temperature is fine — it's strategy, not fact."""
    try:
        res = requests.post(
            f"{LM_STUDIO_URL}/chat/completions",
            json={
                "model": LOCAL_MODEL,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are a research strategist. You plan WHERE to look "
                            "for information — you never provide the information "
                            "itself. You only ever propose steps from the exact "
                            "tool list you are given; you never invent a tool, "
                            "source, or company name."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            timeout=45,
        )
        if res.status_code == 200:
            content = res.json()["choices"][0]["message"]["content"]
            return re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    except requests.exceptions.RequestException as e:
        print(f"  ⚠️  Local model unreachable while planning: {e}")
    return ""


def build_planning_prompt(mission: str) -> str:
    return f"""Mission: "{mission}"

Available tools (this is the COMPLETE list — you cannot use anything
not on it):
{TOOL_LIST_TEXT}

Plan a research strategy for this mission using ONLY the tools above.
Produce 4-8 steps. Each step must have:
  - "tool": one of the exact tool names above, verbatim
  - "params": a dict matching that tool's param names
  - "rationale": one sentence on why this step helps the mission

Order steps from most likely to yield real, on-target results to
least likely. Do not repeat the same tool+params combination twice.

Return ONLY a JSON array of step objects. No explanation, no markdown
fences, just the array.

JSON array:"""


def validate_step(step: dict) -> tuple:
    """
    Returns (is_valid, reason). A step is only valid if its tool name
    exists verbatim in AVAILABLE_TOOLS and its params are a dict.
    This is the hard gate — nothing skips it.
    """
    if not isinstance(step, dict):
        return False, "step is not an object"
    tool = step.get("tool")
    if tool not in AVAILABLE_TOOLS:
        return False, f"'{tool}' is not a real tool — dropped"
    if not isinstance(step.get("params"), dict):
        return False, "params must be an object"
    return True, "ok"


def plan_mission(mission: str) -> list:
    """
    Returns a validated list of step dicts:
      {tool, params, rationale}
    Any step the model proposed that isn't a real, whitelisted tool
    call is dropped and reported — never silently kept, never
    "fixed up" by guessing what was meant.
    """
    print(f"🧠 Planning strategy for: \"{mission}\"")
    prompt = build_planning_prompt(mission)
    raw = ask_local_model(prompt)

    try:
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        proposed = json.loads(match.group()) if match else []
    except Exception:
        proposed = []

    if not proposed:
        print("⚠️  Planner returned nothing usable — falling back to a generic sweep.")
        proposed = [
            {"tool": "apollo.search_organizations",
             "params": {"q_organization_name": mission},
             "rationale": "Direct Apollo search as a baseline when planning fails."},
            {"tool": "browser.hunt_generic_search",
             "params": {"query": mission},
             "rationale": "Generic web sweep as a baseline when planning fails."},
        ]

    validated = []
    for i, step in enumerate(proposed, 1):
        ok, reason = validate_step(step)
        if ok:
            validated.append(step)
        else:
            print(f"  ❌ Step {i} dropped: {reason} — {step}")

    return validated


def print_plan(steps: list):
    print("\n" + "─" * 60)
    print("JAMIE v6 — Research Plan")
    print("─" * 60)
    if not steps:
        print("(no valid steps — nothing will run)")
    for i, step in enumerate(steps, 1):
        print(f"{i}. [{step['tool']}]")
        print(f"   params: {step['params']}")
        print(f"   why: {step.get('rationale', '(no rationale given)')}")
    print("─" * 60)


if __name__ == "__main__":
    test_mission = input("Mission (e.g. 'companies that recently raised $100M'): ").strip()
    if not test_mission:
        test_mission = "companies that recently raised $100M"
    steps = plan_mission(test_mission)
    print_plan(steps)
