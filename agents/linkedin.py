"""
agents/linkedin.py
ARIA™ — LinkedIn Agent (Agent 09)
Runs at 12:00 daily. Posts approved content via Zapier webhook.
Multi-platform, multi-account routing via zapier_routes table in Supabase.
Falls back to evergreen reserve if no approved content exists.
OUP International Ltd, 2026
"""

import os
import json
import logging
import httpx
import time
from datetime import date, datetime
from core.base_agent import BaseAgent
from core.llm import call_llm
from core.memory import supabase_select, supabase_update, supabase_insert

logger = logging.getLogger("aria.linkedin")

WEEKLY_VIBES = {
    0: {"vibe": "authority",     "desc": "Market insight, bold take, data-led. Start the week with credibility."},
    1: {"vibe": "education",     "desc": "How-to, tip, framework. Build audience value."},
    2: {"vibe": "social_proof",  "desc": "Anonymised client result or case study angle."},
    3: {"vibe": "behind_scenes", "desc": "How ARIA works, agent spotlight. Build trust and mystique."},
    4: {"vibe": "provocative",   "desc": "Controversial opinion, challenges conventional wisdom. Drive engagement."},
    5: {"vibe": "human",         "desc": "Founder story, relatable observation. The person behind the brand."},
    6: None,
}

POLL_INTERVAL_SECONDS = 120
POLL_MAX_ATTEMPTS     = 5


