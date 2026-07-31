"""
startup_check.py — "Turn JAMIE on."

Run this before every mission. It checks that every tool JAMIE v6
depends on is actually reachable — not just installed — and refuses
to proceed if something is missing or unreachable. No tool failure
is ever silently skipped; a missing dependency stops the run with a
clear, specific fix, because a half-working stack is exactly how the
old version started inventing data to fill gaps.

Checks, in order:
  1. .env loaded + required keys present
  2. Local LLM (LM Studio) reachable and serving the configured model
  3. Playwright + Chromium installed and launchable
  4. Apollo API key valid (live ping)
  5. Tavily API key valid (live ping)
  6. Supabase reachable

Usage:
    python startup_check.py            # just check, print report
    python startup_check.py --run      # check, then launch main.py if all pass
"""

import os
import sys
import subprocess
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

REQUIRED_ENV_VARS = [
    "APOLLO_API_KEY",
    "TAVILY_API_KEY",
    "SUPABASE_URL",
    "SUPABASE_KEY",
    "LM_STUDIO_URL",
    "LOCAL_MODEL",
]


class CheckResult:
    def __init__(self, name, ok, detail):
        self.name = name
        self.ok = ok
        self.detail = detail


def check_env_vars() -> CheckResult:
    missing = [v for v in REQUIRED_ENV_VARS if not os.environ.get(v)]
    if missing:
        return CheckResult(
            "Environment variables", False,
            f"Missing from .env: {', '.join(missing)}"
        )
    return CheckResult("Environment variables", True, "all required keys present")


def check_local_llm() -> CheckResult:
    url = os.environ.get("LM_STUDIO_URL", "http://localhost:1234/v1")
    model = os.environ.get("LOCAL_MODEL", "")
    try:
        res = requests.get(f"{url}/models", timeout=5)
        if res.status_code != 200:
            return CheckResult(
                "Local LLM (LM Studio)", False,
                f"Reached {url} but got HTTP {res.status_code} — is a model loaded?"
            )
        models = [m.get("id", "") for m in res.json().get("data", [])]
        if model and model not in models:
            return CheckResult(
                "Local LLM (LM Studio)", False,
                f"LM Studio is running but '{model}' isn't loaded. "
                f"Loaded models: {models or 'none'}"
            )
        return CheckResult(
            "Local LLM (LM Studio)", True,
            f"reachable at {url}, serving '{model}'"
        )
    except requests.exceptions.RequestException:
        return CheckResult(
            "Local LLM (LM Studio)", False,
            f"Can't reach {url} — open LM Studio and start the local server "
            f"(Developer tab → Start Server), then load '{model}'."
        )


def check_playwright() -> CheckResult:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return CheckResult(
            "Playwright", False,
            "Not installed — run: pip install playwright"
        )
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)  # quick launch test only
            browser.close()
        return CheckResult("Playwright", True, "Chromium launches successfully")
    except Exception as e:
        return CheckResult(
            "Playwright", False,
            f"Chromium not available — run: playwright install chromium ({e})"
        )


def check_apollo() -> CheckResult:
    api_key = os.environ.get("APOLLO_API_KEY", "")
    if not api_key:
        return CheckResult("Apollo API", False, "APOLLO_API_KEY not set")
    try:
        res = requests.post(
            "https://api.apollo.io/api/v1/mixed_companies/search",
            headers={"Content-Type": "application/json", "x-api-key": api_key},
            json={"q_organization_name": "Anthropic", "per_page": 1},
            timeout=10,
        )
        if res.status_code == 200:
            return CheckResult("Apollo API", True, "key valid, live ping succeeded")
        if res.status_code == 401:
            return CheckResult("Apollo API", False, "401 Unauthorized — key is invalid or expired")
        if res.status_code == 403:
            return CheckResult("Apollo API", False, "403 Forbidden — endpoint not on your current plan")
        return CheckResult("Apollo API", False, f"Unexpected HTTP {res.status_code}")
    except requests.exceptions.RequestException as e:
        return CheckResult("Apollo API", False, f"Network error reaching Apollo: {e}")


