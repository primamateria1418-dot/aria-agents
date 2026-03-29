"""
agents/writer.py
ARIA™ — Writer Agent (Agent 03)
Runs at 07:00 daily. Reads Research angles from content_queue.
Writes full drafts in client brand voice. Stores back to content_queue.
Also handles on-demand writes from LinkedIn agent and CEO/client requests.
OUP International Ltd, 2026
"""

import os
import json
import logging
from datetime import date, datetime
from core.base_agent import BaseAgent
from core.llm import call_llm
from core.memory import supabase_select, supabase_update, supabase_insert

logger = logging.getLogger("aria.writer")


# ── Brand voice profiles ──────────────────────────────────────────────
BRAND_VOICES = {
    "aria_internal": {
        "name": "ARIA",
        "tone": "confident, direct, intelligent. No fluff. We know our stuff.",
        "audience": "startup founders, Series A/B, marketing leaders, B2B SaaS",
        "avoid": "passive voice, clichés like 'game-changing' or 'revolutionise', excessive emojis",
        "linkedin_style": "short paragraphs, punchy opener, ends with a question or strong CTA",
        "email_style": "clear subject line, 3 paragraphs max, one CTA",
        "blog_style": "1,500-2,000 words, SEO-optimised, H2 subheadings, practical takeaways",
    },
    "asset_club": {
        "name": "The Asset Club",
        "tone": "authoritative, premium, investor-grade. Speaks to HNW and institutional audiences.",
        "audience": "HNW investors, family offices, fractional property buyers, asset managers",
        "avoid": "overly casual tone, startup jargon, anything that sounds retail",
        "linkedin_style": "measured, data-led, builds authority. 3-5 paragraphs.",
        "email_style": "elegant, brief. One key insight per email.",
        "blog_style": "Long-form analysis, 1,800-2,500 words, cite data and sources",
    },
    "oup_intl": {
        "name": "Our United Planet",
        "tone": "purposeful, transparent, evidence-based. Mission-driven without being preachy.",
        "audience": "foundation programme officers, impact investors, CSR leads, NGO partners",
        "avoid": "charity clichés, vague impact claims, emotional manipulation",
        "linkedin_style": "lead with the blockchain/AI transparency angle. Specific data.",
        "email_style": "concise grant narrative style. Demonstrate measurable impact.",
        "blog_style": "Impact reports, 1,200-1,800 words, specific outcomes, transparent methodology",
    }
}

# ── Vibe writing guides for LinkedIn agent requests ───────────────────
VIBE_GUIDES = {
    "authority":    "Market insight or bold data-led take. Open with a surprising stat or claim. Build the case. End with a forward-looking question.",
    "education":    "Practical how-to or framework. Numbered steps or a clear before/after. Audience leaves knowing how to do something specific.",
    "social_proof": "Anonymised client result or case study. Lead with the outcome ('One of our clients grew qualified leads by 40% in 6 weeks.'). Story > statistics.",
    "behind_scenes":"Pull back the curtain on how ARIA's agents work. Make AI feel human and fascinating, not threatening.",
    "provocative":  "Take a position that challenges conventional marketing wisdom. Be specific. Expect disagreement — that's the point.",
    "human":        "Founder story, genuine observation, something relatable. The person behind the brand. Vulnerability is a superpower here.",
}


