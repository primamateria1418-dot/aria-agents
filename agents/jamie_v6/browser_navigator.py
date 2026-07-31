"""
browser_navigator.py — JAMIE v6's discovery engine.

Opens a VISIBLE Chrome window (so you can watch it, and step in if a
site throws a captcha) and reads real pages: Wikipedia lists, company
registries, directories. Company name candidates are extracted only
from text that actually exists on the loaded page — via structured
DOM parsing first, with the local LLM used only to classify/clean
already-extracted strings against the mission. The model is NEVER
asked to produce a company name from a blank prompt — that's the
exact failure mode that made v5 hallucinate.

Verification of any candidate happens downstream in
verify_and_enrich.py against the Apollo API — this file only ever
returns "found on this page, here's the source URL", never "this
company exists".
"""

import os
import re
import time
import json
from datetime import datetime
from urllib.parse import quote_plus

import requests
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv

load_dotenv()

LM_STUDIO_URL = os.environ.get("LM_STUDIO_URL", "http://localhost:1234/v1")
LOCAL_MODEL = os.environ.get("LOCAL_MODEL", "qwen/qwen3.6-35b-a3b")
HEADLESS = os.environ.get("BROWSER_HEADLESS", "false").lower() == "true"

SKIP_WORDS = {
    'the', 'and', 'of', 'in', 'for', 'to', 'a', 'an', 'is', 'are', 'was', 'were',
    'company', 'fund', 'office', 'investment', 'group', 'limited', 'ltd', 'plc',
    'inc', 'corp', 'llc', 'total', 'name', 'type', 'date', 'value', 'amount',
    'number', 'code', 'yes', 'no', 'true', 'false', 'n/a', 'na', 'none', 'null',
}


