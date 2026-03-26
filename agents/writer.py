"""
agents/writer.py
ARIA™ — Writer Agent (Agent 03)
Runs at 07:00 daily. Reads Research angles from content_queue.
Writes full drafts in client brand voice. Stores back to content_queue.
OUP International Ltd, 2026
"""

import os
import json
import logging
from datetime import date
from core.base_agent import BaseAgent
from core.llm import call_llm
from core.memory import supabase_select, supabase_update

logger = logging.getLogger("aria.writer")


# ── Brand voice profiles — loaded per client ─────────────────────────
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


class WriterAgent(BaseAgent):
    def __init__(self, client_id: str = "aria_internal"):
        super().__init__(name="writer", client_id=client_id)
        self.brand_voice = BRAND_VOICES.get(client_id, BRAND_VOICES["aria_internal"])

    # ═══════════════════════════════════════════════════════
    #  MAIN CYCLE
    # ═══════════════════════════════════════════════════════

    def run(self):
        # 1. Check flags (urgent angles from Research, hot leads from JAMIE)
        flags = self.check_flags(resolved=False)
        urgent_flags = [f for f in flags if f.get("priority") == "urgent"]
        if urgent_flags:
            self._log_action(f"Processing {len(urgent_flags)} urgent flags from peers")

        # 2. Read CEO instructions
        instructions = self.read_instructions()
        style_overrides = self._parse_style_instructions(instructions)

        # 3. Get content angles from Research (today's queue, not yet drafted)
        angles = self._get_pending_angles()
        self._log_action(f"Found {len(angles)} pending angles from Research")

        # 4. Get hot leads flagged by JAMIE for personalised outreach
        hot_leads = self._get_hot_leads(urgent_flags)

        # 5. Write drafts for angles
        drafted = 0
        for angle in angles[:4]:  # Cap at 4 per cycle — quality over quantity
            draft = self._write_content(angle, style_overrides)
            if draft:
                self._save_draft(angle["id"], draft, angle.get("content_type", "linkedin_post"))
                drafted += 1

        # 6. Write personalised outreach for hot leads
        outreach_drafted = 0
        for lead in hot_leads[:2]:
            outreach = self._write_outreach(lead, style_overrides)
            if outreach:
                self._save_outreach_draft(lead, outreach)
                outreach_drafted += 1

        self._log_action(f"Drafted {drafted} content pieces + {outreach_drafted} outreach")
        self._log_outcome(f"{drafted} drafts in approval queue · {outreach_drafted} personalised outreach")
        self._set_metric("drafts_written", drafted)
        self._set_metric("outreach_drafted", outreach_drafted)
        self._set_metric("angles_available", len(angles))

        # 7. Flag Research if we need more angles
        if len(angles) < 2:
            self.raise_flag(
                "research",
                f"Need more angles — only {len(angles)} available today. More B2B SaaS and AI content needed.",
                priority="normal"
            )

    # ═══════════════════════════════════════════════════════
    #  CONTENT RETRIEVAL
    # ═══════════════════════════════════════════════════════

    def _get_pending_angles(self) -> list:
        """Get Research angles not yet drafted (draft still starts with 'ANGLE:')."""
        rows = supabase_select(
            "content_queue",
            filters={
                "client_id": self.client_id,
                "created_by": "research",
                "approved": False,
                "published": False,
            },
            limit=10
        )
        # Only angles not yet written (still contain the raw angle marker)
        return [r for r in rows if r.get("draft", "").startswith("ANGLE:")]

    def _get_hot_leads(self, urgent_flags: list) -> list:
        """Get hot leads from JAMIE flags and leads table."""
        hot_leads = []

        # From urgent flags with lead context
        for flag in urgent_flags:
            if flag.get("from_agent") == "jamie":
                ctx = flag.get("context", {})
                if ctx.get("lead_id"):
                    leads = supabase_select(
                        "leads",
                        filters={"id": ctx["lead_id"], "client_id": self.client_id}
                    )
                    hot_leads.extend(leads)

        # Also check leads table directly for unflagged hot leads
        if not hot_leads:
            all_hot = supabase_select(
                "leads",
                filters={
                    "client_id": self.client_id,
                    "flagged_to": "writer",
                    "status": "new"
                },
                limit=3
            )
            hot_leads.extend(all_hot)

        return hot_leads

    def _parse_style_instructions(self, instructions: list) -> dict:
        """Extract style/tone overrides from CEO instructions."""
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

    # ═══════════════════════════════════════════════════════
    #  CONTENT WRITING
    # ═══════════════════════════════════════════════════════

    def _write_content(self, angle: dict, style_overrides: dict) -> str | None:
        """Write a full content draft from a Research angle."""
        raw_draft = angle.get("draft", "")
        content_type = angle.get("content_type", "linkedin_post")
        platform = angle.get("platform", "linkedin")

        # Parse the angle from the Research draft
        angle_text = raw_draft.replace("ANGLE:", "").split("\n\n")[0].strip()
        angle_detail = "\n\n".join(raw_draft.split("\n\n")[1:])

        voice = self.brand_voice
        style_note = ""
        if style_overrides.get("tone_override"):
            style_note = f"\nCEO override: {style_overrides['tone_override']}"
        if style_overrides.get("topic_focus"):
            style_note += f"\nFocus: {style_overrides['topic_focus']}"

        format_instructions = {
            "linkedin_post": f"LinkedIn post. Style: {voice['linkedin_style']}. 150-300 words.",
            "email": f"Marketing email. Style: {voice['email_style']}. Include subject line.",
            "blog": f"Blog article. Style: {voice['blog_style']}.",
            "thread": "Twitter/X thread. 5-8 tweets, numbered. Each tweet under 280 chars.",
        }.get(content_type, f"LinkedIn post. 150-300 words.")

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

