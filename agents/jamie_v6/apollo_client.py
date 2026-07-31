"""
apollo_client.py — Thin wrapper around the Apollo.io API for JAMIE v6.

Design principle: this module NEVER invents data. Every function either
returns real records from Apollo, an empty result, or raises/logs an
explicit error. There is no LLM in this file and no fallback that
fabricates a company or contact.

Endpoints used:
  POST /api/v1/mixed_companies/search   — organization search
  GET  /api/v1/organizations/enrich     — organization enrichment by domain
  POST /api/v1/mixed_people/search      — people search
  POST /api/v1/people/match             — single person enrichment

Docs: https://docs.apollo.io/reference
"""

import os
import time
import requests
from typing import Optional

APOLLO_BASE_URL = "https://api.apollo.io/api/v1"


class ApolloError(Exception):
    """Raised on a genuine API failure (auth, rate limit, bad request)."""
    pass


class ApolloClient:
    def __init__(self, api_key: Optional[str] = None, timeout: int = 15):
        self.api_key = api_key or os.environ.get("APOLLO_API_KEY", "")
        if not self.api_key:
            raise ApolloError(
                "APOLLO_API_KEY is not set. Add it to your .env file."
            )
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
            "x-api-key": self.api_key,
        })
        # Running tally so you can see burn rate mid-mission
        self.calls_made = 0

    # ── internal request helper ──────────────────────────────
    def _request(self, method: str, path: str, retries: int = 2, **kwargs):
        url = f"{APOLLO_BASE_URL}{path}"
        last_exc = None
        for attempt in range(retries + 1):
            try:
                res = self.session.request(method, url, timeout=self.timeout, **kwargs)
                self.calls_made += 1

                if res.status_code == 200:
                    return res.json()

                if res.status_code == 401:
                    raise ApolloError(
                        "Apollo API returned 401 Unauthorized — check APOLLO_API_KEY."
                    )

                if res.status_code == 403:
                    raise ApolloError(
                        "Apollo API returned 403 Forbidden — this endpoint may not be "
                        "included in your current plan/seat."
                    )

                if res.status_code == 429:
                    wait = 2 ** attempt
                    print(f"  ⏳ Apollo rate limited — waiting {wait}s (attempt {attempt+1})")
                    time.sleep(wait)
                    continue

                # Any other non-200: surface it, don't guess what it means
                raise ApolloError(
                    f"Apollo API {res.status_code}: {res.text[:300]}"
                )

            except requests.exceptions.RequestException as e:
                last_exc = e
                wait = 2 ** attempt
                print(f"  ⏳ Network error calling Apollo — retrying in {wait}s: {e}")
                time.sleep(wait)

        raise ApolloError(f"Apollo request failed after retries: {last_exc}")

    # ── organization search ───────────────────────────────────
    def search_organizations(
        self,
        q_organization_name: Optional[str] = None,
        organization_locations: Optional[list] = None,
        organization_num_employees_ranges: Optional[list] = None,
        organization_industries: Optional[list] = None,
        page: int = 1,
        per_page: int = 25,
    ) -> dict:
        """
        Search Apollo's organization database directly. Returns Apollo's
        raw JSON response — real records only, or an empty list under
        'organizations' if nothing matches.
        """
        body = {"page": page, "per_page": per_page}
        if q_organization_name:
            body["q_organization_name"] = q_organization_name
        if organization_locations:
            body["organization_locations"] = organization_locations
        if organization_num_employees_ranges:
            body["organization_num_employees_ranges"] = organization_num_employees_ranges
        if organization_industries:
            body["organization_industry_tag_ids"] = organization_industries

        return self._request("POST", "/mixed_companies/search", json=body)

    # ── organization enrichment (verify a candidate name/domain) ─
    def enrich_organization(self, domain: Optional[str] = None,
                             name: Optional[str] = None) -> Optional[dict]:
        """
        Verify a single company found by the browser navigator against
        Apollo's real records. Pass a domain if you have one (most
        reliable); falls back to name search otherwise.

        Returns the organization dict if found, or None if Apollo has
        no matching record — callers must treat None as "unverified",
        never as "invent one anyway".
        """
        if domain:
            params = {"domain": domain}
            data = self._request("GET", "/organizations/enrich", params=params)
            org = data.get("organization")
            return org if org else None

        if name:
            data = self.search_organizations(q_organization_name=name, per_page=5)
            orgs = data.get("organizations", []) or []
            # Only return an exact-ish match — do not guess between
            # multiple candidates.
            for org in orgs:
                if org.get("name", "").strip().lower() == name.strip().lower():
                    return org
            return None

        raise ValueError("enrich_organization requires a domain or name")

    # ── people search (contacts at a verified organization) ──
    def search_people(
        self,
        organization_id: Optional[str] = None,
        organization_domain: Optional[str] = None,
        titles: Optional[list] = None,
        page: int = 1,
        per_page: int = 10,
    ) -> dict:
        """
        Find real contacts at an already-verified organization.
        Do not call this for a company Apollo hasn't confirmed exists.
        """
        body = {"page": page, "per_page": per_page}
        if organization_id:
            body["organization_ids"] = [organization_id]
        if organization_domain:
            body["q_organization_domains"] = organization_domain
        if titles:
            body["person_titles"] = titles

        return self._request("POST", "/mixed_people/search", json=body)

    # ── single person enrichment ──────────────────────────────
    def enrich_person(
        self,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        email: Optional[str] = None,
        organization_name: Optional[str] = None,
        domain: Optional[str] = None,
    ) -> Optional[dict]:
        """
        Enrich (verify) a single contact. Returns None if Apollo has no
        record — never fabricate contact details for a miss.
        """
        body = {}
        if first_name: body["first_name"] = first_name
        if last_name: body["last_name"] = last_name
        if email: body["email"] = email
        if organization_name: body["organization_name"] = organization_name
        if domain: body["domain"] = domain

        if not body:
            raise ValueError("enrich_person requires at least one identifying field")

        data = self._request("POST", "/people/match", json=body)
        return data.get("person") if data.get("person") else None


if __name__ == "__main__":
    # Quick manual smoke test — run `python apollo_client.py` after
    # setting APOLLO_API_KEY in your environment or .env file.
    from dotenv import load_dotenv
    load_dotenv()

    client = ApolloClient()
    print("Testing organization search for 'Anthropic'...")
    result = client.search_organizations(q_organization_name="Anthropic", per_page=3)
    orgs = result.get("organizations", [])
    print(f"Found {len(orgs)} organization(s), {client.calls_made} API call(s) used")
    for org in orgs:
        print(f"  - {org.get('name')} | {org.get('website_url')} | "
              f"{org.get('estimated_num_employees')} employees")
