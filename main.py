"""
main.py
ARIA™ — FastAPI Orchestrator v5
Fixed: scheduler persistence, modular client modules, CREA→post pipeline, blog auto-publish.
OUP International Ltd, 2026
"""

import os
import json
import logging
import asyncio
import random
import urllib.parse
from contextlib import asynccontextmanager
from datetime import date, timedelta
from functools import lru_cache

from dotenv import load_dotenv
load_dotenv()

from fastapi import (
    FastAPI, HTTPException, Header, Depends,
    UploadFile, File, Form, Request,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from typing import Optional, List
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
import jwt
import base64
import httpx

from core.memory import supabase_select, supabase_insert, supabase_update

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("aria.main")

# ═══════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════

JWT_SECRET   = os.environ.get("JWT_SECRET", "aria-jwt-secret-change-in-production")
JWT_ALGO     = "HS256"
CLIENT_ID    = os.environ.get("CLIENT_ID", "aria_internal")
ARIA_API_KEY = os.environ.get("ARIA_API_KEY", "")

GOOGLE_CLIENT_ID       = os.environ.get("GOOGLE_CLIENT_ID", "")
LINKEDIN_CLIENT_ID     = os.environ.get("LINKEDIN_CLIENT_ID", "776qz574t0s657")
LINKEDIN_CLIENT_SECRET = os.environ.get("LINKEDIN_CLIENT_SECRET", "")
LINKEDIN_REDIRECT_URI  = "https://aria-agents.onrender.com/auth/linkedin/callback"
FACEBOOK_APP_ID        = os.environ.get("FACEBOOK_APP_ID", "2184784782315468")
FACEBOOK_APP_SECRET    = os.environ.get("FACEBOOK_APP_SECRET", "")
FRONTEND_URL           = os.environ.get("FRONTEND_URL", "https://aria.ourunitedplanet.org")

DEFAULT_MODULES = ["jamie", "research", "writer", "linkedin", "reporting", "crea"]

BLOG_TOPICS = {
    "monday": [
        "The future of B2B marketing automation and what it means for growth teams",
        "How AI agents are changing the economics of content marketing",
        "Why most marketing stacks fail at scale — and how to fix it",
        "The case for autonomous marketing: less headcount, more output",
        "How to build a marketing system that works while you sleep",
    ],
    "thursday": [
        "LinkedIn in 2026: what the algorithm rewards and how to use it",
        "Intent data vs cold outreach: why the math changed",
        "The approval queue as a product feature — not a bottleneck",
        "How to write B2B content that converts without being annoying",
        "Multi-channel AI marketing: how to orchestrate without chaos",
    ],
}

# ═══════════════════════════════════════════════════════════════
#  AUTH HELPERS
# ═══════════════════════════════════════════════════════════════

def create_client_token(client_id: str) -> str:
    return jwt.encode({"client_id": client_id, "iss": "aria"}, JWT_SECRET, algorithm=JWT_ALGO)

def decode_client_token(token: str) -> str:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
        return payload["client_id"]
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

def verify_api_key(x_api_key: str = Header(default="")):
    if ARIA_API_KEY and x_api_key != ARIA_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return x_api_key

def get_client_id(
    x_client_token: str = Header(default=""),
    x_api_key: str = Header(default=""),
) -> str:
    if x_client_token:
        return decode_client_token(x_client_token)
    if ARIA_API_KEY and x_api_key == ARIA_API_KEY:
        return CLIENT_ID
    if not ARIA_API_KEY:
        return CLIENT_ID
    raise HTTPException(status_code=401, detail="Authentication required")

# ═══════════════════════════════════════════════════════════════
#  AGENT FACTORY
# ═══════════════════════════════════════════════════════════════

def _make_agent(name: str, client_id: str):
    if name == "jamie":
        from agents.jamie import JAMIEHunter
        return JAMIEHunter()
    elif name == "research":
        from agents.research import ResearchAgent
        return ResearchAgent(client_id=client_id)
    elif name == "writer":
        from agents.writer import WriterAgent
        return WriterAgent(client_id=client_id)
    elif name == "reporting":
        from agents.reporting import ReportingAgent
        return ReportingAgent(client_id=client_id)
    elif name == "linkedin":
        from agents.linkedin import LinkedInAgent
        return LinkedInAgent(client_id=client_id)
    elif name == "crea":
        from agents.crea import CREAAgent
        return CREAAgent(client_id=client_id)
    return None

def get_client_modules(client_id: str) -> list[str]:
    try:
        rows = supabase_select("clients", filters={"client_id": client_id}, limit=1)
        if rows and rows[0].get("modules"):
            modules = rows[0]["modules"]
            if isinstance(modules, str):
                modules = json.loads(modules)
            return modules
    except Exception:
        pass
    return DEFAULT_MODULES

def get_agents_for_client(client_id: str) -> dict:
    modules = get_client_modules(client_id)
    agents = {}
    for name in modules:
        agent = _make_agent(name, client_id)
        if agent:
            agents[name] = agent
    return agents

# ═══════════════════════════════════════════════════════════════
#  SCHEDULER HELPERS
# ═══════════════════════════════════════════════════════════════

scheduler = AsyncIOScheduler()

def _run_all_clients(agent_name: str):
    try:
        clients = supabase_select("clients", filters={"active": True}, limit=50)
        if not clients:
            return
        for client in clients:
            cid = client["client_id"]
            modules = get_client_modules(cid)
            if agent_name not in modules:
                continue
            try:
                agent = _make_agent(agent_name, cid)
                if agent:
                    logger.info(f"Scheduler: {agent_name} → {cid}")
                    agent.execute_cycle()
            except Exception as e:
                logger.error(f"Scheduler: {agent_name} failed for {cid}: {e}")
    except Exception as e:
        logger.error(f"Scheduler: _run_all_clients({agent_name}) failed: {e}")

def _run_jamie():
    try:
        from agents.jamie import JAMIEHunter
        JAMIEHunter().execute_cycle()
    except Exception as e:
        logger.error(f"Scheduler: JAMIE failed: {e}")

def _generate_blog_post(publish_day: str):
    """
    Generate a high-quality blog post via Writer.
    Lands in approval queue — approved post publishes to blog the next day.
    """
    topic_list   = BLOG_TOPICS.get(publish_day, BLOG_TOPICS["monday"])
    topic        = random.choice(topic_list)
    publish_date = (date.today() + timedelta(days=1)).isoformat()
    try:
        clients = supabase_select("clients", filters={"active": True}, limit=50)
        for client in clients:
            cid     = client["client_id"]
            modules = get_client_modules(cid)
            if "writer" not in modules:
                continue
            row = supabase_insert("content_requests", {
                "client_id":    cid,
                "topic":        topic,
                "format":       "blog",
                "platform":     "blog",
                "vibe":         "authority",
                "requested_by": "scheduler",
                "schedule_for": publish_date,
                "status":       "pending",
            })
            request_data = {
                **(row or {}),
                "topic":        topic,
                "format":       "blog",
                "platform":     "blog",
                "vibe":         "authority",
                "requested_by": "scheduler",
                "schedule_for": publish_date,
                "id":           (row or {}).get("id"),
                "client_id":    cid,
            }
            from agents.writer import WriterAgent
            WriterAgent(client_id=cid)._write_on_demand(request_data)
            logger.info(f"Blog generated for {publish_day} [{cid}]: {topic[:60]}")
    except Exception as e:
        logger.error(f"Blog generation failed [{publish_day}]: {e}")

# ═══════════════════════════════════════════════════════════════
#  APP LIFECYCLE
# ═══════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"ARIA™ v5 starting — default client: {CLIENT_ID}")

    scheduler.add_job(_run_jamie, IntervalTrigger(minutes=30), id="jamie", replace_existing=True, misfire_grace_time=300)
    scheduler.add_job(lambda: _run_all_clients("research"), CronTrigger(hour=6, minute=0), id="research", replace_existing=True, misfire_grace_time=600)
    scheduler.add_job(lambda: _run_all_clients("writer"),   CronTrigger(hour=7, minute=0), id="writer",   replace_existing=True, misfire_grace_time=600)
    scheduler.add_job(lambda: _run_all_clients("linkedin"), CronTrigger(hour=12, minute=0), id="linkedin", replace_existing=True, misfire_grace_time=600)
    scheduler.add_job(lambda: _run_all_clients("reporting"), CronTrigger(day_of_week="mon", hour=7, minute=0), id="reporting", replace_existing=True, misfire_grace_time=600)

    # Blog: Sunday 8am → ready for Monday · Wednesday 8am → ready for Thursday
    scheduler.add_job(lambda: _generate_blog_post("monday"),   CronTrigger(day_of_week="sun", hour=8, minute=0), id="blog_monday",   replace_existing=True, misfire_grace_time=600)
    scheduler.add_job(lambda: _generate_blog_post("thursday"), CronTrigger(day_of_week="wed", hour=8, minute=0), id="blog_thursday", replace_existing=True, misfire_grace_time=600)

    scheduler.start()
    logger.info("Scheduler live: JAMIE 30m · Research 6am · Writer 7am · LinkedIn 12pm · Reporting Mon 7am · Blog Sun+Wed 8am")
    yield
    scheduler.shutdown()

app = FastAPI(title="ARIA™ Orchestrator", description="Autonomous Marketing Agency — OUP International Ltd", version="5.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ═══════════════════════════════════════════════════════════════
#  REQUEST MODELS
# ═══════════════════════════════════════════════════════════════

class CEOInstruction(BaseModel):
    agent: str
    instruction: str
    priority: str = "normal"

class ContentRequest(BaseModel):
    topic: str
    format: str = "linkedin_post"
    platform: str = "linkedin"
    vibe: Optional[str] = ""
    requested_by: str = "client"
    schedule_for: Optional[str] = None

class ContentApproval(BaseModel):
    content_id: str
    action: str
    ceo_note: Optional[str] = ""
    post_immediately: bool = False
    schedule_for: Optional[str] = None

class ContentEdit(BaseModel):
    content_id: str
    new_draft: str
    edited_by: str = "ceo"

class CEOOverride(BaseModel):
    agent: str
    instruction: str
    trigger_immediately: bool = True

class ClientLogin(BaseModel):
    access_code: str

class ClientOnboard(BaseModel):
    client_id: str
    name: str
    access_code: str
    plan: str = "growth"
    modules: Optional[List[str]] = None
    brand_tone: str = ""
    target_audience: str = ""
    content_goals: str = ""
    avoid: str = ""
    linkedin_style: str = ""
    contact_name: str = ""
    contact_email: str = ""
    linkedin_company_id: str = ""
    linkedin_make_webhook: str = ""
    monthly_fee: str = ""

class BrandProfileUpdate(BaseModel):
    tone: Optional[str] = None
    target_audience: Optional[str] = None
    content_goals: Optional[str] = None
    avoid: Optional[str] = None
    linkedin_style: Optional[str] = None
    email_style: Optional[str] = None
    blog_style: Optional[str] = None
    research_topics: Optional[str] = None
    facebook_style: Optional[str] = None

class FacebookRequest(BaseModel):
    topic: Optional[str] = None
    vibe: Optional[str] = None

class BlogRequest(BaseModel):
    topic: Optional[str] = None
    word_count: Optional[int] = 1500
    vibe: Optional[str] = None

class CREAGenerateRequest(BaseModel):
    campaign_id: str
    channel: str = "linkedin"
    dimensions: Optional[str] = None
    quantity: int = 1
    override_prompt: Optional[str] = None
    base_image_b64: Optional[str] = None
    denoise: Optional[float] = None

class CREAExpandRequest(BaseModel):
    brief: str
    style: str = "photorealistic"
    format: str = "linkedin"

class CREASaveAssetRequest(BaseModel):
    campaign_id: str
    image_url: Optional[str] = None
    image_b64: Optional[str] = None
    prompt: str = ""
    negative_prompt: str = ""
    style: str = "photorealistic"
    channel: str = "linkedin"

class CREARejectRequest(BaseModel):
    feedback: str = ""

class ComfyUISubmitRequest(BaseModel):
    workflow: dict

class ChatMessage(BaseModel):
    message: str
    history: Optional[List[dict]] = []

class RouteCreate(BaseModel):
    platform: str
    account_type: str
    account_name: str
    webhook_url: str
    client_id: Optional[str] = None
    notes: Optional[str] = ""

class RouteToggle(BaseModel):
    active: bool

class IntakeBriefRequest(BaseModel):
    campaign_name: str
    raw_brief: str
    business_problem: Optional[str] = None
    target_audience_detail: Optional[str] = None
    success_30_days: Optional[str] = None
    success_90_days: Optional[str] = None
    already_tried: Optional[str] = None
    constraints: Optional[str] = None
    single_most_important_outcome: Optional[str] = None

class GoogleAuthPayload(BaseModel):
    credential: str

class FacebookAuthPayload(BaseModel):
    access_token: str
    user_id: str

class TasterRequest(BaseModel):
    business_name: str
    niche: str
    platform: str = "linkedin"

# ═══════════════════════════════════════════════════════════════
#  SSO HELPERS
# ═══════════════════════════════════════════════════════════════

def _find_or_create_sso_client(provider: str, provider_id: str, email: str, name: str) -> dict:
    field = f"{provider}_id"
    try:
        rows = supabase_select("clients", filters={field: provider_id}, limit=1)
        if rows:
            return rows[0]
    except Exception:
        pass
    if email:
        try:
            rows = supabase_select("clients", filters={"email": email, "active": True}, limit=1)
            if rows:
                supabase_update("clients", data={field: provider_id}, filters={"email": email})
                return rows[0]
        except Exception:
            pass
    client_id   = f"client_{provider_id[:8].lower()}"
    access_code = f"{provider[:2]}{provider_id[:6]}"
    row = supabase_insert("clients", {
        "client_id": client_id, "name": name or email or "New Client",
        "email": email, field: provider_id, "access_code": access_code,
        "plan": "trial", "modules": '["writer","research","linkedin","reporting","crea"]',
        "avatar": (name or "C")[0].upper(), "active": True,
    })
    supabase_insert("brand_profiles", {
        "client_id": client_id, "name": name or "My Brand",
        "tone": "Professional and authoritative", "target_audience": "B2B decision makers",
        "content_goals": "Build brand authority and generate leads",
        "avoid": "Clichés and generic marketing speak",
        "linkedin_style": "Short paragraphs, punchy opener, ends with question or CTA",
    })
    return row or {}

# ═══════════════════════════════════════════════════════════════
#  AUTH ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.post("/auth/login")
async def login(payload: ClientLogin):
    clients = supabase_select("clients", filters={"access_code": payload.access_code, "active": True}, limit=1)
    if not clients:
        raise HTTPException(status_code=401, detail="Invalid access code")
    client = clients[0]
    token  = create_client_token(client["client_id"])
    return {
        "token": token, "client_id": client["client_id"],
        "name": client["name"], "plan": client["plan"],
        "modules": get_client_modules(client["client_id"]),
        "avatar": client.get("avatar", client["name"][0].upper()),
        "contact": client.get("contact_name", ""),
    }

@app.get("/auth/me")
async def me(client_id: str = Depends(get_client_id)):
    clients = supabase_select("clients", filters={"client_id": client_id}, limit=1)
    if not clients:
        raise HTTPException(status_code=404, detail="Client not found")
    client = clients[0]
    client["modules"] = get_client_modules(client_id)
    return client

@app.post("/auth/google")
async def auth_google(payload: GoogleAuthPayload):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            res = await c.get("https://oauth2.googleapis.com/tokeninfo", params={"id_token": payload.credential})
            res.raise_for_status()
            info = res.json()
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Google token invalid: {e}")
    if info.get("aud") != GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=401, detail="Token audience mismatch")
    provider_id = info.get("sub", "")
    email = info.get("email", "")
    name  = info.get("name", "")
    client = _find_or_create_sso_client("google", provider_id, email, name)
    if not client:
        raise HTTPException(status_code=500, detail="Failed to create client record")
    token = create_client_token(client["client_id"])
    return {"token": token, "client_id": client["client_id"], "name": client.get("name", name),
            "plan": client.get("plan", "trial"), "modules": get_client_modules(client["client_id"]),
            "avatar": client.get("avatar", name[0].upper() if name else "C"), "provider": "google", "email": email}

@app.get("/auth/linkedin")
async def auth_linkedin_redirect():
    params = {"response_type": "code", "client_id": LINKEDIN_CLIENT_ID, "redirect_uri": LINKEDIN_REDIRECT_URI,
              "scope": "openid profile email w_member_social r_organization_social", "state": "aria_sso"}
    return RedirectResponse("https://www.linkedin.com/oauth/v2/authorization?" + urllib.parse.urlencode(params))

@app.get("/auth/linkedin/callback")
async def auth_linkedin_callback(code: str = "", error: str = "", state: str = ""):
    if error or not code:
        return RedirectResponse(f"{FRONTEND_URL}?auth_error=linkedin_denied")
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            token_res = await c.post("https://www.linkedin.com/oauth/v2/accessToken",
                data={"grant_type": "authorization_code", "code": code, "redirect_uri": LINKEDIN_REDIRECT_URI,
                      "client_id": LINKEDIN_CLIENT_ID, "client_secret": LINKEDIN_CLIENT_SECRET},
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            token_res.raise_for_status()
            access_token = token_res.json().get("access_token", "")
            profile_res  = await c.get("https://api.linkedin.com/v2/userinfo", headers={"Authorization": f"Bearer {access_token}"})
            profile_res.raise_for_status()
            profile = profile_res.json()
    except Exception as e:
        logger.error(f"LinkedIn OAuth error: {e}")
        return RedirectResponse(f"{FRONTEND_URL}?auth_error=linkedin_failed")
    provider_id = profile.get("sub", "")
    email = profile.get("email", "")
    name  = profile.get("name", "")
    client = _find_or_create_sso_client("linkedin", provider_id, email, name)
    if not client:
        return RedirectResponse(f"{FRONTEND_URL}?auth_error=client_create_failed")
    try:
        existing = supabase_select("zapier_routes", filters={"client_id": client["client_id"], "platform": "linkedin"}, limit=1)
        if not existing:
            supabase_insert("zapier_routes", {"client_id": client["client_id"], "platform": "linkedin", "account_type": "personal",
                "account_name": name, "webhook_url": "", "access_token": access_token, "active": True, "notes": "Connected via LinkedIn SSO"})
        else:
            supabase_update("zapier_routes", data={"access_token": access_token}, filters={"client_id": client["client_id"], "platform": "linkedin"})
    except Exception as e:
        logger.warning(f"Could not save LinkedIn token: {e}")
    aria_token = create_client_token(client["client_id"])
    return RedirectResponse(f"{FRONTEND_URL}?sso_token={aria_token}&sso_name={name}&sso_provider=linkedin")

@app.post("/auth/facebook")
async def auth_facebook(payload: FacebookAuthPayload):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            verify_res = await c.get("https://graph.facebook.com/debug_token",
                params={"input_token": payload.access_token, "access_token": f"{FACEBOOK_APP_ID}|{FACEBOOK_APP_SECRET}"})
            verify_res.raise_for_status()
            if not verify_res.json().get("data", {}).get("is_valid"):
                raise HTTPException(status_code=401, detail="Invalid Facebook token")
            profile_res = await c.get(f"https://graph.facebook.com/{payload.user_id}",
                params={"fields": "id,name,email", "access_token": payload.access_token})
            profile_res.raise_for_status()
            profile = profile_res.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Facebook verification failed: {e}")
    provider_id = profile.get("id", payload.user_id)
    email = profile.get("email", "")
    name  = profile.get("name", "")
    client = _find_or_create_sso_client("facebook", provider_id, email, name)
    if not client:
        raise HTTPException(status_code=500, detail="Failed to create client record")
    try:
        existing = supabase_select("zapier_routes", filters={"client_id": client["client_id"], "platform": "facebook"}, limit=1)
        if not existing:
            supabase_insert("zapier_routes", {"client_id": client["client_id"], "platform": "facebook", "account_type": "personal",
                "account_name": name, "webhook_url": "", "access_token": payload.access_token, "active": True, "notes": "Connected via Facebook SSO"})
    except Exception as e:
        logger.warning(f"Could not save Facebook token: {e}")
    token = create_client_token(client["client_id"])
    return {"token": token, "client_id": client["client_id"], "name": client.get("name", name),
            "plan": client.get("plan", "trial"), "modules": get_client_modules(client["client_id"]),
            "avatar": client.get("avatar", name[0].upper() if name else "C"), "provider": "facebook", "email": email}

# ═══════════════════════════════════════════════════════════════
#  CORE ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.get("/")
async def root():
    return {"service": "ARIA™ Orchestrator", "version": "5.0.0", "status": "online"}

@app.get("/status")
async def status():
    jobs = [{"id": j.id, "next_run": str(j.next_run_time) if j.next_run_time else "paused"} for j in scheduler.get_jobs()]
    return {"status": "online", "client_id": CLIENT_ID, "agents": DEFAULT_MODULES, "scheduled_jobs": jobs, "version": "5.0.0"}

# ═══════════════════════════════════════════════════════════════
#  CREA™ PUBLIC TASTER  (no auth — public funnel endpoint)
# ═══════════════════════════════════════════════════════════════

_taster_ips: dict = {}  # in-memory: ip → ISO date string

@app.post("/taster")
async def taster(payload: TasterRequest, request: Request):
    from core.llm import call_llm_async

    # ── IP rate limit: 1 per IP per 24 hours ──
    client_ip = request.client.host
    today_str = date.today().isoformat()
    if _taster_ips.get(client_ip) == today_str:
        raise HTTPException(status_code=429, detail="One free generation per 24 hours. Upgrade to CREA™ for unlimited.")
    _taster_ips[client_ip] = today_str

    biz  = payload.business_name.strip()
    niche = payload.niche.strip()
    plat  = payload.platform.lower()

    platform_instructions = {
        "linkedin": (
            "Write a 150-200 word LinkedIn post. "
            "Authority tone. Short punchy paragraphs. Data or insight opener. "
            "End with a thought-provoking question or CTA. Max 2 hashtags."
        ),
        "instagram": (
            "Write a bold Instagram caption. "
            "Hook on line 1. 6-8 punchy short lines. End with CTA. "
            "4-5 relevant hashtags on the final line."
        ),
        "facebook": (
            "Write a warm, conversational Facebook post. "
            "2-3 paragraphs. Speak directly to business owners. "
            "End with an engaging question that invites comments."
        ),
    }

    prompt = f"""You are CREA™ — an elite AI content engine for businesses.
Write a single, high-converting {plat} post for this business:
Business Name: {biz}
Industry/Niche: {niche}

{platform_instructions.get(plat, platform_instructions['linkedin'])}

Rules:
- Sound human, not like AI
- Address a real pain point in the {niche} industry
- Make the reader feel seen
- Do not use: "game-changer", "leverage", "delve", "in today's world"
- Output the post text only — no preamble, no explanation, no title"""

    try:
        post_text = await call_llm_async(prompt, max_tokens=500, temperature=0.8)
    except Exception as e:
        logger.error(f"Taster LLM failed: {e}")
        raise HTTPException(status_code=503, detail="Content generation failed. Please try again.")

    # ── Pollinations image ──
    width, height = (1080, 1080) if plat == "instagram" else (1216, 640)
    img_prompt = (
        f"premium dark cinematic brand visual for {niche} business, "
        "ultra high quality, minimal text, luxury aesthetic, dark background, professional"
    )
    encoded   = urllib.parse.quote(img_prompt)
    image_url = (
        f"https://image.pollinations.ai/prompt/{encoded}"
        f"?width={width}&height={height}&nologo=true&seed={abs(hash(biz)) % 9999}&model=flux"
    )

    logger.info(f"Taster generated: {biz} / {niche} / {plat} [{client_ip}]")
    return {
        "post":      post_text,
        "image_url": image_url,
        "platform":  plat,
        "business":  biz,
    }


# ═══════════════════════════════════════════════════════════════
#  MARKETING CHAT — public "Say hi to LUIN" widget on luin.cc
#  Groq key stays server-side; IP rate-limited: 30 messages/IP/day
# ═══════════════════════════════════════════════════════════════

_marketing_chat_ips: dict = {}

LUIN_MARKETING_PROMPT = """You are LUIN — the AI Marketing Concierge of LUIN™, built by OUP International Ltd. You're warm, witty, and genuinely curious about the person you're talking to.

Your style:
- Conversational and human — short messages, like texting with a sharp friend
- Confident without being cocky — you know what LUIN can do, you don't need to oversell it
- You ask good questions and actually listen to the answers

Your job:
1. Greet them warmly and learn what they do
2. Understand their marketing situation — what's working, what isn't
3. Identify whether LUIN is a genuine fit for them
4. If they're a fit, guide them toward booking a strategy call
5. Weave in what LUIN does only when it's relevant to what they've shared

Qualification signals:
- Do they run a business or lead marketing? (decision maker = strong fit)
- How big is their team? (solo/small = strong LUIN fit)
- Are they spending on marketing already?

LUIN'S TIERS (share only when relevant or asked):
- Starter: $997/mo — 1 workspace, 15 posts/month, 2 platforms
- Growth: $2,497/mo — 3 workspaces, 60 posts/month, 5 platforms, AI Concierge
- Enterprise: custom — unlimited workspaces and posts, custom integrations, SLA

If they share contact details or want to book, respond warmly and include the marker [SHOW_CALENDLY] at the very end of your message.
Keep every reply to 1-3 short sentences unless detail is genuinely needed. Never say "can't"."""


class MarketingChatMessage(BaseModel):
    message: str
    history: Optional[List[dict]] = None


@app.post("/marketing/chat")
async def marketing_chat(payload: MarketingChatMessage, request: Request):
    from core.llm import call_llm_async

    # ── IP rate limit: 30 messages per IP per day ──
    client_ip = request.client.host
    today_str = date.today().isoformat()
    ip_record = _marketing_chat_ips.get(client_ip)
    if ip_record and ip_record["date"] == today_str:
        if ip_record["count"] >= 30:
            raise HTTPException(status_code=429, detail="Daily chat limit reached — book a call to continue the conversation.")
        ip_record["count"] += 1
    else:
        _marketing_chat_ips[client_ip] = {"date": today_str, "count": 1}

    msg = (payload.message or "").strip()
    if not msg or len(msg) > 2000:
        raise HTTPException(status_code=400, detail="Message must be 1-2000 characters.")

    full_prompt = "\n\n".join(
        f"{h['role']}: {h['content'][:2000]}"
        for h in (payload.history or [])[-8:]
        if h.get("role") in ("user", "assistant") and h.get("content")
    )
    full_prompt += f"\n\nuser: {msg}"

    try:
        reply = await call_llm_async(full_prompt, system=LUIN_MARKETING_PROMPT, max_tokens=400, temperature=0.8)
    except Exception as e:
        logger.error(f"Marketing chat error: {e}")
        reply = "I'm having a moment — try again?"

    show_calendly = "[SHOW_CALENDLY]" in reply
    reply = reply.replace("[SHOW_CALENDLY]", "").strip()
    return {"reply": reply, "show_calendly": show_calendly}

# ═══════════════════════════════════════════════════════════════
#  WEBHOOK / CEO
# ═══════════════════════════════════════════════════════════════

@app.post("/webhook/ceo")
async def ceo_webhook(payload: CEOInstruction, client_id: str = Depends(get_client_id)):
    supabase_insert("instructions", {"client_id": client_id, "target_agent": payload.agent, "instruction": payload.instruction, "issued_by": "ceo", "active": True})
    if payload.priority == "urgent":
        def run(name, cid):
            agent = _make_agent(name, cid)
            if agent: agent.execute_cycle()
        asyncio.create_task(asyncio.to_thread(run, payload.agent, client_id))
        return {"status": "instruction_stored_and_triggered", "agent": payload.agent}
    return {"status": "instruction_stored", "agent": payload.agent}

@app.post("/trigger/{agent_name}")
async def trigger_agent(agent_name: str, client_id: str = Depends(get_client_id)):
    modules = get_client_modules(client_id)
    if agent_name not in modules:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_name}' not enabled. Available: {modules}")
    def run(name, cid):
        agent = _make_agent(name, cid)
        if agent: agent.execute_cycle()
    asyncio.create_task(asyncio.to_thread(run, agent_name, client_id))
    return {"status": "triggered", "agent": agent_name, "client_id": client_id}

@app.get("/briefing")
async def morning_briefing(client_id: str = Depends(get_client_id)):
    today   = date.today().isoformat()
    reports = supabase_select("reports", filters={"client_id": client_id, "cycle_date": today}, limit=20)
    flags   = supabase_select("agent_flags", filters={"client_id": client_id, "resolved": False}, limit=20)
    pending = supabase_select("content_queue", filters={"client_id": client_id, "approved": False, "published": False}, limit=10)
    leads   = supabase_select("leads", filters={"client_id": client_id}, order_by="created_at", limit=5)
    hot     = [l for l in leads if l.get("score", 0) >= 8]
    parts   = []
    if reports: parts.append(f"{len(reports)} agents reported.")
    if hot:     parts.append(f"{len(hot)} hot lead(s).")
    if pending: parts.append(f"{len(pending)} piece(s) awaiting approval.")
    return {"date": today, "client_id": client_id, "modules": get_client_modules(client_id),
            "agent_reports": reports, "active_flags": flags, "pending_approval": len(pending),
            "hot_leads_today": hot, "summary": " ".join(parts) if parts else "No activity yet today."}

@app.get("/agents/{name}/leads")
async def get_leads(name: str, limit: int = 50, client_id: str = Depends(get_client_id)):
    leads = supabase_select("leads", filters={"client_id": client_id}, order_by="created_at", limit=limit)
    return {"agent": name, "leads": leads, "count": len(leads)}

@app.get("/agents/{name}/report")
async def get_agent_report(name: str, client_id: str = Depends(get_client_id)):
    reports = supabase_select("reports", filters={"client_id": client_id, "agent_name": name}, order_by="cycle_date", limit=1)
    if not reports:
        raise HTTPException(status_code=404, detail=f"No reports for '{name}'")
    return reports[0]

@app.get("/content/queue")
async def content_queue(approved: bool = None, client_id: str = Depends(get_client_id)):
    filters = {"client_id": client_id}
    if approved is not None:
        filters["approved"] = approved
    content = supabase_select("content_queue", filters=filters, limit=50)
    return {"content": content, "count": len(content)}

@app.get("/memory/{agent_name}")
async def get_agent_memory(agent_name: str, days: int = 7, client_id: str = Depends(get_client_id)):
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    memory = supabase_select("agent_memory", filters={"client_id": client_id, "agent_name": agent_name}, order_by="cycle_date", limit=days)
    return {"agent": agent_name, "memory": [m for m in memory if m.get("cycle_date", "") >= cutoff]}

@app.get("/flags")
async def get_flags(resolved: bool = False, client_id: str = Depends(get_client_id)):
    flags = supabase_select("agent_flags", filters={"client_id": client_id, "resolved": resolved}, limit=50)
    return {"flags": flags, "count": len(flags)}

# ═══════════════════════════════════════════════════════════════
#  CONTENT COMMAND CENTRE
# ═══════════════════════════════════════════════════════════════

@app.get("/content/review-queue")
async def review_queue(client_id: str = Depends(get_client_id)):
    pending   = supabase_select("content_queue", filters={"client_id": client_id, "status": "pending_review"}, limit=50)
    approved  = supabase_select("content_queue", filters={"client_id": client_id, "status": "approved", "published": False}, limit=20)
    scheduled = supabase_select("content_queue", filters={"client_id": client_id, "status": "scheduled"}, limit=20)
    requests_list = supabase_select("content_requests", filters={"client_id": client_id, "status": "pending"}, limit=20)
    return {"pending_review": pending, "approved": approved, "scheduled": scheduled, "client_requests": requests_list,
            "counts": {"pending_review": len(pending), "approved": len(approved), "scheduled": len(scheduled), "client_requests": len(requests_list)}}

@app.post("/content/request")
async def submit_content_request(payload: ContentRequest, client_id: str = Depends(get_client_id)):
    row = supabase_insert("content_requests", {"client_id": client_id, "topic": payload.topic, "format": payload.format,
        "platform": payload.platform, "vibe": payload.vibe or None, "requested_by": payload.requested_by,
        "schedule_for": payload.schedule_for or None, "status": "pending"})
    def run_writer(cid, req):
        from agents.writer import WriterAgent
        WriterAgent(client_id=cid)._write_on_demand(req)
    request_data = {**(row or {}), "topic": payload.topic, "format": payload.format, "platform": payload.platform,
        "vibe": payload.vibe or "", "requested_by": payload.requested_by, "id": (row or {}).get("id"), "client_id": client_id}
    asyncio.create_task(asyncio.to_thread(run_writer, client_id, request_data))
    return {"status": "triggered", "message": "Writer is working on it — check review queue in ~30s", "request_id": (row or {}).get("id")}

@app.post("/content/approve")
async def approve_content(payload: ContentApproval, client_id: str = Depends(get_client_id)):
    content = supabase_select("content_queue", filters={"id": payload.content_id, "client_id": client_id}, limit=1)
    if not content:
        raise HTTPException(status_code=404, detail="Content not found")

    if payload.action == "approve":
        update = {"approved": True, "status": "approved", "ceo_note": payload.ceo_note or None, "approved_at": date.today().isoformat()}
        if payload.schedule_for:
            update["status"] = "scheduled"
            update["scheduled_for"] = payload.schedule_for
        supabase_update("content_queue", row_id=payload.content_id, data=update)
        _attach_crea_image(payload.content_id, client_id)

        # ── Auto-publish to blog if platform is blog ──
        item = content[0]
        if item.get("platform") == "blog":
            draft        = item.get("draft", "")
            lines        = draft.strip().split("\n")
            title        = lines[0].lstrip("#").strip() if lines else "Untitled"
            word_count   = len(draft.split())
            read_mins    = max(1, round(word_count / 200))
            publish_date = item.get("scheduled_for") or (date.today() + timedelta(days=1)).isoformat()
            try:
                supabase_insert("blog_posts", {
                    "client_id":     client_id,
                    "title":         title,
                    "slug":          payload.content_id,
                    "tag":           "AI & Automation",
                    "tag_filter":    "ai",
                    "excerpt":       " ".join(draft.replace("#", "").split()[:30]) + "...",
                    "body":          draft,
                    "author":        "ARIA™",
                    "author_initial":"A",
                    "emoji":         "🤖",
                    "read_time":     f"{read_mins} min read",
                    "published":     True,
                    "publish_date":  publish_date,
                    "featured":      False,
                })
                logger.info(f"Blog post published: {title[:60]} → {publish_date}")
            except Exception as e:
                logger.warning(f"Blog publish failed: {e}")

        if payload.post_immediately:
            def post(cid):
                from agents.linkedin import LinkedInAgent
                LinkedInAgent(client_id=cid).execute_cycle()
            asyncio.create_task(asyncio.to_thread(post, client_id))
            return {"status": "approved_and_posting", "content_id": payload.content_id}
        return {"status": "approved", "content_id": payload.content_id}

    elif payload.action == "reject":
        supabase_update("content_queue", row_id=payload.content_id,
            data={"status": "rejected", "ceo_note": payload.ceo_note or None, "approved": False})
        return {"status": "rejected", "content_id": payload.content_id}

    elif payload.action == "request_changes":
        supabase_update("content_queue", row_id=payload.content_id,
            data={"status": "changes_requested", "ceo_note": payload.ceo_note or "", "approved": False})
        note = payload.ceo_note or ""
        def redraft(cid, cid2, n):
            from agents.writer import WriterAgent
            WriterAgent(client_id=cid)._write_on_demand({"id": None, "topic": f"Redraft content {cid2}. CEO feedback: {n}",
                "format": "linkedin_post", "platform": "linkedin", "vibe": "", "requested_by": "ceo", "client_id": cid})
        asyncio.create_task(asyncio.to_thread(redraft, client_id, payload.content_id, note))
        return {"status": "changes_requested", "content_id": payload.content_id}

    raise HTTPException(status_code=400, detail=f"Unknown action '{payload.action}'")

def _attach_crea_image(content_id: str, client_id: str):
    try:
        assets = supabase_select("crea_assets", filters={"client_id": client_id, "approved": True, "attached": False}, limit=1)
        if assets:
            asset = assets[0]
            image_url = asset.get("image_url", "")
            if image_url:
                supabase_update("content_queue", row_id=content_id, data={"image_url": image_url, "crea_asset_id": asset["id"]})
                supabase_update("crea_assets", row_id=asset["id"], data={"attached": True, "attached_to": content_id})
    except Exception as e:
        logger.warning(f"CREA attach failed: {e}")

@app.patch("/content/edit")
async def edit_content(payload: ContentEdit, client_id: str = Depends(get_client_id)):
    supabase_update("content_queue", row_id=payload.content_id,
        data={"draft": payload.new_draft, "edited_by": payload.edited_by, "edited_at": date.today().isoformat(), "status": "pending_review", "approved": False})
    return {"status": "updated", "content_id": payload.content_id}

@app.post("/content/post-now/{content_id}")
async def post_now(content_id: str, client_id: str = Depends(get_client_id)):
    rows = supabase_select("content_queue", filters={"id": content_id, "client_id": client_id}, limit=1)
    if not rows:
        raise HTTPException(status_code=404, detail="Content not found")
    item = rows[0]
    if not item.get("approved"):
        raise HTTPException(status_code=400, detail="Must be approved before posting")
    platform  = item.get("platform", "linkedin")
    draft     = item.get("draft", "")
    image_url = item.get("image_url", "")
    routes = supabase_select("zapier_routes", filters={"client_id": client_id, "platform": platform, "active": True}, limit=1)
    if not routes:
        routes = supabase_select("zapier_routes", filters={"client_id": client_id, "active": True}, limit=1)
    if not routes:
        raise HTTPException(status_code=503, detail=f"No active webhook route found for platform '{platform}'.")
    webhook_url = routes[0]["webhook_url"]
    async def dispatch():
        payload = {"content": draft, "platform": platform, "content_id": content_id, "client_id": client_id, "post_type": item.get("post_type", "text")}
        if image_url:
            payload["media_url"] = image_url
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                res = await c.post(webhook_url, json=payload)
                res.raise_for_status()
                logger.info(f"post-now dispatched → {platform} ({res.status_code})")
        except Exception as e:
            logger.error(f"post-now webhook failed [{platform}]: {e}")
            return
        supabase_update("content_queue", row_id=content_id, data={"published": True, "published_at": date.today().isoformat(), "status": "published"})
    asyncio.create_task(dispatch())
    return {"status": "dispatching", "content_id": content_id, "platform": platform, "webhook": webhook_url[:40] + "..."}

@app.post("/content/override")
async def ceo_override(payload: CEOOverride, client_id: str = Depends(get_client_id)):
    supabase_insert("instructions", {"client_id": client_id, "target_agent": payload.agent, "instruction": payload.instruction, "issued_by": "ceo", "priority": "urgent", "active": True})
    if payload.trigger_immediately:
        def run(name, cid):
            agent = _make_agent(name, cid)
            if agent: agent.execute_cycle()
        asyncio.create_task(asyncio.to_thread(run, payload.agent, client_id))
        return {"status": "override_issued_and_triggered", "agent": payload.agent}
    return {"status": "override_issued", "agent": payload.agent}

@app.get("/content/schedule")
async def get_schedule(client_id: str = Depends(get_client_id)):
    scheduled = supabase_select("content_queue", filters={"client_id": client_id, "approved": True, "published": False}, order_by="scheduled_for", limit=30)
    return {"schedule": scheduled, "count": len(scheduled)}

@app.post("/linkedin/seed-evergreen")
async def seed_evergreen(client_id: str = Depends(get_client_id)):
    def seed(cid):
        from agents.linkedin import LinkedInAgent
        LinkedInAgent(client_id=cid).seed_evergreen_reserve()
    asyncio.create_task(asyncio.to_thread(seed, client_id))
    return {"status": "seeding", "client_id": client_id}

@app.get("/content/evergreen")
async def get_evergreen_reserve(client_id: str = Depends(get_client_id)):
    return supabase_select("evergreen_reserve", filters={"client_id": client_id}, limit=50) or []

# ═══════════════════════════════════════════════════════════════
#  CEO CHAT
# ═══════════════════════════════════════════════════════════════

@app.post("/chat")
async def ceo_chat(payload: ChatMessage, client_id: str = Depends(get_client_id)):
    from core.llm import call_llm_async
    profiles = supabase_select("brand_profiles", filters={"client_id": client_id}, limit=1)
    profile  = profiles[0] if profiles else {}
    today    = date.today().isoformat()
    pending  = supabase_select("content_queue", filters={"client_id": client_id, "status": "pending_review"}, limit=5)
    modules  = get_client_modules(client_id)
    system_prompt = f"""You are ARIA CEO — autonomous AI Marketing Director for {profile.get('name', client_id)}.
Speak with authority, brevity, precision. You run these agents: {', '.join(modules)}.
Brand tone: {profile.get('tone', 'Professional')}
Audience: {profile.get('target_audience', 'B2B decision makers')}
Today: {today} · Pending: {len(pending)} items
Be concise — 2-4 sentences unless detail needed. Never say "can't"."""
    full_prompt = "\n\n".join(f"{h['role']}: {h['content']}" for h in (payload.history or [])[-6:] if h.get("role") in ("user", "assistant"))
    full_prompt += f"\n\nuser: {payload.message}"
    try:
        reply = await call_llm_async(full_prompt, system=system_prompt, max_tokens=400, temperature=0.7)
    except Exception as e:
        logger.error(f"Chat error: {e}")
        reply = "I encountered an issue. Please try again."
    return {"reply": reply, "client_id": client_id}

# ═══════════════════════════════════════════════════════════════
#  FACEBOOK / BLOG
# ═══════════════════════════════════════════════════════════════

@app.post("/facebook/generate")
async def facebook_generate(payload: FacebookRequest, client_id: str = Depends(get_client_id)):
    def run(cid, req):
        from agents.writer import WriterAgent
        WriterAgent(client_id=cid)._write_on_demand(req)
    asyncio.create_task(asyncio.to_thread(run, client_id, {"id": None, "topic": payload.topic or "Create an engaging Facebook post",
        "format": "facebook_post", "platform": "facebook", "vibe": payload.vibe or "", "requested_by": "manual", "client_id": client_id}))
    return {"status": "generating", "message": "Facebook post will appear in queue."}

@app.post("/blog/generate")
async def blog_generate(payload: BlogRequest, client_id: str = Depends(get_client_id)):
    def run(cid, req):
        from agents.writer import WriterAgent
        WriterAgent(client_id=cid)._write_on_demand(req)
    asyncio.create_task(asyncio.to_thread(run, client_id, {"id": None, "topic": payload.topic or "Write a thought leadership blog article",
        "format": "blog", "platform": "blog", "vibe": payload.vibe or "", "requested_by": "manual",
        "client_id": client_id, "word_count": payload.word_count or 1500}))
    return {"status": "generating", "message": f"Blog (~{payload.word_count or 1500} words) will appear in queue."}

@app.get("/blog/posts")
async def get_blog_posts(limit: int = 20):
    """Public endpoint — no auth. Returns published blog posts."""
    try:
        from supabase import create_client
        url = os.environ.get("SUPABASE_URL", "")
        key = os.environ.get("SUPABASE_KEY", "")
        sb  = create_client(url, key)
        res = sb.table("blog_posts").select("*").eq("published", True).order("publish_date", desc=True).limit(limit).execute()
        return res.data or []
    except Exception as e:
        logger.error(f"Blog posts fetch failed: {e}")
        return []

# ═══════════════════════════════════════════════════════════════
#  BRAND PROFILE
# ═══════════════════════════════════════════════════════════════

@app.get("/brand/profile")
async def get_brand_profile(client_id: str = Depends(get_client_id)):
    profiles = supabase_select("brand_profiles", filters={"client_id": client_id}, limit=1)
    if not profiles:
        raise HTTPException(status_code=404, detail="Brand profile not found")
    return profiles[0]

@app.put("/brand/profile")
async def update_brand_profile(payload: BrandProfileUpdate, client_id: str = Depends(get_client_id)):
    updates = {k: v for k, v in payload.dict().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    supabase_update("brand_profiles", data=updates, filters={"client_id": client_id})
    return {"status": "updated", "client_id": client_id}

# ═══════════════════════════════════════════════════════════════
#  CREA™ CREATIVE AGENT
# ═══════════════════════════════════════════════════════════════

@app.get("/agents/crea/health")
async def crea_health():
    from agents.crea import CREAAgent
    return CREAAgent().health()

@app.post("/agents/crea/expand-prompt")
async def crea_expand_prompt(payload: CREAExpandRequest, client_id: str = Depends(get_client_id)):
    from agents.crea import CREAAgent
    return CREAAgent(client_id=client_id).expand_prompt(brief=payload.brief, style=payload.style, format=payload.format)

@app.post("/agents/crea/save-asset")
async def crea_save_asset(payload: CREASaveAssetRequest, client_id: str = Depends(get_client_id)):
    from agents.crea import CREAAgent
    return CREAAgent(client_id=client_id).save_asset(campaign_id=payload.campaign_id, image_b64=payload.image_b64,
        image_url=payload.image_url, prompt=payload.prompt, negative_prompt=payload.negative_prompt,
        style=payload.style, channel=payload.channel)

@app.post("/agents/crea/generate")
async def crea_generate(payload: CREAGenerateRequest, client_id: str = Depends(get_client_id)):
    from agents.crea import CREAAgent
    kwargs = {"campaign_id": payload.campaign_id, "prompt": payload.override_prompt or "",
        "channel": payload.channel, "dimensions": payload.dimensions,
        "base_image_b64": payload.base_image_b64}
    if payload.denoise is not None:
        kwargs["denoise"] = payload.denoise
    result = CREAAgent(client_id=client_id).generate(**kwargs)
    if not result.get("success"):
        raise HTTPException(status_code=503, detail=result.get("error", "Generation failed"))
    return result

@app.get("/agents/crea/assets/{campaign_id}")
async def crea_assets(campaign_id: str, client_id: str = Depends(get_client_id)):
    from agents.crea import CREAAgent
    assets = CREAAgent(client_id=client_id).get_assets(campaign_id)
    return {"assets": assets, "count": len(assets), "campaign_id": campaign_id}

@app.post("/agents/crea/approve/{asset_id}")
async def crea_approve(asset_id: str, client_id: str = Depends(get_client_id)):
    from agents.crea import CREAAgent
    CREAAgent(client_id=client_id).approve_asset(asset_id)
    return {"status": "approved", "asset_id": asset_id}

@app.post("/agents/crea/reject/{asset_id}")
async def crea_reject(asset_id: str, payload: CREARejectRequest, client_id: str = Depends(get_client_id)):
    from agents.crea import CREAAgent
    CREAAgent(client_id=client_id).reject_asset(asset_id, payload.feedback)
    return {"status": "rejected", "asset_id": asset_id}

@app.post("/agents/crea/comfyui-submit")
async def comfyui_submit(payload: ComfyUISubmitRequest, client_id: str = Depends(get_client_id)):
    ngrok = os.environ.get("COMFYUI_NGROK", "")
    if not ngrok:
        raise HTTPException(status_code=503, detail="COMFYUI_NGROK not set")
    async with httpx.AsyncClient(timeout=15) as c:
        res = await c.post(f"{ngrok}/prompt", json={"prompt": payload.workflow},
            headers={"Content-Type": "application/json", "ngrok-skip-browser-warning": "true"})
        res.raise_for_status()
        return res.json()

@app.get("/agents/crea/comfyui-history/{prompt_id}")
async def comfyui_history(prompt_id: str, client_id: str = Depends(get_client_id)):
    ngrok = os.environ.get("COMFYUI_NGROK", "")
    if not ngrok:
        raise HTTPException(status_code=503, detail="COMFYUI_NGROK not set")
    for attempt in range(5):
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                res = await c.get(f"{ngrok}/history/{prompt_id}", headers={"ngrok-skip-browser-warning": "true"})
                res.raise_for_status()
                data = res.json()
                if prompt_id in data:
                    return data
        except Exception:
            pass
        await asyncio.sleep(2 ** attempt)
    raise HTTPException(status_code=503, detail="ComfyUI history not ready")

# ═══════════════════════════════════════════════════════════════
#  INTAKE AGENT
# ═══════════════════════════════════════════════════════════════

@app.post("/intake/brief")
async def intake_brief(payload: IntakeBriefRequest, client_id: str = Depends(get_client_id)):
    required = ["business_problem", "target_audience_detail", "success_30_days", "success_90_days", "already_tried", "single_most_important_outcome"]
    missing  = [f for f in required if not getattr(payload, f, None)]
    if missing:
        return {"status": "questions_required", "campaign_name": payload.campaign_name, "message": "Answer these questions first.",
            "questions": [
                {"field": "business_problem",              "question": "What is the actual business problem?"},
                {"field": "target_audience_detail",        "question": "Who is the target audience?"},
                {"field": "success_30_days",               "question": "What does success look like in 30 days?"},
                {"field": "success_90_days",               "question": "What does success look like in 90 days?"},
                {"field": "already_tried",                 "question": "What has already been tried?"},
                {"field": "constraints",                   "question": "What constraints exist?"},
                {"field": "single_most_important_outcome", "question": "If one thing, what must it be?"},
            ]}
    row = supabase_insert("campaigns", {"client_id": client_id, "campaign_name": payload.campaign_name,
        "raw_brief": payload.raw_brief, "business_problem": payload.business_problem,
        "target_audience": payload.target_audience_detail, "success_30_days": payload.success_30_days,
        "success_90_days": payload.success_90_days, "already_tried": payload.already_tried,
        "constraints": payload.constraints, "single_most_important_outcome": payload.single_most_important_outcome,
        "status": "strategy_ready", "created_at": date.today().isoformat()})
    return {"status": "strategy_document_created", "campaign_id": row["id"] if row else None}

@app.get("/intake/campaigns")
async def list_campaigns(client_id: str = Depends(get_client_id)):
    campaigns = supabase_select("campaigns", filters={"client_id": client_id}, limit=20)
    return {"campaigns": campaigns, "count": len(campaigns)}

# ═══════════════════════════════════════════════════════════════
#  ADMIN
# ═══════════════════════════════════════════════════════════════

@app.post("/admin/onboard")
async def onboard_client(payload: ClientOnboard, _=Depends(verify_api_key)):
    existing = supabase_select("clients", filters={"client_id": payload.client_id}, limit=1)
    if existing:
        raise HTTPException(status_code=409, detail=f"Client '{payload.client_id}' already exists")
    modules = payload.modules or DEFAULT_MODULES
    supabase_insert("clients", {"client_id": payload.client_id, "name": payload.name, "access_code": payload.access_code,
        "plan": payload.plan, "modules": json.dumps(modules), "avatar": payload.name[0].upper(),
        "contact_name": payload.contact_name, "contact_email": payload.contact_email, "monthly_fee": payload.monthly_fee, "active": True})
    supabase_insert("brand_profiles", {"client_id": payload.client_id, "name": payload.name,
        "tone": payload.brand_tone or f"Professional and authoritative voice for {payload.name}",
        "target_audience": payload.target_audience, "content_goals": payload.content_goals, "avoid": payload.avoid,
        "linkedin_style": payload.linkedin_style or "Short paragraphs, punchy opener, ends with question or CTA",
        "email_style": "Clear subject line, 3 paragraphs max, one CTA", "blog_style": "1,500-2,000 words, SEO-optimised, H2 subheadings"})
    if payload.linkedin_make_webhook and payload.linkedin_company_id:
        from agents.linkedin import add_route
        add_route(client_id=payload.client_id, platform="linkedin", account_type="company_page",
            account_name=payload.name, webhook_url=payload.linkedin_make_webhook, notes="Auto-provisioned")
    token = create_client_token(payload.client_id)
    logger.info(f"Onboarded: {payload.client_id} ({payload.name}) modules={modules}")
    return {"status": "onboarded", "client_id": payload.client_id, "name": payload.name, "modules": modules, "token": token}

@app.get("/admin/clients")
async def list_clients(_=Depends(verify_api_key)):
    clients = supabase_select("clients", limit=100)
    return {"clients": clients, "count": len(clients)}

@app.post("/admin/upload-brand-asset/{client_id}")
async def upload_brand_asset(client_id: str, asset_type: str = Form(...), file: UploadFile = File(...), _=Depends(verify_api_key)):
    contents = await file.read()
    b64 = base64.b64encode(contents).decode()
    supabase_insert("brand_assets", {"client_id": client_id, "asset_type": asset_type, "filename": file.filename, "mimetype": file.content_type, "data_b64": b64})
    return {"status": "uploaded", "client_id": client_id, "asset_type": asset_type}

# ═══════════════════════════════════════════════════════════════
#  PLATFORMS & ROUTES
# ═══════════════════════════════════════════════════════════════

@app.get("/platforms/routes")
async def list_routes(client: Optional[str] = None, client_id: str = Depends(get_client_id)):
    from agents.linkedin import get_all_routes
    return {"routes": get_all_routes(client or client_id), "count": 0}

@app.post("/platforms/routes")
async def create_route(payload: RouteCreate, _=Depends(verify_api_key)):
    from agents.linkedin import add_route
    row = add_route(client_id=payload.client_id or CLIENT_ID, platform=payload.platform, account_type=payload.account_type,
        account_name=payload.account_name, webhook_url=payload.webhook_url, notes=payload.notes or "")
    if not row:
        raise HTTPException(status_code=500, detail="Failed to create route")
    return {"status": "created", "route": row}

@app.patch("/platforms/routes/{route_id}")
async def update_route(route_id: str, payload: RouteToggle, _=Depends(verify_api_key)):
    from agents.linkedin import toggle_route
    toggle_route(route_id, payload.active)
    return {"status": "updated", "route_id": route_id, "active": payload.active}

# ═══════════════════════════════════════════════════════════════
#  SOCIAL OPERATOR — Performance data intake (v6)
#  Feed post analytics from Make.com or LinkedIn API back into ARIA.
# ═══════════════════════════════════════════════════════════════

class PerformanceReport(BaseModel):
    content_id:  str
    likes:       Optional[int] = 0
    comments:    Optional[int] = 0
    shares:      Optional[int] = 0
    impressions: Optional[int] = 0
    clicks:      Optional[int] = 0
    source:      Optional[str] = "make"   # 'make' | 'api' | 'manual'

@app.post("/social/report-performance")
async def report_performance(payload: PerformanceReport, _=Depends(verify_api_key)):
    """
    Receive post engagement data from Make.com (or any source) and update
    the post_performance table so the Social Operator can evaluate it.

    Make.com setup:
      1. Create a scheduled scenario (daily, 9am)
      2. Module: LinkedIn → Get Page Post Statistics (use r_organization_social scope)
      3. Module: HTTP → POST https://aria-agents.onrender.com/social/report-performance
         Headers: X-API-Key: {{ARIA_API_KEY}}
         Body: { content_id, likes, comments, shares, impressions, clicks, source: "make" }
    """
    record = supabase_select(
        "post_performance",
        filters={"content_id": payload.content_id},
        limit=1,
    )
    if not record:
        raise HTTPException(status_code=404, detail="Post not found in tracking table")

    supabase_update("post_performance", row_id=record[0]["id"], data={
        "likes":        payload.likes,
        "comments":     payload.comments,
        "shares":       payload.shares,
        "impressions":  payload.impressions,
        "clicks":       payload.clicks,
        "data_source":  payload.source,
        "updated_at":   datetime.utcnow().isoformat() if hasattr(datetime, 'utcnow') else "",
    })
    return {"status": "updated", "content_id": payload.content_id}

@app.get("/social/performance")
async def get_performance(client_id: str = Depends(get_client_id)):
    """
    Return all post_performance records for this client.
    Used by the dashboard to show the Social Operator's tracking data.
    """
    records = supabase_select(
        "post_performance",
        filters={"client_id": client_id},
        order_by="posted_at",
        limit=50,
    )
    return {"records": records, "count": len(records)}

@app.get("/memory/durable")
async def get_durable_memory(client_id: str = Depends(get_client_id)):
    """Return all durable memories for this client (for dashboard display)."""
    from core.durable_memory import DurableMemoryManager
    mem = DurableMemoryManager(client_id=client_id, agent_name="all")
    memories = mem.recall(limit=100, include_stale=False)
    return {"memories": memories, "count": len(memories)}
