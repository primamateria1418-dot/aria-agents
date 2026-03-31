"""
agents/crea.py
ARIA™ — CREA™ Creative Agent
ComfyUI (local via ngrok) + Groq prompt engineering.
No FAL.ai. No Anthropic API. Groq handles prompt expansion.
QA gate is rule-based (generation success = pass).
OUP International Ltd, 2026
"""

import os
import json
import time
import logging
import httpx
from datetime import datetime, timezone
from core.base_agent import BaseAgent
from core.memory import supabase_select, supabase_insert, supabase_update

logger = logging.getLogger("aria.crea")

GROQ_API_KEY   = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL     = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
COMFYUI_NGROK  = os.environ.get("COMFYUI_NGROK", "http://127.0.0.1:8188")

# ── ComfyUI polling config ─────────────────────────────────────────────
POLL_INTERVAL  = 3    # seconds between /history polls
POLL_TIMEOUT   = 180  # seconds before giving up

# ── Channel dimension reference ────────────────────────────────────────
CHANNEL_DIMENSIONS = {
    "instagram_post":     {"w": 1024, "h": 1024, "ratio": "1:1",    "notes": "Primary social format"},
    "instagram_story":    {"w": 768,  "h": 1344, "ratio": "9:16",   "notes": "Keep text in centre 60%"},
    "linkedin":           {"w": 1216, "h": 640,  "ratio": "1.91:1", "notes": "Professional tone"},
    "facebook":           {"w": 1216, "h": 640,  "ratio": "1.91:1", "notes": "Same as LinkedIn"},
    "twitter":            {"w": 1344, "h": 768,  "ratio": "16:9",   "notes": "Bold, high contrast"},
    "banner_leaderboard": {"w": 1344, "h": 768,  "ratio": "8:1",    "notes": "Minimal text"},
    "banner_rectangle":   {"w": 768,  "h": 640,  "ratio": "6:5",    "notes": "Most common display"},
}

# ── Groq prompt engineering system prompt ──────────────────────────────
PROMPT_ENGINEER_SYSTEM = """You are a specialist AI image prompt engineer for advertising.
Given a campaign brief, construct a JuggernautXL-optimised image generation prompt.

Rules:
- Specify art style, lighting, composition, and colour palette derived from brand params
- Include target audience context in visual language
- AVOID generic AI aesthetics: no centred lone subject on plain background, no stock-photo look
- Include specific visual storytelling details, textures, environments
- Optimise for photorealism — JuggernautXL excels at this
- Keep positive prompt under 180 words

Return JSON only — no markdown, no backticks, no preamble:
{
  "positive_prompt": "...",
  "negative_prompt": "centered subject, stock photo, generic background, AI artifacts, watermark, text overlay, oversaturated, blurry, deformed, bad anatomy, ugly, duplicate"
}"""