class LinkedInAgent(BaseAgent):
    def __init__(self, client_id: str = "aria_internal"):
        super().__init__(name="linkedin", client_id=client_id)

    def run(self):
        today       = date.today()
        weekday     = today.weekday()
        vibe_config = WEEKLY_VIBES.get(weekday)

        if vibe_config is None:
            self._log_action("Sunday — no post scheduled")
            self._log_outcome("Rest day observed")
            return

        vibe      = vibe_config["vibe"]
        vibe_desc = vibe_config["desc"]
        day_name  = today.strftime("%A")
        self._log_action(f"Running — {day_name} [{vibe}]")

        routes = self._load_routes()
        if not routes:
            self.raise_flag("aria_ceo",
                "LinkedIn agent has no active Zapier routes. Add routes to zapier_routes table in Supabase.",
                priority="urgent")

        content = self._get_approved_content(vibe)

        if not content:
            self._log_action("No approved content — requesting on-demand write from Writer")
            self.raise_flag("writer",
                f"LinkedIn needs a {vibe} post for today ({day_name}). "
                f"Vibe: {vibe_desc} Write now, save to content_queue with "
                f"vibe='{vibe}', platform='linkedin', approved=true.",
                priority="urgent",
                context={"vibe": vibe, "day": day_name, "requested_by": "linkedin"})
            content = self._poll_for_content(vibe)

        if not content:
            self._log_action("Writer did not deliver — pulling from evergreen reserve")
            evergreen = self._get_evergreen(vibe)
            if evergreen:
                self._post_to_all_routes(
                    text=evergreen.get("content", ""),
                    vibe=vibe, day=day_name,
                    content_id=evergreen.get("id", ""),
                    routes=routes, target_accounts=[])
                supabase_update("evergreen_reserve", row_id=evergreen["id"], data={
                    "last_used":  datetime.utcnow().isoformat(),
                    "used_count": (evergreen.get("used_count", 0) or 0) + 1,
                })
                self.raise_flag("aria_ceo",
                    f"LinkedIn used evergreen fallback today ({day_name}, {vibe}).",
                    priority="normal")
            else:
                self._log_outcome(f"SKIPPED — no content available [{vibe}] [{day_name}]")
                self.raise_flag("aria_ceo",
                    f"LinkedIn post SKIPPED today ({day_name}). No content and evergreen reserve empty.",
                    priority="urgent")
                self._set_metric("post_skipped", True)
            return

        post_text       = content.get("draft", "")
        target_accounts = content.get("target_accounts") or []

        for prefix in ["[PERSONALISED OUTREACH", "ANGLE:", "WEEKLY REPORT"]:
            if post_text.startswith(prefix):
                self._log_action(f"Content has internal marker — skipping")
                return

        results = self._post_to_all_routes(
            text=post_text, vibe=vibe, day=day_name,
            content_id=content.get("id", ""),
            routes=routes, target_accounts=target_accounts)

        supabase_update("content_queue", row_id=content["id"], data={
            "published":       True,
            "published_at":    datetime.utcnow().isoformat(),
            "zapier_response": results,
            "status":          "published",
        })

        self._log_outcome(f"Posted [{vibe}] [{day_name}] to {len(results)} account(s)")
        self._set_metric("post_published", True)
        self._set_metric("accounts_posted", len(results))
        self._set_metric("vibe", vibe)

    # ── Routing ────────────────────────────────────────────────────

    def _load_routes(self) -> list:
        routes = supabase_select("zapier_routes",
            filters={"client_id": self.client_id, "active": True}, limit=20)
        self._log_action(f"Loaded {len(routes)} active route(s): "
                         f"{[r.get('account_name','?') for r in routes]}")
        return routes

    def _filter_routes(self, routes: list, target_accounts: list) -> list:
        if not target_accounts:
            return routes
        return [r for r in routes if r.get("account_type") in target_accounts]

    def _post_to_all_routes(self, text, vibe, day, content_id, routes, target_accounts) -> dict:
        active_routes = self._filter_routes(routes, target_accounts)
        if not active_routes:
            active_routes = routes

        results = {}
        for route in active_routes:
            account_name = route.get("account_name", "unknown")
            webhook_url  = route.get("webhook_url", "")

            if "REPLACE_ME" in webhook_url or not webhook_url:
                logger.warning(f"Route '{account_name}' has placeholder webhook URL — skipping")
                results[account_name] = {"skipped": True, "reason": "webhook not configured"}
                continue

            payload = {
                "content":      text,
                "platform":     route.get("platform", "linkedin"),
                "account_type": route.get("account_type", "company_page"),
                "account_name": account_name,
                "vibe":         vibe,
                "day":          day,
                "client_id":    self.client_id,
                "content_id":   content_id,
            }
            response = self._fire_zapier(webhook_url, payload, account_name)
            results[account_name] = response
            self._log_action(f"Posted to {account_name} — "
                             f"status: {response.get('status', response.get('error', '?'))}")

        return results

    def _fire_zapier(self, webhook_url, payload, account_name) -> dict:
        try:
            with httpx.Client(timeout=15) as client:
                res = client.post(webhook_url, json=payload)
                res.raise_for_status()
                return {"status": res.status_code, "account": account_name, "body": res.text[:200]}
        except Exception as e:
            logger.error(f"Zapier webhook failed for {account_name}: {e}")
            self.raise_flag("aria_ceo",
                f"Zapier webhook failed for '{account_name}'. Error: {str(e)[:150]}",
                priority="urgent")
            return {"error": str(e), "account": account_name}

    # ── Content retrieval ──────────────────────────────────────────

    def _get_approved_content(self, vibe: str) -> dict | None:
        rows = supabase_select("content_queue",
            filters={"client_id": self.client_id, "platform": "linkedin",
                     "approved": True, "published": False, "vibe": vibe}, limit=1)
        if not rows:
            rows = supabase_select("content_queue",
                filters={"client_id": self.client_id, "platform": "linkedin",
                         "approved": True, "published": False}, limit=1)
        return rows[0] if rows else None

    def _poll_for_content(self, vibe: str) -> dict | None:
        for attempt in range(1, POLL_MAX_ATTEMPTS + 1):
            self._log_action(f"Polling attempt {attempt}/{POLL_MAX_ATTEMPTS}")
            time.sleep(POLL_INTERVAL_SECONDS)
            rows = supabase_select("content_queue",
                filters={"client_id": self.client_id, "platform": "linkedin",
                         "approved": True, "published": False, "vibe": vibe}, limit=1)
            if rows:
                return rows[0]
        return None

    def _get_evergreen(self, vibe: str) -> dict | None:
        rows = supabase_select("evergreen_reserve",
            filters={"client_id": self.client_id, "platform": "linkedin", "vibe": vibe},
            order_by="last_used", limit=1)
        if not rows:
            rows = supabase_select("evergreen_reserve",
                filters={"client_id": self.client_id, "platform": "linkedin"},
                order_by="last_used", limit=1)
        return rows[0] if rows else None

    # ── Evergreen seeding ──────────────────────────────────────────

    def seed_evergreen_reserve(self):
        self._log_action("Seeding evergreen reserve")
        vibes_to_generate = [
            ("authority", 2), ("education", 2), ("social_proof", 1),
            ("behind_scenes", 2), ("provocative", 1), ("human", 2),
        ]
        generated = 0
        for vibe, count in vibes_to_generate:
            vibe_desc = next(
                (v["desc"] for v in WEEKLY_VIBES.values() if v and v["vibe"] == vibe), vibe)
            for _ in range(count):
                post = self._generate_evergreen_post(vibe, vibe_desc)
                if post:
                    supabase_insert("evergreen_reserve", {
                        "client_id":  self.client_id,
                        "platform":   "linkedin",
                        "vibe":       vibe,
                        "content":    post,
                        "used_count": 0,
                        "last_used":  "2000-01-01T00:00:00",
                    })
                    generated += 1
        self._log_outcome(f"Evergreen reserve seeded — {generated} posts")

        def _generate_evergreen_post(self, vibe: str, vibe_desc: str) -> str | None:
    profiles = supabase_select("brand_profiles", filters={"client_id": self.client_id}, limit=1)
    profile  = profiles[0] if profiles else {}
    brand_name    = profile.get("name", self.client_id)
    tone          = profile.get("tone", "Confident, direct, intelligent. No fluff.")
    audience      = profile.get("target_audience", "B2B decision makers")
    linkedin_style = profile.get("linkedin_style", "Short paragraphs. Punchy opener. 2 hashtags max. Ends with question or CTA.")
    avoid         = profile.get("avoid", "")

    avoid_line = f"Avoid: {avoid}" if avoid else ""

    prompt = f"""Write a LinkedIn post for {brand_name}.
Vibe: {vibe} — {vibe_desc}
Brand voice: {tone}
Audience: {audience}
Style: {linkedin_style}
{avoid_line}
Length: 150-280 words. EVERGREEN — no specific dates or current events.
Write ONLY the post."""
    try:
        return call_llm(prompt, max_tokens=400, temperature=0.85)
    except Exception as e:
        logger.error(f"Evergreen generation failed [{vibe}]: {e}")
        return None

