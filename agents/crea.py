"""
agents/crea.py
ARIA™ — CREA™ Creative Agent
ComfyUI (local via ngrok, Flux.1 Dev) + Groq prompt engineering.
No FAL.ai. No Anthropic API. Groq handles prompt expansion.
QA gate is rule-based (generation success = pass).
Expand + generate now happen in one call (generate()) so the frontend gets
back a real image_url instead of just an expanded prompt.
OUP International Ltd, 2026
"""

import os
import json
import time
import base64
import logging
import httpx
from datetime import datetime, timezone
from core.base_agent import BaseAgent
from core.memory import supabase_select, supabase_insert, supabase_update

logger = logging.getLogger("aria.crea")

GROQ_API_KEY   = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL     = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
COMFYUI_NGROK  = os.environ.get("COMFYUI_NGROK", "http://127.0.0.1:8188")

# Flux Dev model filenames — must match what's actually in ComfyUI's models
# folders on the gmtek box. Confirmed with Luke: flux1-dev.safetensors,
# t5xxl_fp16 + clip_l text encoders. VAE filename (ae.safetensors) is the
# standard BFL release name — verify this matches if generation fails.
FLUX_UNET_NAME   = os.environ.get("FLUX_UNET_NAME", "flux1-dev.safetensors")
FLUX_CLIP_T5     = os.environ.get("FLUX_CLIP_T5", "t5xxl_fp16.safetensors")
FLUX_CLIP_L      = os.environ.get("FLUX_CLIP_L", "clip_l.safetensors")
FLUX_VAE_NAME    = os.environ.get("FLUX_VAE_NAME", "ae.safetensors")

POLL_INTERVAL  = 3
POLL_TIMEOUT   = 180

CHANNEL_DIMENSIONS = {
    "instagram_post":     {"w": 1024, "h": 1024, "ratio": "1:1",    "notes": "Primary social format"},
    "instagram_story":    {"w": 768,  "h": 1344, "ratio": "9:16",   "notes": "Keep text in centre 60%"},
    "linkedin":           {"w": 1216, "h": 640,  "ratio": "1.91:1", "notes": "Professional tone"},
    "facebook":           {"w": 1216, "h": 640,  "ratio": "1.91:1", "notes": "Same as LinkedIn"},
    "twitter":            {"w": 1344, "h": 768,  "ratio": "16:9",   "notes": "Bold, high contrast"},
    "banner_leaderboard": {"w": 1344, "h": 768,  "ratio": "8:1",    "notes": "Minimal text"},
    "banner_rectangle":   {"w": 768,  "h": 640,  "ratio": "6:5",    "notes": "Most common display"},
}

PROMPT_ENGINEER_SYSTEM = """You are a specialist AI image prompt engineer for advertising.
Given a campaign brief, construct a Flux.1 Dev-optimised image generation prompt.

Rules:
- Specify art style, lighting, composition, and colour palette derived from brand params
- Include target audience context in visual language
- AVOID generic AI aesthetics: no centred lone subject on plain background, no stock-photo look
- Include specific visual storytelling details, textures, environments
- Optimise for photorealism — Flux.1 Dev excels at this
- Keep positive prompt under 180 words
- Flux does not use a real negative prompt (no classifier-free negative guidance at cfg 1.0) —
  still return one for logging/QA purposes, phrased as things to avoid in the composition

Return JSON only — no markdown, no backticks, no preamble:
{
  "positive_prompt": "...",
  "negative_prompt": "centered subject, stock photo, generic background, AI artifacts, watermark, text overlay, oversaturated, blurry, deformed, bad anatomy, ugly, duplicate"
}"""