class CREAAgent(BaseAgent):
    def __init__(self, client_id: str = "aria_internal"):
        super().__init__(name="crea", client_id=client_id)

    # ═══════════════════════════════════════════════════════
    #  HEALTH
    # ═══════════════════════════════════════════════════════

    def health(self) -> dict:
        """Ping ComfyUI via ngrok and return status."""
        try:
            with httpx.Client(timeout=8) as client:
                res = client.get(f"{COMFYUI_NGROK}/system_stats")
                res.raise_for_status()
                stats = res.json()
                return {
                    "status": "online",
                    "ngrok_url": COMFYUI_NGROK,
                    "comfyui": stats,
                }
        except Exception as e:
            return {
                "status": "offline",
                "ngrok_url": COMFYUI_NGROK,
                "error": str(e),
                "hint": "Start ComfyUI: cd C:\\AI\\ComfyUI && python main.py, then ngrok http 8188 --url=setsuko-tabernacular-necole.ngrok-free.dev",
            }

    # ═══════════════════════════════════════════════════════
    #  PROMPT EXPANSION (called by /agents/crea/expand-prompt)
    # ═══════════════════════════════════════════════════════

    def expand_prompt(self, brief: str, style: str = "photorealistic", format: str = "linkedin") -> dict:
        """
        Groq expands a short user brief into a full JuggernautXL prompt.
        Also returns the workflow JSON ready to POST to ComfyUI.
        """
        dim_key  = self._resolve_dimension_key(format, None)
        dim_info = CHANNEL_DIMENSIONS.get(dim_key, CHANNEL_DIMENSIONS["linkedin"])

        positive, negative = self._groq_expand(brief, style, dim_info, format)

        workflow = self._build_workflow(positive, negative, dim_info)

        return {
            "positive_prompt":  positive,
            "negative_prompt":  negative,
            "workflow_json":    workflow,
            "dimensions":       dim_info,
            "channel":          format,
        }

    # ═══════════════════════════════════════════════════════
    #  MAIN GENERATE (backend-initiated, uses ngrok directly)
    # ═══════════════════════════════════════════════════════

    def generate(
        self,
        campaign_id: str,
        prompt: str,
        style: str = "photorealistic",
        channel: str = "linkedin",
        dimensions: str = None,
    ) -> dict:
        """
        Full pipeline: expand prompt → submit to ComfyUI → poll → fetch image → store.
        Called from POST /agents/crea/generate.
        Returns asset metadata dict.
        """
        dim_key  = self._resolve_dimension_key(channel, dimensions)
        dim_info = CHANNEL_DIMENSIONS.get(dim_key, CHANNEL_DIMENSIONS["linkedin"])

        # 1. Expand prompt
        positive, negative = self._groq_expand(prompt, style, dim_info, channel)

        # 2. Build workflow JSON
        workflow = self._build_workflow(positive, negative, dim_info)

        # 3. Submit to ComfyUI
        prompt_id = self._comfyui_submit(workflow)
        if not prompt_id:
            return {"success": False, "error": "ComfyUI submission failed — is it running?"}

        # 4. Poll until done
        filename = self._comfyui_poll(prompt_id)
        if not filename:
            return {"success": False, "error": "ComfyUI generation timed out or failed"}

        # 5. Build image URL (ngrok viewable)
        image_url = f"{COMFYUI_NGROK}/view?filename={filename}"

        # 6. QA gate — rule-based: generation success = pass
        qa_passed = True
        qa_score  = {"overall_pass": True, "method": "rule_based", "reason": "Generation completed successfully"}

        # 7. Store to crea_assets
        asset_id = self._store_asset(
            campaign_id=campaign_id,
            image_url=image_url,
            positive=positive,
            negative=negative,
            channel=channel,
            dim_info=dim_info,
            style=style,
            qa_score=qa_score,
            qa_passed=qa_passed,
        )

        logger.info(f"CREA: asset stored — id={asset_id} campaign={campaign_id}")

        return {
            "success":          True,
            "asset_id":         asset_id,
            "image_url":        image_url,
            "filename":         filename,
            "prompt_id":        prompt_id,
            "positive_prompt":  positive,
            "negative_prompt":  negative,
            "qa_passed":        qa_passed,
            "qa_score":         qa_score,
            "channel":          channel,
            "dimensions":       f"{dim_info['w']}x{dim_info['h']}",
        }

    # ═══════════════════════════════════════════════════════
    #  SAVE ASSET (called from dashboard after browser-side generation)
    # ═══════════════════════════════════════════════════════

    def save_asset(
        self,
        campaign_id: str,
        image_b64: str = None,
        image_url: str = None,
        prompt: str = "",
        negative_prompt: str = "",
        style: str = "photorealistic",
        channel: str = "linkedin",
    ) -> dict:
        """
        Store a completed asset. Called by POST /agents/crea/save-asset.
        Browser sends image_url (ngrok /view URL) or base64.
        """
        dim_key  = self._resolve_dimension_key(channel, None)
        dim_info = CHANNEL_DIMENSIONS.get(dim_key, CHANNEL_DIMENSIONS["linkedin"])

        qa_score = {"overall_pass": True, "method": "rule_based", "reason": "User confirmed generation"}

        asset_id = self._store_asset(
            campaign_id=campaign_id,
            image_url=image_url,
            image_b64=image_b64,
            positive=prompt,
            negative=negative_prompt,
            channel=channel,
            dim_info=dim_info,
            style=style,
            qa_score=qa_score,
            qa_passed=True,
        )

        return {"success": True, "asset_id": asset_id}

    # ═══════════════════════════════════════════════════════
    #  GROQ — PROMPT ENGINEERING
    # ═══════════════════════════════════════════════════════

    def _groq_expand(self, brief: str, style: str, dim_info: dict, channel: str) -> tuple[str, str]:
        """Call Groq to expand a brief into a JuggernautXL prompt."""
        user = (
            f"Brief: {brief}\n"
            f"Style: {style}\n"
            f"Channel: {channel} ({dim_info['w']}x{dim_info['h']}px, {dim_info['ratio']} ratio)\n"
            f"Notes: {dim_info['notes']}\n\n"
            "Expand this into a JuggernautXL image generation prompt now."
        )

        try:
            with httpx.Client(timeout=20) as client:
                res = client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {GROQ_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model":       GROQ_MODEL,
                        "messages":    [
                            {"role": "system", "content": PROMPT_ENGINEER_SYSTEM},
                            {"role": "user",   "content": user},
                        ],
                        "max_tokens":  450,
                        "temperature": 0.75,
                    }
                )
                res.raise_for_status()
                raw   = res.json()["choices"][0]["message"]["content"].strip()
                # Strip any accidental markdown fences
                clean = raw.lstrip("```json").lstrip("```").rstrip("```").strip()
                data  = json.loads(clean)
                return data["positive_prompt"], data["negative_prompt"]

        except Exception as e:
            logger.warning(f"Groq prompt expansion failed: {e}. Using fallback.")
            fallback_pos = (
                f"Ultra-realistic professional advertising photograph, {brief}, "
                f"{style} style, cinematic lighting, sharp focus, "
                f"{dim_info['ratio']} aspect ratio, commercial quality, 8k"
            )
            fallback_neg = (
                "centered subject, stock photo, generic background, AI artifacts, "
                "watermark, text overlay, oversaturated, blurry, deformed, bad anatomy"
            )
            return fallback_pos, fallback_neg

    # ═══════════════════════════════════════════════════════
    #  COMFYUI — WORKFLOW + SUBMISSION + POLLING
    # ═══════════════════════════════════════════════════════

    def _build_workflow(self, positive: str, negative: str, dim_info: dict) -> dict:
        """
        Build the ComfyUI API workflow JSON for JuggernautXL Ragnarok.
        Node IDs match standard ComfyUI SDXL workflow conventions.
        """
        return {
            "4": {
                "class_type": "CheckpointLoaderSimple",
                "inputs": {
                    "ckpt_name": "juggernautXL_ragnarokBy.safetensors"
                }
            },
            "6": {
                "class_type": "CLIPTextEncode",
                "inputs": {
                    "text": positive,
                    "clip": ["4", 1]
                }
            },
            "7": {
                "class_type": "CLIPTextEncode",
                "inputs": {
                    "text": negative,
                    "clip": ["4", 1]
                }
            },
            "5": {
                "class_type": "EmptyLatentImage",
                "inputs": {
                    "width":       dim_info["w"],
                    "height":      dim_info["h"],
                    "batch_size":  1
                }
            },
            "3": {
                "class_type": "KSampler",
                "inputs": {
                    "model":            ["4", 0],
                    "positive":         ["6", 0],
                    "negative":         ["7", 0],
                    "latent_image":     ["5", 0],
                    "seed":             int(time.time()),
                    "steps":            30,
                    "cfg":              7.0,
                    "sampler_name":     "euler_ancestral",
                    "scheduler":        "karras",
                    "denoise":          1.0
                }
            },
            "8": {
                "class_type": "VAEDecode",
                "inputs": {
                    "samples": ["3", 0],
                    "vae":     ["4", 2]
                }
            },
            "9": {
                "class_type": "SaveImage",
                "inputs": {
                    "images":       ["8", 0],
                    "filename_prefix": f"ARIA_CREA_{int(time.time())}"
                }
            }
        }

    def _comfyui_submit(self, workflow: dict) -> str | None:
        """POST workflow to ComfyUI /prompt, return prompt_id."""
        try:
            with httpx.Client(timeout=15) as client:
                res = client.post(
                    f"{COMFYUI_NGROK}/prompt",
                    json={"prompt": workflow},
                    headers={"Content-Type": "application/json"},
                )
                res.raise_for_status()
                data = res.json()
                prompt_id = data.get("prompt_id")
                logger.info(f"CREA: ComfyUI job submitted — prompt_id={prompt_id}")
                return prompt_id
        except Exception as e:
            logger.error(f"CREA: ComfyUI submission failed: {e}")
            return None

    def _comfyui_poll(self, prompt_id: str) -> str | None:
        """
        Poll GET /history/{prompt_id} every POLL_INTERVAL seconds.
        Returns filename when done, None on timeout.
        """
        deadline = time.time() + POLL_TIMEOUT
        logger.info(f"CREA: polling ComfyUI for prompt_id={prompt_id}")

        while time.time() < deadline:
            try:
                with httpx.Client(timeout=10) as client:
                    res = client.get(f"{COMFYUI_NGROK}/history/{prompt_id}")
                    res.raise_for_status()
                    history = res.json()

                if prompt_id in history:
                    outputs = history[prompt_id].get("outputs", {})
                    # Node 9 is SaveImage
                    node_9 = outputs.get("9", {})
                    images = node_9.get("images", [])
                    if images:
                        filename = images[0].get("filename")
                        logger.info(f"CREA: ComfyUI done — filename={filename}")
                        return filename

            except Exception as e:
                logger.warning(f"CREA: poll error: {e}")

            time.sleep(POLL_INTERVAL)

        logger.error(f"CREA: ComfyUI timed out after {POLL_TIMEOUT}s for prompt_id={prompt_id}")
        return None

    # ═══════════════════════════════════════════════════════
    #  SUPABASE STORAGE
    # ═══════════════════════════════════════════════════════

    def _store_asset(
        self,
        campaign_id: str,
        image_url: str = None,
        image_b64: str = None,
        positive: str = "",
        negative: str = "",
        channel: str = "linkedin",
        dim_info: dict = None,
        style: str = "photorealistic",
        qa_score: dict = None,
        qa_passed: bool = True,
    ) -> str | None:
        dim_info = dim_info or CHANNEL_DIMENSIONS["linkedin"]
        row = supabase_insert("crea_assets", {
            "campaign_id":     campaign_id,
            "client_id":       self.client_id,
            "image_url":       image_url,
            "image_data":      image_b64,
            "prompt_used":     positive,
            "negative_prompt": negative,
            "style":           style,
            "channel":         channel,
            "dimensions":      f"{dim_info['w']}x{dim_info['h']}",
            "qa_score":        json.dumps(qa_score or {}),
            "qa_passed":       qa_passed,
            "status":          "draft",
            "approved":        False,
            "generation_model":"juggernautXL_ragnarok",
            "created_at":      datetime.now(timezone.utc).isoformat(),
        })
        return row[0]["id"] if row else None

    # ═══════════════════════════════════════════════════════
    #  ASSET MANAGEMENT
    # ═══════════════════════════════════════════════════════

    def get_assets(self, campaign_id: str) -> list:
        filters = {"client_id": self.client_id}
        if campaign_id and campaign_id != "all":
            filters["campaign_id"] = campaign_id
        return supabase_select("crea_assets", filters=filters, limit=50)

    def approve_asset(self, asset_id: str):
        supabase_update("crea_assets", {"status": "approved", "approved": True}, filters={"id": asset_id})

    def reject_asset(self, asset_id: str, feedback: str = ""):
        supabase_update("crea_assets", {"status": "rejected", "approved": False, "feedback": feedback}, filters={"id": asset_id})

    # ═══════════════════════════════════════════════════════
    #  HELPERS
    # ═══════════════════════════════════════════════════════

    def _resolve_dimension_key(self, channel: str, dimensions: str | None) -> str:
        if dimensions:
            for key, val in CHANNEL_DIMENSIONS.items():
                if f"{val['w']}x{val['h']}" == dimensions:
                    return key
        mapping = {
            "instagram":  "instagram_post",
            "story":      "instagram_story",
            "linkedin":   "linkedin",
            "facebook":   "facebook",
            "twitter":    "twitter",
            "banner":     "banner_rectangle",
        }
        return mapping.get(channel.lower(), "linkedin")

    def run(self):
        """Scheduled run — CREA is manual-only. No-op."""
        logger.info("CREAAgent.run called — CREA is manual-only, skipping.")