# ── Route management helpers (used by main.py endpoints) ─────────────

def get_all_routes(client_id: str) -> list:
    return supabase_select("zapier_routes", filters={"client_id": client_id}, limit=50)

def add_route(client_id: str, platform: str, account_type: str,
              account_name: str, webhook_url: str, notes: str = "") -> dict | None:
    return supabase_insert("zapier_routes", {
        "client_id": client_id, "platform": platform,
        "account_type": account_type, "account_name": account_name,
        "webhook_url": webhook_url, "notes": notes, "active": True,
    })

def toggle_route(route_id: str, active: bool) -> dict | None:
    return supabase_update("zapier_routes", row_id=route_id, data={"active": active})


# ── CLI ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv
    load_dotenv()

    agent = LinkedInAgent(client_id="aria_internal")

    if "--seed" in sys.argv:
        print("=== SEEDING EVERGREEN RESERVE ===")
        agent.seed_evergreen_reserve()
    elif "--routes" in sys.argv:
        print("=== ACTIVE ROUTES ===")
        for r in get_all_routes("aria_internal"):
            status = "✓" if r["active"] else "✗"
            print(f"  {status} {r['account_name']} [{r['platform']} / {r['account_type']}]")
    elif "--test" in sys.argv:
        print("=== TEST MODE ===")
        agent._log_action("TEST: Checked routes and content queue")
        agent._set_metric("post_published", False)
        print(json.dumps(agent.self_review(), indent=2))
    else:
        print("=== LIVE RUN ===")
        agent.execute_cycle()
        print("✓ Complete")