def check_tavily() -> CheckResult:
    api_key = os.environ.get("TAVILY_API_KEY", "")
    if not api_key:
        return CheckResult("Tavily API", False, "TAVILY_API_KEY not set")
    try:
        res = requests.post(
            "https://api.tavily.com/search",
            json={"api_key": api_key, "query": "test", "max_results": 1},
            timeout=10,
        )
        if res.status_code == 200:
            return CheckResult("Tavily API", True, "key valid, live ping succeeded")
        if res.status_code in (401, 403):
            return CheckResult("Tavily API", False, f"HTTP {res.status_code} — key is invalid or expired")
        return CheckResult("Tavily API", False, f"Unexpected HTTP {res.status_code}")
    except requests.exceptions.RequestException as e:
        return CheckResult("Tavily API", False, f"Network error reaching Tavily: {e}")


def check_supabase() -> CheckResult:
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_KEY", "")
    if not url or not key:
        return CheckResult("Supabase", False, "SUPABASE_URL or SUPABASE_KEY not set")
    try:
        res = requests.get(
            f"{url}/rest/v1/",
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            timeout=10,
        )
        if res.status_code in (200, 404):  # 404 on bare /rest/v1/ is normal, still reachable
            return CheckResult("Supabase", True, f"reachable at {url}")
        if res.status_code in (401, 403):
            return CheckResult("Supabase", False, f"HTTP {res.status_code} — key is invalid")
        return CheckResult("Supabase", False, f"Unexpected HTTP {res.status_code}")
    except requests.exceptions.RequestException as e:
        return CheckResult("Supabase", False, f"Network error reaching Supabase: {e}")


def push_status_to_supabase(results: list):
    """
    Push each check result to a `system_status` table in Supabase so
    the Render dashboard can display live tool health instead of you
    having to watch this terminal.

    Expected table schema (create once in Supabase SQL editor):

        create table system_status (
            tool_name text primary key,
            ok boolean not null,
            detail text,
            checked_at timestamptz not null default now()
        );

    Uses upsert on tool_name so the dashboard always shows the latest
    check, not a growing history table.
    """
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_KEY", "")
    if not url or not key:
        print("⚠️  Skipping Supabase status push — SUPABASE_URL/KEY not set.")
        return

    endpoint = f"{url}/rest/v1/system_status"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",  # upsert on primary key
    }
    rows = [
        {
            "tool_name": r.name,
            "ok": r.ok,
            "detail": r.detail,
            "checked_at": datetime.utcnow().isoformat(),
        }
        for r in results
    ]
    try:
        res = requests.post(endpoint, headers=headers, json=rows, timeout=10)
        if res.status_code in (200, 201, 204):
            print("📡 Status pushed to Supabase — visible on the dashboard now.")
        else:
            print(f"⚠️  Supabase status push failed: HTTP {res.status_code} — {res.text[:200]}")
    except requests.exceptions.RequestException as e:
        print(f"⚠️  Supabase status push failed: {e}")


def run_all_checks() -> list:
    checks = [
        check_env_vars,
        check_local_llm,
        check_playwright,
        check_apollo,
        check_tavily,
        check_supabase,
    ]
    results = []
    for check_fn in checks:
        print(f"Checking {check_fn.__name__.replace('check_', '').replace('_', ' ')}...", end=" ")
        result = check_fn()
        print("✅" if result.ok else "❌")
        results.append(result)
    return results


def print_report(results: list):
    print("\n" + "─" * 60)
    print("JAMIE v6 — Preflight Report")
    print("─" * 60)
    for r in results:
        status = "✅ OK  " if r.ok else "❌ FAIL"
        print(f"{status}  {r.name}: {r.detail}")
    print("─" * 60)


def main():
    results = run_all_checks()
    print_report(results)
    push_status_to_supabase(results)

    all_ok = all(r.ok for r in results)

    if not all_ok:
        print("\n🛑 Not all systems are ready. Fix the ❌ items above before running a mission.")
        sys.exit(1)

    print("\n✅ All systems ready.")

    if "--run" in sys.argv:
        main_py = os.path.join(os.path.dirname(__file__), "main.py")
        if os.path.exists(main_py):
            print("Launching main.py...\n")
            subprocess.run([sys.executable, main_py])
        else:
            print("main.py isn't built yet — nothing to launch. "
                  "Preflight passed, so you're clear to build/run the next piece.")


if __name__ == "__main__":
    main()
