"""
lead_tracker.py — the single source of truth for "is this a real
lead," decoupled from Apollo entirely.

A candidate becomes CONFIRMED by being found on real pages — the
evidence is the citation trail itself. Apollo is demoted to an
optional, on-demand enrichment you can run against any lead (to pull
contact/employee/funding data), never the thing that decides whether
a lead counts or how well it scores.

Confidence tiers:
  - "confirmed"      — found on 2+ independent sources (different
                        domains/tools)
  - "single_source"  — found on exactly 1 source — kept, shown, never
                        silently dropped, just flagged lower-confidence
  - "unconfirmed"     — placeholder state before any source is recorded
                        (should not normally be visible to you)

Every lead carries its full sources list forward — tool, URL, and
which mission step found it — so you can see exactly where each
verification and each cross-reference came from.
"""

from urllib.parse import urlparse


class Lead:
    def __init__(self, name: str):
        self.name = name.strip()
        self.sources = []          # [{tool, source_url, domain}]
        self.apollo_record = None  # set only if enrichment was run and matched
        self.apollo_checked = False
        self.apollo_contacts = []  # people records, if contact enrichment was run

    def add_source(self, tool: str, source_url: str):
        domain = ""
        try:
            domain = urlparse(source_url).netloc.lower().replace("www.", "")
        except Exception:
            pass
        entry = {"tool": tool, "source_url": source_url, "domain": domain}
        # Avoid recording the exact same tool+url twice
        if entry not in self.sources:
            self.sources.append(entry)

    @property
    def distinct_domains(self) -> set:
        return {s["domain"] for s in self.sources if s["domain"]}

    @property
    def confidence(self) -> str:
        if len(self.distinct_domains) >= 2:
            return "confirmed"
        if len(self.sources) >= 1:
            return "single_source"
        return "unconfirmed"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "confidence": self.confidence,
            "sources": list(self.sources),
            "distinct_source_count": len(self.distinct_domains),
            "apollo_checked": self.apollo_checked,
            "apollo_record": self.apollo_record,
            "apollo_contacts": self.apollo_contacts,
        }


class LeadTracker:
    """Accumulates candidates across every plan step, merging duplicates
    by normalized name and building up each lead's source trail."""

    def __init__(self):
        self._leads = {}  # normalized name -> Lead

    def record(self, name: str, tool: str, source_url: str = None):
        if not name or not isinstance(name, str):
            return
        key = name.strip().lower()
        if not key:
            return
        if key not in self._leads:
            self._leads[key] = Lead(name.strip())
        if source_url:
            self._leads[key].add_source(tool, source_url)
        elif tool:
            # A step with no page URL (e.g. a direct Apollo org search)
            # still counts as one source, tagged by tool name only.
            self._leads[key].add_source(tool, source_url or f"apollo://{tool}")

    def all_leads(self) -> list:
        return list(self._leads.values())

    def confirmed(self) -> list:
        return [l for l in self._leads.values() if l.confidence == "confirmed"]

    def single_source(self) -> list:
        return [l for l in self._leads.values() if l.confidence == "single_source"]

    def get(self, name: str) -> "Lead | None":
        return self._leads.get(name.strip().lower())

    def print_summary(self):
        leads = sorted(self.all_leads(), key=lambda l: len(l.distinct_domains), reverse=True)
        print(f"\n{'─'*60}")
        print(f"Lead Tracker — {len(leads)} distinct candidate(s)")
        print(f"{'─'*60}")
        for lead in leads:
            mark = "✅" if lead.confidence == "confirmed" else "🔹"
            print(f"{mark} {lead.name}  [{lead.confidence}, "
                  f"{len(lead.distinct_domains)} source(s)]")
            for s in lead.sources:
                label = s["domain"] or s["source_url"]
                print(f"     ↳ via {s['tool']}: {label}")
        print(f"{'─'*60}")


if __name__ == "__main__":
    # Manual smoke test
    tracker = LeadTracker()
    tracker.record("Example Robotics Ltd", "browser.hunt_wikipedia",
                    "https://en.wikipedia.org/wiki/List_of_private_equity_firms")
    tracker.record("Example Robotics Ltd", "browser.hunt_companies_house",
                    "https://find-and-update.company-information.service.gov.uk/search?q=Example+Robotics")
    tracker.record("Single Mention Corp", "browser.hunt_generic_search",
                    "https://duckduckgo.com/html/?q=Single+Mention+Corp")
    tracker.print_summary()