def ask_local_model(prompt: str, max_tokens: int = 600, temperature: float = 0.2) -> str:
    """
    Call the local LM Studio model. Used only to classify or clean
    text already extracted from a real page — never to generate
    facts from nothing. Low temperature by design: this is a
    filtering task, not a creative one.
    """
    try:
        res = requests.post(
            f"{LM_STUDIO_URL}/chat/completions",
            json={
                "model": LOCAL_MODEL,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You classify and clean text extracted from a real web "
                            "page. You only ever work with the text given to you — "
                            "you never add a company, name, or fact that isn't "
                            "present in the input. If nothing in the input matches "
                            "the request, return an empty JSON array."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            timeout=30,
        )
        if res.status_code == 200:
            content = res.json()["choices"][0]["message"]["content"]
            return re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    except requests.exceptions.RequestException:
        pass
    return ""


def fast_filter_candidates(entries: list) -> list:
    """Cheap regex pre-filter — strips obvious non-company junk before the LLM sees it."""
    results, seen = [], set()
    for entry in entries:
        if not entry or not isinstance(entry, str):
            continue
        e = entry.strip()
        if len(e) < 4 or len(e) > 100:
            continue
        if re.match(r'^[\d\s.,\-+%£$€/]+$', e):
            continue
        if e.startswith("http"):
            continue
        if e.lower() in SKIP_WORDS:
            continue
        if not re.search(r"[a-zA-Z]", e):
            continue
        key = e.lower()
        if key in seen:
            continue
        seen.add(key)
        results.append(e)
    return results


class BrowserNavigator:
    def __init__(self, headless: bool = None):
        self.headless = HEADLESS if headless is None else headless
        self.log = []
        self.playwright = None
        self.browser = None
        self.page = None

    def think(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log.append(f"[{ts}] {msg}")
        print(f"  🌐 JAMIE (browser): {msg}")

    # ── lifecycle ─────────────────────────────────────────────
    def start(self):
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(headless=self.headless)
        context = self.browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            )
        )
        self.page = context.new_page()
        self.think(f"Chrome window opened (headless={self.headless})")

    def stop(self):
        if self.browser:
            self.browser.close()
        if self.playwright:
            self.playwright.stop()
        self.think("Chrome window closed")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

    # ── page loading ──────────────────────────────────────────
    def load_page(self, url: str, wait_ms: int = 2000) -> bool:
        try:
            self.think(f"Navigating to {url}")
            self.page.goto(url, timeout=20000, wait_until="domcontentloaded")
            self.page.wait_for_timeout(wait_ms)
            return True
        except Exception as e:
            self.think(f"Failed to load {url}: {e}")
            return False

    # ── extraction ────────────────────────────────────────────
    def extract_structured_text(self) -> str:
        """
        Pull text preferentially from tables and lists (most reliable
        structure for company names), falling back to visible body
        text only if nothing structured is found.
        """
        structured = ""
        try:
            tables = self.page.query_selector_all("table")[:3]
            for table in tables:
                rows = table.query_selector_all("tr")
                for row in rows:
                    cells = row.query_selector_all("td, th")
                    for cell in cells:
                        text = cell.inner_text().strip()
                        if text:
                            structured += text + " | "
                    structured += "\n"
        except Exception:
            pass

        try:
            lists = self.page.query_selector_all("ul li, ol li")[:200]
            for li in lists:
                text = li.inner_text().strip()
                if text:
                    structured += text + "\n"
        except Exception:
            pass

        if len(structured) < 200:
            try:
                structured = self.page.inner_text("body")[:6000]
            except Exception:
                structured = ""

        return structured

    def extract_candidates(self, mission: str) -> list:
        """
        Extract candidate company names from the currently loaded page.
        Returns a list of dicts: {name, source_url}
        """
        raw_text = self.extract_structured_text()
        if not raw_text.strip():
            self.think("No extractable text on this page")
            return []

        # Fast regex pass first — cheap, catches obvious junk
        raw_lines = re.split(r"[\n|]", raw_text)
        pre_filtered = fast_filter_candidates(raw_lines)

        if not pre_filtered:
            return []

        # LLM only classifies which of these ALREADY-EXTRACTED strings
        # are real company names relevant to the mission — it cannot
        # add anything not in `pre_filtered`.
        chunk = pre_filtered[:150]  # keep prompt bounded
        prompt = f"""Mission: {mission}

Below is a list of text strings extracted from a real web page. Some are
company/organization names, some are navigation junk, categories, or
unrelated text. Return ONLY a JSON array containing the subset of these
EXACT strings (copy them verbatim, don't rewrite) that are real company
or organization names plausibly relevant to the mission.

If none qualify, return an empty JSON array.

Strings:
{json.dumps(chunk)}

JSON array:"""

        result = ask_local_model(prompt, max_tokens=1000, temperature=0.1)
        try:
            match = re.search(r"\[.*\]", result, re.DOTALL)
            names = json.loads(match.group()) if match else []
        except Exception:
            names = []

        # Safety net: only keep names that were actually in the
        # pre-filtered list — the model cannot sneak in a fabricated
        # name even if it tries.
        valid_set = {n.lower() for n in pre_filtered}
        names = [n for n in names if isinstance(n, str) and n.lower() in valid_set]

        source_url = self.page.url
        self.think(f"Extracted {len(names)} candidate(s) from {source_url}")
        return [{"name": n, "source_url": source_url} for n in names]

    # ── mission-level hunting ─────────────────────────────────
    def hunt_wikipedia(self, mission: str, direct_pages: list = None) -> list:
        """Visit Wikipedia list/category pages relevant to the mission."""
        candidates = []
        pages = direct_pages or []
        if not pages:
            query = quote_plus(f"list of {mission}")
            pages = [f"https://en.wikipedia.org/w/index.php?search={query}"]

        for url in pages:
            if self.load_page(url):
                candidates.extend(self.extract_candidates(mission))
            time.sleep(0.5)
        return candidates

    def hunt_companies_house(self, mission: str, search_term: str) -> list:
        """Search UK Companies House register directly."""
        candidates = []
        url = f"https://find-and-update.company-information.service.gov.uk/search?q={quote_plus(search_term)}"
        if self.load_page(url):
            candidates.extend(self.extract_candidates(mission))
        return candidates

    def hunt_generic_search(self, mission: str, query: str) -> list:
        """Fallback: run a query through a real search engine page and read the results."""
        candidates = []
        url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
        if self.load_page(url):
            candidates.extend(self.extract_candidates(mission))
        return candidates


if __name__ == "__main__":
    # Manual smoke test — watch the Chrome window open and hunt.
    test_mission = "private equity firms United Kingdom"
    with BrowserNavigator() as nav:
        results = nav.hunt_wikipedia(
            test_mission,
            direct_pages=["https://en.wikipedia.org/wiki/List_of_private_equity_firms"],
        )
        print(f"\nFound {len(results)} candidate(s):")
        for r in results[:20]:
            print(f"  - {r['name']}  (source: {r['source_url']})")