Write the full content piece now. No preamble, no explanation — just the content itself."""

        try:
            draft = call_llm(
                prompt,
                system=f"You are a world-class copywriter for {voice['name']}. Write only the requested content.",
                max_tokens=800,
                temperature=0.75
            )
            return draft.strip()
        except Exception as e:
            logger.error(f"Writing failed for angle: {e}")
            return None

    def _write_outreach(self, lead: dict, style_overrides: dict) -> str | None:
        """Write personalised outreach for a hot lead."""
        voice = self.brand_voice
        lead_name = lead.get("display_name") or lead.get("username") or "there"
        message = lead.get("message_text", "")
        group = lead.get("group_name", "")
        score = lead.get("score", 7)
        reason = lead.get("score_reason", "")

        prompt = f"""You are the Writer agent for {voice['name']}, writing personalised outreach.

Brand voice: {voice['tone']}
Target audience: {voice['audience']}

Lead details:
- Name: {lead_name}
- Source group: {group}
- Lead score: {score}/10
- Why they're a hot lead: {reason}
- Their message/activity: {message[:300]}

Write a short, personalised LinkedIn DM or Telegram message (100-150 words max).
- Reference something specific from their message/context
- Lead with value, not a pitch
- End with a soft question or CTA
- Sound human, not automated
- Match our brand voice

Write ONLY the message — no subject line, no explanation."""

        try:
            outreach = call_llm(
                prompt,
                system=f"You are a senior BD writer for {voice['name']}. Write human, personalised outreach.",
                max_tokens=300,
                temperature=0.8
            )
            return outreach.strip()
        except Exception as e:
            logger.error(f"Outreach writing failed: {e}")
            return None

    # ═══════════════════════════════════════════════════════
    #  STORAGE
    # ═══════════════════════════════════════════════════════

    def _save_draft(self, angle_id: str, draft: str, content_type: str):
        """Update the content_queue row with the full draft."""
        supabase_update(
            "content_queue",
            row_id=angle_id,
            data={
                "draft": draft,
                "created_by": "writer",
                "approved": False,
                "published": False,
            }
        )
        self._log_outcome(f"Draft saved [{content_type}] — {len(draft)} chars")

    def _save_outreach_draft(self, lead: dict, outreach: str):
        """Store personalised outreach as a new content_queue row."""
        from core.memory import supabase_insert
        supabase_insert("content_queue", {
            "client_id": self.client_id,
            "content_type": "linkedin_post",
            "platform": "linkedin",
            "draft": f"[PERSONALISED OUTREACH — {lead.get('display_name', 'Lead')}]\n\n{outreach}",
            "approved": False,
            "published": False,
            "lead_id": lead.get("id"),
            "created_by": "writer"
        })
        self._log_outcome(f"Outreach draft saved for lead: {lead.get('display_name', '?')}")

        # Update lead status
        supabase_update(
            "leads",
            row_id=lead["id"],
            data={"status": "contacted", "flagged_to": None}
        )


# ═══════════════════════════════════════════════════════════
#  CLI TEST MODE
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
        agent._log_outcome("TEST: 2 drafts in approval queue")
        agent._set_metric("drafts_written", 2)
        reflection = agent.self_review()
        print(json.dumps(reflection, indent=2))
        print("✓ Test complete")
    elif "--demo" in sys.argv:
        print(f"=== WRITER AGENT — DEMO WRITE ===")
        voice = agent.brand_voice
        print(f"Brand: {voice['name']} · Tone: {voice['tone'][:60]}...")
        # Write one sample piece
        sample_angle = {
            "id": "demo",
            "draft": "ANGLE: Why AI marketing agencies outperform in-house teams at 1/5 the cost\n\nStudies show AI-powered content creation delivers 3x the output at 20% the cost of traditional agencies. Here's the breakdown.",
            "content_type": "linkedin_post",
            "platform": "linkedin"
        }
        result = agent._write_content(sample_angle, {})
        print("\n--- DRAFT OUTPUT ---\n")
        print(result)
    else:
        print(f"=== WRITER AGENT — LIVE RUN (client: {client_id}) ===")
        agent.execute_cycle()
        print("✓ Cycle complete")