class WriterAgent(BaseAgent):
    def __init__(self, client_id: str = "aria_internal"):
        super().__init__(name="writer", client_id=client_id)
        self.brand_voice = self._get_brand_voice()

    def _get_brand_voice(self) -> dict:
        """Load brand voice from Supabase brand_profiles. Falls back to hardcoded dict."""
        try:
            profiles = supabase_select(
                "brand_profiles",
                filters={"client_id": self.client_id},
                limit=1
            )
            if profiles:
                p = profiles[0]
                return {
                    "name":          p.get("name", self.client_id),
                    "tone":          p.get("tone", "Professional and direct."),
                    "audience":      p.get("target_audience", "Business professionals"),
                    "avoid":         p.get("avoid", ""),
                    "linkedin_style":p.get("linkedin_style", "Short paragraphs, punchy opener, ends with question or CTA."),
                    "email_style":   p.get("email_style", "Clear subject line, 3 paragraphs max, one CTA."),
                    "blog_style":    p.get("blog_style", "1,500-2,000 words, SEO-optimised, H2 subheadings."),
                }
        except Exception as e:
            logger.warning(f"Could not load brand_profiles for {self.client_id}: {e}")
        # Fallback to hardcoded
        return BRAND_VOICES.get(self.client_id, BRAND_VOICES["aria_internal"])

    # ═══════════════════════════════════════════════════════
    #  MAIN DAILY CYCLE
    # ═══════════════════════════════════════════════════════

    def run(self):
        # 1. Check flags — LinkedIn urgent requests take priority
        flags = self.check_flags(resolved=False)
        linkedin_flags = [
            f for f in flags
            if f.get("from_agent") == "linkedin" and f.get("priority") == "urgent"
        ]
        other_urgent = [
            f for f in flags
            if f.get("priority") == "urgent" and f.get("from_agent") != "linkedin"
        ]

        # 2. Handle LinkedIn on-demand requests first
        for flag in linkedin_flags:
            ctx = flag.get("context", {})
            vibe = ctx.get("vibe")
            day  = ctx.get("day", date.today().strftime("%A"))
            if vibe:
                self._log_action(f"LinkedIn urgent request — writing {vibe} post for {day}")
                self._write_for_day(day, vibe, auto_approve=True)
                self.resolve_flag(flag["id"])

        # 3. Handle CEO/client on-demand requests
        on_demand = self._get_pending_on_demand_requests()
        for req in on_demand:
            self._log_action(f"On-demand request: {req.get('topic', '?')[:60]}")
            self._write_on_demand(req)

        # 4. CEO instructions
        instructions = self.read_instructions()
        style_overrides = self._parse_style_instructions(instructions)

        # 5. Standard Research angles
        angles = self._get_pending_angles()
        self._log_action(f"Found {len(angles)} pending angles from Research")

        hot_leads = self._get_hot_leads(other_urgent)

        drafted = 0
        for angle in angles[:4]:
            draft = self._write_content(angle, style_overrides)
            if draft:
                self._save_draft(angle["id"], draft, angle.get("content_type", "linkedin_post"))
                drafted += 1

        outreach_drafted = 0
        for lead in hot_leads[:2]:
            outreach = self._write_outreach(lead, style_overrides)
            if outreach:
                self._save_outreach_draft(lead, outreach)
                outreach_drafted += 1

        self._log_action(f"Drafted {drafted} content pieces + {outreach_drafted} outreach")
        self._log_outcome(f"{drafted} drafts in review queue · {outreach_drafted} personalised outreach")
        self._set_metric("drafts_written", drafted)
        self._set_metric("outreach_drafted", outreach_drafted)
        self._set_metric("on_demand_written", len(on_demand))

        if len(angles) < 2:
            self.raise_flag(
                "research",
                f"Need more angles — only {len(angles)} available today.",
                priority="normal"
            )

    # ═══════════════════════════════════════════════════════
    #  ON-DEMAND: LINKEDIN DAY/VIBE
    # ═══════════════════════════════════════════════════════

    def _write_for_day(self, day: str, vibe: str, auto_approve: bool = False) -> dict | None:
        """
        Write a LinkedIn post for a specific day vibe.
        Called directly by LinkedIn agent via flag.
        auto_approve=True so LinkedIn agent can post without CEO bottleneck.
        """
        voice     = self.brand_voice
        vibe_desc = VIBE_GUIDES.get(vibe, f"LinkedIn post with a {vibe} angle.")

        prompt = f"""You are the Writer agent for {voice['name']}.

Brand voice: {voice['tone']}
Target audience: {voice['audience']}
Avoid: {voice['avoid']}
LinkedIn style: {voice['linkedin_style']}

Today is {day}. Write a LinkedIn post with this vibe:
Vibe: {vibe}
Direction: {vibe_desc}

Length: 150-280 words.
No hashtag spam — 2 maximum, only if genuinely relevant.
No emojis unless they serve the post.
Write ONLY the post. No preamble, no explanation."""

        try:
            content = call_llm(prompt, max_tokens=400, temperature=0.8)
            content = content.strip()
        except Exception as e:
            logger.error(f"_write_for_day failed [{vibe}]: {e}")
            return None

        row = supabase_insert("content_queue", {
            "client_id":    self.client_id,
            "content_type": "linkedin_post",
            "platform":     "linkedin",
            "draft":        content,
            "vibe":         vibe,
            "scheduled_for":date.today().isoformat(),
            "approved":     auto_approve,
            "published":    False,
            "status":       "approved" if auto_approve else "pending_review",
            "created_by":   "writer",
            "requested_by": "linkedin",
        })

        self._log_outcome(f"Wrote {vibe} post for {day} — {len(content)} chars — auto_approve={auto_approve}")
        return row

    # ═══════════════════════════════════════════════════════
    #  ON-DEMAND: CEO / CLIENT REQUEST
    # ═══════════════════════════════════════════════════════

    def _write_on_demand(self, request: dict) -> dict | None:
        """
        Write content from a CEO or client request submitted via dashboard.
        Saves to content_queue with status='pending_review' for CEO approval.
        """
        topic       = request.get("topic", "")
        format_type = request.get("format", "linkedin_post")
        platform    = request.get("platform", "linkedin")
        requested_by= request.get("requested_by", "client")
        request_id  = request.get("id")
        vibe        = request.get("vibe", "")

        voice = self.brand_voice

        format_instructions = {
            "linkedin_post": f"LinkedIn post. Style: {voice['linkedin_style']}. 150-300 words.",
            "email":         f"Marketing email with subject line. Style: {voice['email_style']}.",
            "blog":          f"Blog article. Style: {voice['blog_style']}.",
            "thread":        "Twitter/X thread. 5-8 tweets, numbered. Each under 280 chars.",
        }.get(format_type, "LinkedIn post. 150-300 words.")

        vibe_note = f"\nVibe/angle: {VIBE_GUIDES.get(vibe, vibe)}" if vibe else ""

        prompt = f"""You are the Writer agent for {voice['name']}.

Brand voice: {voice['tone']}
Target audience: {voice['audience']}
Avoid: {voice['avoid']}
Format: {format_instructions}{vibe_note}

Content request: {topic}

Today's date: {date.today().strftime('%d %B %Y')}

Write the full content piece now. No preamble — just the content."""

        try:
            content = call_llm(prompt, max_tokens=900, temperature=0.75)
            content = content.strip()
        except Exception as e:
            logger.error(f"_write_on_demand failed: {e}")
            # Mark request as failed
            if request_id:
                supabase_update(
                    "content_requests",
                    row_id=request_id,
                    data={"status": "failed", "error": str(e)}
                )
            return None

        # Save draft to content_queue
        row = supabase_insert("content_queue", {
            "client_id":    self.client_id,
            "content_type": format_type,
            "platform":     platform,
            "draft":        content,
            "vibe":         vibe or None,
            "approved":     False,
            "published":    False,
            "status":       "pending_review",
            "created_by":   "writer",
            "requested_by": requested_by,
            "request_id":   request_id,
        })

        # Mark original request as fulfilled
        if request_id:
            supabase_update(
                "content_requests",
                row_id=request_id,
                data={
                    "status":       "fulfilled",
                    "fulfilled_at": datetime.utcnow().isoformat(),
                    "content_id":   row.get("id") if row else None,
                }
            )

        self._log_outcome(f"On-demand draft written [{format_type}] — {len(content)} chars — requested by: {requested_by}")
        return row

    def _get_pending_on_demand_requests(self) -> list:
        """Get unfulfilled content requests from CEO or clients."""
        return supabase_select(
            "content_requests",
            filters={
                "client_id": self.client_id,
                "status":    "pending",
            },
            limit=5
        )

    # ═══════════════════════════════════════════════════════
    #  STANDARD PIPELINE (unchanged from original)
    # ═══════════════════════════════════════════════════════

    def _get_pending_angles(self) -> list:
        rows = supabase_select(
            "content_queue",
            filters={
                "client_id":  self.client_id,
                "created_by": "research",
                "approved":   False,
                "published":  False,
            },
            limit=10
        )
        return [r for r in rows if r.get("draft", "").startswith("ANGLE:")]

    def _get_hot_leads(self, urgent_flags: list) -> list:
        hot_leads = []
        for flag in urgent_flags:
            if flag.get("from_agent") == "jamie":
                ctx = flag.get("context", {})
                if ctx.get("lead_id"):
                    leads = supabase_select(
                        "leads",
                        filters={"id": ctx["lead_id"], "client_id": self.client_id}
                    )
                    hot_leads.extend(leads)
        if not hot_leads:
            all_hot = supabase_select(
                "leads",
                filters={
                    "client_id":  self.client_id,
                    "flagged_to": "writer",
                    "status":     "new"
                },
                limit=3
            )
            hot_leads.extend(all_hot)
        return hot_leads

    def _parse_style_instructions(self, instructions: list) -> dict:
        overrides = {}
        for inst in instructions:
            text = inst.get("instruction", "").lower()
            if "tone" in text:
                overrides["tone_override"] = inst["instruction"]
            if "focus on" in text or "write about" in text:
                overrides["topic_focus"] = inst["instruction"]
            if "word count" in text or "length" in text:
                overrides["length_override"] = inst["instruction"]
        return overrides

    def _write_content(self, angle: dict, style_overrides: dict) -> str | None:
        raw_draft    = angle.get("draft", "")
        content_type = angle.get("content_type", "linkedin_post")
        voice        = self.brand_voice

        angle_text   = raw_draft.replace("ANGLE:", "").split("\n\n")[0].strip()
        angle_detail = "\n\n".join(raw_draft.split("\n\n")[1:])

        style_note = ""
        if style_overrides.get("tone_override"):
            style_note = f"\nCEO override: {style_overrides['tone_override']}"
        if style_overrides.get("topic_focus"):
            style_note += f"\nFocus: {style_overrides['topic_focus']}"

        format_instructions = {
            "linkedin_post": f"LinkedIn post. Style: {voice['linkedin_style']}. 150-300 words.",
            "email":         f"Marketing email. Style: {voice['email_style']}. Include subject line.",
            "blog":          f"Blog article. Style: {voice['blog_style']}.",
            "thread":        "Twitter/X thread. 5-8 tweets, numbered. Each tweet under 280 chars.",
        }.get(content_type, "LinkedIn post. 150-300 words.")

        prompt = f"""You are the Writer agent for {voice['name']}.

Brand voice: {voice['tone']}
Target audience: {voice['audience']}
Avoid: {voice['avoid']}
Format: {format_instructions}
{style_note}

Content angle to develop:
{angle_text}

Additional context:
{angle_detail}

Today's date: {date.today().strftime('%d %B %Y')}

Write the full content piece now. No preamble — just the content."""

        try:
            return call_llm(prompt, max_tokens=800, temperature=0.75).strip()
        except Exception as e:
            logger.error(f"Writing failed for angle: {e}")
            return None

    def _write_outreach(self, lead: dict, style_overrides: dict) -> str | None:
        voice     = self.brand_voice
        lead_name = lead.get("display_name") or lead.get("username") or "there"
        message   = lead.get("message_text", "")
        group     = lead.get("group_name", "")
        score     = lead.get("score", 7)
        reason    = lead.get("score_reason", "")

        prompt = f"""You are the Writer agent for {voice['name']}, writing personalised outreach.

Brand voice: {voice['tone']}
Target audience: {voice['audience']}

Lead details:
- Name: {lead_name}
- Source group: {group}
- Lead score: {score}/10
- Why they're a hot lead: {reason}
- Their message/activity: {message[:300]}

Write a short, personalised LinkedIn DM (100-150 words max).
Reference something specific from their context. Lead with value. End with a soft CTA.
Sound human, not automated. Write ONLY the message."""

        try:
            return call_llm(prompt, max_tokens=300, temperature=0.8).strip()
        except Exception as e:
            logger.error(f"Outreach writing failed: {e}")
            return None

    def _save_draft(self, angle_id: str, draft: str, content_type: str):
        supabase_update(
            "content_queue",
            row_id=angle_id,
            data={
                "draft":      draft,
                "created_by": "writer",
                "approved":   False,
                "published":  False,
                "status":     "pending_review",
            }
        )
        self._log_outcome(f"Draft saved [{content_type}] — {len(draft)} chars")

    def _save_outreach_draft(self, lead: dict, outreach: str):
        supabase_insert("content_queue", {
            "client_id":    self.client_id,
            "content_type": "linkedin_post",
            "platform":     "linkedin",
            "draft":        f"[PERSONALISED OUTREACH — {lead.get('display_name', 'Lead')}]\n\n{outreach}",
            "approved":     False,
            "published":    False,
            "status":       "pending_review",
            "lead_id":      lead.get("id"),
            "created_by":   "writer",
            "requested_by": "writer",
        })
        supabase_update(
            "leads",
            row_id=lead["id"],
            data={"status": "contacted", "flagged_to": None}
        )
        self._log_outcome(f"Outreach draft saved for lead: {lead.get('display_name', '?')}")