class CREAAgent(BaseAgent):
    def __init__(self, client_id: str = "aria_internal"):
        super().__init__(name="crea", client_id=client_id)

    def health(self) -> dict:
        try:
            with httpx.Client(timeout=8) as client:
                res = client.get(f"{COMFYUI_NGROK}/system_stats")
                res.raise_for_status()
                stats = res.json()
                return {"status": "online", "ngrok_url": COMFYUI_NGROK, "comfyui": stats}
        except Exception as e:
            return {
                "status": "offline",
                "ngrok_url": COMFYUI_NGROK,
                "error": str(e),
                "hint": "Start ComfyUI: cd C:\\AI\\ComfyUI && python main.py, then ngrok http 8188 --url=setsuko-tabernacular-necole.ngrok-free.dev",
            }

    def expand_prompt(self, brief: str, style: str = "photorealistic", format: str = "linkedin") -> dict:
        """Prompt-engineering preview only — does NOT generate an image.
        Kept for backward compatibility; the dashboard's Step 1→2 flow now
        calls generate() directly so it gets a real image_url back."""
        dim_key  = self._resolve_dimension_key(format, None)
        dim_info = CHANNEL_DIMENSIONS.get(dim_key, CHANNEL_DIMENSIONS["linkedin"])
        positive, negative = self._groq_expand(brief, style, dim_info, format)
        workflow = self._build_workflow(positive, negative, dim_info)
        return {
            "positive_prompt": positive,
            "negative_prompt": negative,
            "workflow_json":   workflow,
            "dimensions":      dim_info,
            "channel":         format,
        }

    def generate(self, campaign_id: str, prompt: str, style: str = "photorealistic", channel: str = "linkedin",
                 dimensions: str = None, base_image_b64: str = None, denoise: float = 0.75) -> dict:
        """Expand the brief via Groq and generate via ComfyUI/Flux in one call.
        If base_image_b64 is provided, runs as image-to-image at the given
        denoise strength (0.0 = ignore prompt entirely, 1.0 = ignore base image
        entirely) so a client's own reference photo can be used as a base."""
        dim_key  = self._resolve_dimension_key(channel, dimensions)
        dim_info = CHANNEL_DIMENSIONS.get(dim_key, CHANNEL_DIMENSIONS["linkedin"])
        positive, negative = self._groq_expand(prompt, style, dim_info, channel)

        base_filename = None
        if base_image_b64:
            base_filename = self._comfyui_upload_image(base_image_b64)
            if not base_filename:
                return {"success": False, "error": "Base image upload to ComfyUI failed"}

        workflow = self._build_workflow(
            positive, negative, dim_info,
            base_image_filename=base_filename,
            denoise=denoise if base_filename else 1.0,
        )
        prompt_id = self._comfyui_submit(workflow)
        if not prompt_id:
            return {"success": False, "error": "ComfyUI submission failed — is it running?"}
        filename = self._comfyui_poll(prompt_id)
        if not filename:
            return {"success": False, "error": "ComfyUI generation timed out or failed"}
        image_url = f"{COMFYUI_NGROK}/view?filename={filename}"
        qa_passed = True
        qa_score  = {"overall_pass": True, "method": "rule_based", "reason": "Generation completed successfully"}
        asset_id  = self._store_asset(
            campaign_id=campaign_id, image_url=image_url, positive=positive,
            negative=negative, channel=channel, dim_info=dim_info,
            style=style, qa_score=qa_score, qa_passed=qa_passed,
        )
        logger.info(f"CREA: asset stored — id={asset_id} campaign={campaign_id}")
        return {
            "success": True, "asset_id": asset_id, "image_url": image_url,
            "filename": filename, "prompt_id": prompt_id,
            "positive_prompt": positive, "negative_prompt": negative,
            "qa_passed": qa_passed, "qa_score": qa_score,
            "channel": channel, "dimensions": f"{dim_info['w']}x{dim_info['h']}",
            "used_base_image": bool(base_filename),
        }

    def save_asset(self, campaign_id: str, image_b64: str = None, image_url: str = None,
                   prompt: str = "", negative_prompt: str = "", style: str = "photorealistic", channel: str = "linkedin") -> dict:
        dim_key  = self._resolve_dimension_key(channel, None)
        dim_info = CHANNEL_DIMENSIONS.get(dim_key, CHANNEL_DIMENSIONS["linkedin"])
        qa_score = {"overall_pass": True, "method": "rule_based", "reason": "User confirmed generation"}
        asset_id = self._store_asset(
            campaign_id=campaign_id, image_url=image_url, image_b64=image_b64,
            positive=prompt, negative=negative_prompt, channel=channel,
            dim_info=dim_info, style=style, qa_score=qa_score, qa_passed=True,
        )
        return {"success": True, "asset_id": asset_id}

    def _groq_expand(self, brief: str, style: str, dim_info: dict, channel: str) -> tuple[str, str]:
        user = (
            f"Brief: {brief}\nStyle: {style}\n"
            f"Channel: {channel} ({dim_info['w']}x{dim_info['h']}px, {dim_info['ratio']} ratio)\n"
            f"Notes: {dim_info['notes']}\n\nExpand this into a Flux.1 Dev image generation prompt now."
        )
        try:
            with httpx.Client(timeout=20) as client:
                res = client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                    json={"model": GROQ_MODEL, "messages": [
                        {"role": "system", "content": PROMPT_ENGINEER_SYSTEM},
                        {"role": "user",   "content": user},
                    ], "max_tokens": 450, "temperature": 0.75}
                )
                res.raise_for_status()
                raw   = res.json()["choices"][0]["message"]["content"].strip()
                clean = raw.lstrip("```json").lstrip("```").rstrip("```").strip()
                data  = json.loads(clean)
                return data["positive_prompt"], data["negative_prompt"]
        except Exception as e:
            logger.warning(f"Groq prompt expansion failed: {e}. Using fallback.")
            return (
                f"Ultra-realistic professional advertising photograph, {brief}, {style} style, cinematic lighting, sharp focus, {dim_info['ratio']} aspect ratio, commercial quality, 8k",
                "centered subject, stock photo, generic background, AI artifacts, watermark, text overlay, oversaturated, blurry, deformed, bad anatomy"
            )

    def _build_workflow(self, positive: str, negative: str, dim_info: dict,
                         base_image_filename: str = None, denoise: float = 1.0) -> dict:
        """Flux.1 Dev graph. Flux uses a UNETLoader + DualCLIPLoader + VAELoader
        trio rather than a single SDXL-style checkpoint, and runs at cfg=1.0
        with guidance handled by a separate FluxGuidance node — real negative
        prompting isn't a thing here, so the negative slot is a zeroed-out
        conditioning to satisfy KSampler's required input.
        When base_image_filename is set, swaps EmptyLatentImage for
        LoadImage → VAEEncode (img2img) at the given denoise strength."""
        workflow = {
            "1": {"class_type": "UNETLoader", "inputs": {"unet_name": FLUX_UNET_NAME, "weight_dtype": "default"}},
            "2": {"class_type": "DualCLIPLoader", "inputs": {"clip_name1": FLUX_CLIP_T5, "clip_name2": FLUX_CLIP_L, "type": "flux"}},
            "3": {"class_type": "VAELoader", "inputs": {"vae_name": FLUX_VAE_NAME}},
            "4": {"class_type": "CLIPTextEncode", "inputs": {"text": positive, "clip": ["2", 0]}},
            "5": {"class_type": "FluxGuidance", "inputs": {"conditioning": ["4", 0], "guidance": 3.5}},
            "6": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["4", 0]}},
            "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
            "10": {"class_type": "SaveImage", "inputs": {"images": ["9", 0], "filename_prefix": f"LUIN_CREA_{int(time.time())}"}},
        }

        if base_image_filename:
            workflow["11"] = {"class_type": "LoadImage", "inputs": {"image": base_image_filename}}
            workflow["7"]  = {"class_type": "VAEEncode", "inputs": {"pixels": ["11", 0], "vae": ["3", 0]}}
        else:
            workflow["7"] = {"class_type": "EmptyLatentImage", "inputs": {"width": dim_info["w"], "height": dim_info["h"], "batch_size": 1}}
            denoise = 1.0

        workflow["8"] = {"class_type": "KSampler", "inputs": {
            "model": ["1", 0], "positive": ["5", 0], "negative": ["6", 0], "latent_image": ["7", 0],
            "seed": int(time.time()), "steps": 20, "cfg": 1.0,
            "sampler_name": "euler", "scheduler": "simple", "denoise": denoise,
        }}

        return workflow

    def _comfyui_upload_image(self, image_b64: str) -> str | None:
        """Uploads a base64 image to ComfyUI's /upload/image endpoint so it can
        be referenced by filename in a LoadImage node. Strips a data URL prefix
        (e.g. 'data:image/png;base64,') if present."""
        try:
            raw_b64 = image_b64.split(",")[-1] if "," in image_b64 else image_b64
            image_bytes = base64.b64decode(raw_b64)
            files = {"image": (f"crea_upload_{int(time.time())}.png", image_bytes, "image/png")}
            with httpx.Client(timeout=30) as client:
                res = client.post(f"{COMFYUI_NGROK}/upload/image", files=files, data={"overwrite": "true"})
                res.raise_for_status()
                name = res.json().get("name")
                logger.info(f"CREA: base image uploaded to ComfyUI — name={name}")
                return name
        except Exception as e:
            logger.error(f"CREA: image upload to ComfyUI failed: {e}")
            return None

    def _comfyui_submit(self, workflow: dict) -> str | None:
        try:
            with httpx.Client(timeout=15) as client:
                res = client.post(f"{COMFYUI_NGROK}/prompt", json={"prompt": workflow}, headers={"Content-Type": "application/json"})
                res.raise_for_status()
                prompt_id = res.json().get("prompt_id")
                logger.info(f"CREA: ComfyUI job submitted — prompt_id={prompt_id}")
                return prompt_id
        except Exception as e:
            logger.error(f"CREA: ComfyUI submission failed: {e}")
            return None

    def _comfyui_poll(self, prompt_id: str) -> str | None:
        deadline = time.time() + POLL_TIMEOUT
        logger.info(f"CREA: polling ComfyUI for prompt_id={prompt_id}")
        while time.time() < deadline:
            try:
                with httpx.Client(timeout=10) as client:
                    res = client.get(f"{COMFYUI_NGROK}/history/{prompt_id}")
                    res.raise_for_status()
                    history = res.json()
                if prompt_id in history:
                    images = history[prompt_id].get("outputs", {}).get("10", {}).get("images", [])
                    if images:
                        filename = images[0].get("filename")
                        logger.info(f"CREA: ComfyUI done — filename={filename}")
                        return filename
            except Exception as e:
                logger.warning(f"CREA: poll error: {e}")
            time.sleep(POLL_INTERVAL)
        logger.error(f"CREA: ComfyUI timed out after {POLL_TIMEOUT}s for prompt_id={prompt_id}")
        return None

    def _store_asset(self, campaign_id: str, image_url: str = None, image_b64: str = None,
                     positive: str = "", negative: str = "", channel: str = "linkedin",
                     dim_info: dict = None, style: str = "photorealistic",
                     qa_score: dict = None, qa_passed: bool = True) -> str | None:
        dim_info = dim_info or CHANNEL_DIMENSIONS["linkedin"]
        row = supabase_insert("crea_assets", {
            "campaign_id": campaign_id, "client_id": self.client_id,
            "image_url": image_url, "image_data": image_b64,
            "prompt_used": positive, "negative_prompt": negative,
            "style": style, "channel": channel,
            "dimensions": f"{dim_info['w']}x{dim_info['h']}",
            "qa_score": json.dumps(qa_score or {}), "qa_passed": qa_passed,
            "status": "draft", "approved": False,
            "generation_model": "flux1-dev",
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        return row["id"] if row else None

    def get_assets(self, campaign_id: str) -> list:
        filters = {"client_id": self.client_id}
        if campaign_id and campaign_id != "all":
            filters["campaign_id"] = campaign_id
        return supabase_select("crea_assets", filters=filters, limit=50)

    def approve_asset(self, asset_id: str):
        supabase_update("crea_assets", row_id=asset_id, data={"status": "approved", "approved": True})

    def reject_asset(self, asset_id: str, feedback: str = ""):
        supabase_update("crea_assets", row_id=asset_id, data={"status": "rejected", "approved": False, "feedback": feedback})

    def _resolve_dimension_key(self, channel: str, dimensions: str | None) -> str:
        if dimensions:
            for key, val in CHANNEL_DIMENSIONS.items():
                if f"{val['w']}x{val['h']}" == dimensions:
                    return key
        mapping = {
            "instagram": "instagram_post", "story": "instagram_story",
            "linkedin": "linkedin", "facebook": "facebook",
            "twitter": "twitter", "banner": "banner_rectangle",
        }
        return mapping.get(channel.lower(), "linkedin")

    def run(self):
        logger.info("CREAAgent.run called — CREA is manual-only, skipping.")
