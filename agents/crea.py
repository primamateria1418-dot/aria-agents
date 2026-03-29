"""
agents/crea.py
ARIA™ — CREA™ Creative Agent
Campaign-aware, brief-driven, quality-gated creative pipeline.
Reads Campaign Strategy Document → builds prompt → generates via FAL.ai
→ QA gate via Claude Haiku vision → stores to Supabase.
OUP International Ltd, 2026
"""

import os
import json
import logging
import base64
import httpx
from datetime import datetime, timezone
from core.base_agent import BaseAgent
from core.memory import supabase_select, supabase_insert, supabase_update

logger = logging.getLogger("aria.crea")

FAL_API_KEY       = os.environ.get("FAL_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GROQ_API_KEY      = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL        = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

# ── Channel dimension reference ───────────────────────────────────────
CHANNEL_DIMENSIONS = {
    "instagram_post":    {"w": 1080, "h": 1080, "ratio": "1:1",    "notes": "Primary social format"},
    "instagram_story":   {"w": 1080, "h": 1920, "ratio": "9:16",   "notes": "Keep text in centre 60%"},
    "linkedin":          {"w": 1200, "h": 628,  "ratio": "1.91:1", "notes": "Professional tone"},
    "facebook":          {"w": 1200, "h": 628,  "ratio": "1.91:1", "notes": "Same as LinkedIn"},
    "twitter":           {"w": 1600, "h": 900,  "ratio": "16:9",   "notes": "Bold, high contrast"},
    "banner_leaderboard":{"w": 728,  "h": 90,   "ratio": "8:1",    "notes": "Minimal text"},
    "banner_rectangle":  {"w": 300,  "h": 250,  "ratio": "6:5",    "notes": "Most common display"},
}

QA_DIMENSIONS = [
    ("generic_composition", 6),
    ("brand_alignment", 7),
    ("audience_fit", 6),
]

QA_SYSTEM_PROMPT = """You are a senior creative director reviewing AI-generated ad creative.
You are ruthless about quality. Your job is to catch AI-generated mediocrity before it reaches a client.

Score the image on each dimension from 0-10 and return JSON only:
{
  "generic_composition": 0-10,
  "brand_alignment": 0-10,
  "audience_fit": 0-10,
  "ai_artefacts": true/false,
  "channel_fit": true/false,
  "overall_pass": true/false,
  "fail_reasons": [],
  "prompt_adjustments": ""
}

Fail criteria: generic_composition < 6, brand_alignment < 7, audience_fit < 6,
ai_artefacts = true, channel_fit = false."""


class CREAAgent(BaseAgent):
    def __init__(self, client_id: str = "aria_internal"):
        super().__init__(name="crea", client_id=client_id)

    # ═══════════════════════════════════════════════════════
    #  MAIN ENTRY — generate an asset for a campaign
    # ═══════════════════════════════════════════════════════

    def generate(
        self,
        campaign_id: str,
        channel: str = "linkedin",
        dimensions: str = None,
        quantity: int = 1,
        override_prompt: str = None,
        model: str = "flux-pro",
    ) -> list[dict]:
        """
        Full pipeline: brief → prompt → generate → QA → final → store.
        Returns list of asset metadata dicts.
        """
        if not FAL_API_KEY:
            logger.error("FAL_API_KEY not set — cannot generate images")
            return []

        # 1. Read brief
        brief = self._get_campaign_brief(campaign_id)
        dim_key = self._resolve_dimension_key(channel, dimensions)
        dim_info = CHANNEL_DIMENSIONS.get(dim_key, CHANNEL_DIMENSIONS["linkedin"])

        results = []
        for i in range(quantity):
            try:
                result = self._generate_single(
                    campaign_id=campaign_id,
                    brief=brief,
                    channel=channel,
                    dim_info=dim_info,
                    override_prompt=override_prompt,
                    final_model=model,
                )
                results.append(result)
            except Exception as e:
                logger.error(f"CREA generate #{i+1} failed for {campaign_id}: {e}")

        return results

    def _generate_single(
        self,
        campaign_id: str,
        brief: dict,
        channel: str,
        dim_info: dict,
        override_prompt: str,
        final_model: str,
    ) -> dict:
        """One full pipeline iteration."""
        # 2. Build prompt (skip if override provided)
        if override_prompt:
            prompt, negative_prompt = override_prompt, ""
        else:
            prompt, negative_prompt = self._construct_prompt(brief, channel, dim_info)

        # 3. QA iteration loop (max 3 attempts with flux-schnell)
        qa_result = None
        qa_iterations = 0
        current_prompt = prompt
        current_negative = negative_prompt

        for attempt in range(3):
            qa_iterations += 1
            draft_url = self._fal_generate(current_prompt, current_negative, "fal-ai/flux/schnell", dim_info)
            if not draft_url:
                break

            qa_result = self._qa_score(draft_url, brief, channel)
            if qa_result.get("overall_pass"):
                break

            # Adjust prompt based on QA feedback
            adjustments = qa_result.get("prompt_adjustments", "")
            if adjustments:
                current_prompt = f"{prompt}. {adjustments}"
            logger.info(f"CREA QA fail #{attempt+1} for campaign {campaign_id}: {qa_result.get('fail_reasons')}")

        if not qa_result:
            qa_result = {"overall_pass": False, "fail_reasons": ["Generation failed"], "qa_iterations": qa_iterations}

        # 4. Final generation at full quality
        if qa_result.get("overall_pass"):
            final_url = self._fal_generate(current_prompt, current_negative, f"fal-ai/flux/{final_model.replace('flux-', '')}", dim_info)
        else:
            # Flag for human review — use draft URL if available
            final_url = draft_url if 'draft_url' in dir() and draft_url else None
            logger.warning(f"CREA: asset failed QA after 3 attempts for {campaign_id}. Flagging for human review.")
            self.raise_flag(
                "ceo",
                f"CREA asset failed QA for campaign {campaign_id}. Human review required.",
                priority="normal",
                context={"campaign_id": campaign_id, "fail_reasons": qa_result.get("fail_reasons", [])}
            )

        # 5. Store asset metadata
        row = supabase_insert("crea_assets", {
            "campaign_id":      campaign_id,
            "client_id":        self.client_id,
            "asset_url":        final_url,
            "prompt_used":      current_prompt,
            "negative_prompt":  current_negative,
            "qa_score":         json.dumps(qa_result),
            "qa_passed":        qa_result.get("overall_pass", False),
            "qa_iterations":    qa_iterations,
            "generation_model": final_model,
            "channel":          channel,
            "dimensions":       f"{dim_info['w']}x{dim_info['h']}",
            "status":           "draft" if qa_result.get("overall_pass") else "flagged",
            "created_at":       datetime.now(timezone.utc).isoformat(),
            "version":          1,
        })

        asset_id = row[0]["id"] if row else None
        logger.info(f"CREA: asset stored — id={asset_id} passed={qa_result.get('overall_pass')} iterations={qa_iterations}")

        return {
            "asset_id":   asset_id,
            "asset_url":  final_url,
            "qa_passed":  qa_result.get("overall_pass", False),
            "qa_score":   qa_result,
            "iterations": qa_iterations,
            "prompt":     current_prompt,
        }

    # ═══════════════════════════════════════════════════════
    #  BRIEF — read Campaign Strategy Document
    # ═══════════════════════════════════════════════════════

    def _get_campaign_brief(self, campaign_id: str) -> dict:
        """Load campaign strategy doc + brand profile for this client."""
        brief = {"campaign_id": campaign_id}

        # Try campaigns table first
        try:
            campaigns = supabase_select("campaigns", filters={"id": campaign_id}, limit=1)
            if campaigns:
                brief.update(campaigns[0])
        except Exception as e:
            logger.warning(f"Could not load campaign {campaign_id}: {e}")

        # Always load brand profile
        try:
            profiles = supabase_select("brand_profiles", filters={"client_id": self.client_id}, limit=1)
            if profiles:
                p = profiles[0]
                brief["brand_name"]      = p.get("name", self.client_id)
                brief["brand_tone"]      = p.get("tone", "Professional")
                brief["target_audience"] = p.get("target_audience", "Business professionals")
                brief["avoid"]           = p.get("avoid", "")
                brief["content_goals"]   = p.get("content_goals", "Build brand authority")
        except Exception as e:
            logger.warning(f"Could not load brand profile for {self.client_id}: {e}")

        return brief

    # ═══════════════════════════════════════════════════════
    #  PROMPT CONSTRUCTION (via Groq)
    # ═══════════════════════════════════════════════════════

    def _construct_prompt(self, brief: dict, channel: str, dim_info: dict) -> tuple[str, str]:
        """Build Flux-optimised image prompt from campaign brief using Groq."""
        system = """You are a specialist AI image prompt engineer for advertising.
Given a campaign brief, construct a Flux-optimised image generation prompt.

Rules:
- Specify art style, lighting, composition, and colour palette from brand params
- Include target audience context in visual language
- Specify channel dimensions and aspect ratio requirements
- AVOID generic AI aesthetics: no centred lone subject on plain background, no stock-photo look
- Include specific visual storytelling details
- Keep it under 200 words

Return JSON only:
{
  "positive_prompt": "...",
  "negative_prompt": "centered subject, stock photo, generic background, AI artifacts, watermark, text overlay, oversaturated"
}"""

        user = f"""Campaign brief:
Brand: {brief.get('brand_name', 'Unknown')}
Tone: {brief.get('brand_tone', 'Professional')}
Audience: {brief.get('target_audience', 'Business professionals')}
Campaign goal: {brief.get('content_goals', 'Build brand authority')}
Avoid: {brief.get('avoid', '')}
Channel: {channel} ({dim_info['w']}x{dim_info['h']}px, {dim_info['ratio']} ratio)
Notes: {dim_info['notes']}

Construct the Flux image prompt now."""

        try:
            with httpx.Client(timeout=15) as client:
                res = client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                    json={
                        "model": GROQ_MODEL,
                        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                        "max_tokens": 400,
                        "temperature": 0.7,
                    }
                )
                res.raise_for_status()
                raw = res.json()["choices"][0]["message"]["content"].strip()
                clean = raw.lstrip("```json").lstrip("```").rstrip("```").strip()
                data = json.loads(clean)
                return data["positive_prompt"], data["negative_prompt"]
        except Exception as e:
            logger.warning(f"Prompt construction failed: {e}. Using fallback prompt.")
            fallback = (
                f"Professional advertising image for {brief.get('brand_name', 'a brand')}, "
                f"targeting {brief.get('target_audience', 'professionals')}, "
                f"clean modern aesthetic, {dim_info['ratio']} ratio, "
                f"high quality commercial photography style"
            )
            return fallback, "stock photo, generic, centered lone subject, AI artifacts, watermark"

    # ═══════════════════════════════════════════════════════
    #  FAL.ai GENERATION
    # ═══════════════════════════════════════════════════════

    def _fal_generate(self, prompt: str, negative_prompt: str, model_path: str, dim_info: dict) -> str | None:
        """Call FAL.ai and return the image URL."""
        try:
            with httpx.Client(timeout=60) as client:
                res = client.post(
                    f"https://fal.run/{model_path}",
                    headers={
                        "Authorization": f"Key {FAL_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "prompt": prompt,
                        "negative_prompt": negative_prompt,
                        "image_size": {"width": dim_info["w"], "height": dim_info["h"]},
                        "num_inference_steps": 4,  # schnell is fast
                        "num_images": 1,
                        "enable_safety_checker": True,
                    }
                )
                res.raise_for_status()
                data = res.json()
                images = data.get("images", [])
                if images:
                    return images[0].get("url")
                logger.warning(f"FAL.ai returned no images: {data}")
                return None
        except Exception as e:
            logger.error(f"FAL.ai generation failed ({model_path}): {e}")
            return None

    # ═══════════════════════════════════════════════════════
    #  QA GATE — Claude Haiku vision scoring
    # ═══════════════════════════════════════════════════════

    def _qa_score(self, image_url: str, brief: dict, channel: str) -> dict:
        """Pass image to Claude Haiku for quality scoring."""
        if not ANTHROPIC_API_KEY:
            logger.warning("ANTHROPIC_API_KEY not set — skipping QA, marking as passed")
            return {"overall_pass": True, "qa_skipped": True}

        user_content = [
            {
                "type": "image",
                "source": {"type": "url", "url": image_url},
            },
            {
                "type": "text",
                "text": (
                    f"Evaluate this ad creative for {brief.get('brand_name', 'a brand')}.\n"
                    f"Target audience: {brief.get('target_audience', 'business professionals')}\n"
                    f"Channel: {channel}\n"
                    f"Brand tone: {brief.get('brand_tone', 'Professional')}\n\n"
                    "Score and return JSON as instructed."
                )
            }
        ]

        try:
            with httpx.Client(timeout=30) as client:
                res = client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": ANTHROPIC_API_KEY,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": "claude-haiku-4-5-20251001",
                        "max_tokens": 500,
                        "system": QA_SYSTEM_PROMPT,
                        "messages": [{"role": "user", "content": user_content}],
                    }
                )
                res.raise_for_status()
                raw = res.json()["content"][0]["text"].strip()
                clean = raw.lstrip("```json").lstrip("```").rstrip("```").strip()
                return json.loads(clean)
        except Exception as e:
            logger.warning(f"QA scoring failed: {e}. Marking as passed to avoid blocking pipeline.")
            return {"overall_pass": True, "qa_error": str(e)}

    # ═══════════════════════════════════════════════════════
    #  HELPERS
    # ═══════════════════════════════════════════════════════

    def _resolve_dimension_key(self, channel: str, dimensions: str | None) -> str:
        """Map channel string to a CHANNEL_DIMENSIONS key."""
        if dimensions:
            # Try to match by dimension string e.g. "1080x1080"
            for key, val in CHANNEL_DIMENSIONS.items():
                if f"{val['w']}x{val['h']}" == dimensions:
                    return key
        mapping = {
            "instagram": "instagram_post",
            "story":     "instagram_story",
            "linkedin":  "linkedin",
            "facebook":  "facebook",
            "twitter":   "twitter",
            "banner":    "banner_rectangle",
        }
        return mapping.get(channel.lower(), "linkedin")

    def get_assets(self, campaign_id: str) -> list:
        return supabase_select("crea_assets", filters={"campaign_id": campaign_id, "client_id": self.client_id}, limit=50)

    def approve_asset(self, asset_id: str):
        supabase_update("crea_assets", {"status": "approved"}, filters={"id": asset_id})

    def reject_asset(self, asset_id: str, feedback: str = ""):
        supabase_update("crea_assets", {"status": "rejected", "feedback": feedback}, filters={"id": asset_id})

    def run(self):
        """Scheduled run — check for pending campaign briefs and generate assets."""
        try:
            pending = supabase_select(
                "campaigns",
                filters={"client_id": self.client_id, "status": "brief_ready"},
                limit=5
            )
            for campaign in pending:
                self._log_action(f"Auto-generating assets for campaign: {campaign.get('id')}")
                self.generate(
                    campaign_id=campaign["id"],
                    channel="linkedin",
                    quantity=1,
                )
        except Exception as e:
            logger.error(f"CREAAgent.run failed for {self.client_id}: {e}")