# ═══════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv
    load_dotenv()

    client_id = "aria_internal"
    for arg in sys.argv:
        if arg.startswith("--client="):
            client_id = arg.split("=")[1]

    agent = WriterAgent(client_id=client_id)

    if "--test" in sys.argv:
        print(f"=== WRITER AGENT — TEST MODE (client: {client_id}) ===")
        agent._log_action("TEST: Wrote 2 dummy drafts")
        agent._set_metric("drafts_written", 2)
        print(json.dumps(agent.self_review(), indent=2))

    elif "--demo" in sys.argv:
        print("=== WRITER AGENT — DEMO: on-demand vibe write ===")
        result = agent._write_for_day("Monday", "authority", auto_approve=False)
        print(f"Result: {result}")

    elif "--on-demand" in sys.argv:
        print("=== WRITER AGENT — DEMO: client request ===")
        fake_request = {
            "id":           "demo-001",
            "client_id":    client_id,
            "topic":        "Why most startups waste 60% of their marketing budget in year one",
            "format":       "linkedin_post",
            "platform":     "linkedin",
            "vibe":         "provocative",
            "requested_by": "client",
            "status":       "pending",
        }
        result = agent._write_on_demand(fake_request)
        print(f"Result: {result}")

    else:
        print(f"=== WRITER AGENT — LIVE RUN (client: {client_id}) ===")
        agent.execute_cycle()
        print("✓ Cycle complete")
