"""
main.py
ARIA™ — FastAPI Orchestrator v3
All agents scheduled here. CEO webhook, briefing, lead, content command centre endpoints.
OUP International Ltd, 2026
"""

import os
import json
import logging
import asyncio
from contextlib import asynccontextmanager
from datetime import date
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException, Header, Depends, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional, List
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
import jwt
import base64

from agents.jamie import JAMIEHunter
from agents.research import ResearchAgent
from agents.writer import WriterAgent
from agents.reporting import ReportingAgent
from agents.linkedin import LinkedInAgent
from core.memory import supabase_select, supabase_insert, supabase_update

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("aria.main")

# ── JWT Config ────────────────────────────────────────────────────────
JWT_SECRET = os.environ.get("JWT_SECRET", "aria-jwt-secret-change-in-production")
JWT_ALGO   = "HS256"

def create_client_token(client_id: str) -> str:
    return jwt.encode({"client_id": client_id, "iss": "aria"}, JWT_SECRET, algorithm=JWT_ALGO)

def decode_client_token(token: str) -> str:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
        return payload["client_id"]
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

# ── Agent factory — creates per-client instances ──────────────────────
def get_agents(client_id: str):
    return {
        "jamie":     JAMIEHunter(),
        "research":  ResearchAgent(client_id=client_id),
        "writer":    WriterAgent(client_id=client_id),
        "reporting": ReportingAgent(client_id=client_id),
        "linkedin":  LinkedInAgent(client_id=client_id),
    }

# ── Default internal agents for scheduler ────────────────────────────
CLIENT_ID = os.environ.get("CLIENT_ID", "aria_internal")
jamie     = JAMIEHunter()
research  = ResearchAgent(client_id=CLIENT_ID)
writer    = WriterAgent(client_id=CLIENT_ID)
reporting = ReportingAgent(client_id=CLIENT_ID)
linkedin  = LinkedInAgent(client_id=CLIENT_ID)

scheduler = AsyncIOScheduler()

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"ARIA starting — client: {CLIENT_ID}")
    scheduler.add_job(lambda: jamie.execute_cycle(),     IntervalTrigger(minutes=30),                        id="jamie",     replace_existing=True)
    scheduler.add_job(lambda: research.execute_cycle(),  CronTrigger(hour=6, minute=0),                      id="research",  replace_existing=True)
    scheduler.add_job(lambda: writer.execute_cycle(),    CronTrigger(hour=7, minute=0),                      id="writer",    replace_existing=True)
    scheduler.add_job(lambda: linkedin.execute_cycle(),  CronTrigger(hour=12, minute=0),                     id="linkedin",  replace_existing=True)
    scheduler.add_job(lambda: reporting.execute_cycle(), CronTrigger(day_of_week="mon", hour=7, minute=0),   id="reporting", replace_existing=True)
    scheduler.start()
    logger.info("Scheduler started — Research 6am · Writer 7am · LinkedIn 12pm · Reporting Mon 7am")
    yield
    scheduler.shutdown()
    logger.info("ARIA shutdown complete")

app = FastAPI(title="ARIA™ Orchestrator", description="OUP International Ltd", version="3.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Auth ──────────────────────────────────────────────────────────────
ARIA_API_KEY = os.environ.get("ARIA_API_KEY", "")

def verify_api_key(x_api_key: str = Header(default="")):
    if ARIA_API_KEY and x_api_key != ARIA_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return x_api_key

def get_client_id(x_client_token: str = Header(default=""), x_api_key: str = Header(default="")) -> str:
    if x_client_token:
        return decode_client_token(x_client_token)
    if ARIA_API_KEY and x_api_key == ARIA_API_KEY:
        return CLIENT_ID
    if not ARIA_API_KEY:
        return CLIENT_ID
    raise HTTPException(status_code=401, detail="Authentication required")

# ── Models ────────────────────────────────────────────────────────────
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

class RouteCreate(BaseModel):
    platform: str
    account_type: str
    account_name: str
    webhook_url: str
    client_id: Optional[str] = None
    notes: Optional[str] = ""

class RouteToggle(BaseModel):
    active: bool

# ═══════════════════════════════════════════════════════════════════════
#  AUTH
# ═══════════════════════════════════════════════════════════════════════

@app.post("/auth/login")
async def login(payload: ClientLogin):
    clients = supabase_select("clients", filters={"access_code": payload.access_code, "active": True}, limit=1)
    if not clients:
        raise HTTPException(status_code=401, detail="Invalid access code")
    client = clients[0]
    token = create_client_token(client["client_id"])
    return {
        "token":     token,
        "client_id": client["client_id"],
        "name":      client["name"],
        "plan":      client["plan"],
        "avatar":    client.get("avatar", client["name"][0].upper()),
        "contact":   client.get("contact_name", ""),
    }

@app.get("/auth/me")
async def me(client_id: str = Depends(get_client_id)):
    clients = supabase_select("clients", filters={"client_id": client_id}, limit=1)
    if not clients:
        raise HTTPException(status_code=404, detail="Client not found")
    return clients[0]

# ═══════════════════════════════════════════════════════════════════════
#  CORE ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════

@app.get("/status")
async def status():
    jobs = [{"id": j.id, "next_run": str(j.next_run_time) if j.next_run_time else "paused"} for j in scheduler.get_jobs()]
    return {"status": "online", "client_id": CLIENT_ID, "agents": ["jamie", "research", "writer", "linkedin", "reporting"], "scheduled_jobs": jobs, "version": "3.0.0"}

@app.post("/webhook/ceo")
async def ceo_webhook(payload: CEOInstruction, client_id: str = Depends(get_client_id)):
    supabase_insert("instructions", {"client_id": client_id, "target_agent": payload.agent, "instruction": payload.instruction, "issued_by": "ceo", "active": True})
    if payload.priority == "urgent":
        def run(name, cid):
            agents = get_agents(cid)
            if name in agents: agents[name].execute_cycle()
        asyncio.create_task(asyncio.to_thread(run, payload.agent, client_id))
        return {"status": "instruction_stored_and_triggered", "agent": payload.agent}
    return {"status": "instruction_stored", "agent": payload.agent, "note": "Agent will apply on next scheduled cycle"}

@app.post("/trigger/{agent_name}")
async def trigger_agent(agent_name: str, client_id: str = Depends(get_client_id)):
    valid = ["jamie", "research", "writer", "linkedin", "reporting"]
    if agent_name not in valid:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_name}' not found")
    def run(name, cid):
        agents = get_agents(cid)
        if name in agents: agents[name].execute_cycle()
    asyncio.create_task(asyncio.to_thread(run, agent_name, client_id))
    return {"status": "triggered", "agent": agent_name, "client_id": client_id}

@app.get("/briefing")
async def morning_briefing(client_id: str = Depends(get_client_id)):
    today   = date.today().isoformat()
    reports = supabase_select("reports",       filters={"client_id": client_id, "cycle_date": today}, limit=20)
    flags   = supabase_select("agent_flags",   filters={"client_id": client_id, "resolved": False},  limit=20)
    pending = supabase_select("content_queue", filters={"client_id": client_id, "approved": False, "published": False}, limit=10)
    leads   = supabase_select("leads",         filters={"client_id": client_id}, order_by="created_at", limit=5)
    hot_leads = [l for l in leads if l.get("score", 0) >= 8]
    return {"date": today, "client_id": client_id, "agent_reports": reports, "active_flags": flags,
            "pending_approval": len(pending), "hot_leads_today": hot_leads,
            "summary": _build_briefing_summary(reports, pending, hot_leads)}

@app.get("/agents/{name}/leads")
async def get_leads(name: str, limit: int = 50, client_id: str = Depends(get_client_id)):
    leads = supabase_select("leads", filters={"client_id": client_id}, order_by="created_at", limit=limit)
    return {"agent": name, "leads": leads, "count": len(leads)}

@app.get("/agents/{name}/report")
async def get_agent_report(name: str, client_id: str = Depends(get_client_id)):
    reports = supabase_select("reports", filters={"client_id": client_id, "agent_name": name}, order_by="cycle_date", limit=1)
    if not reports:
        raise HTTPException(status_code=404, detail=f"No reports for agent '{name}'")
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
    from datetime import timedelta
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    memory = supabase_select("agent_memory", filters={"client_id": client_id, "agent_name": agent_name}, order_by="cycle_date", limit=days)
    return {"agent": agent_name, "memory": [m for m in memory if m.get("cycle_date", "") >= cutoff]}

@app.get("/flags")
async def get_flags(resolved: bool = False, client_id: str = Depends(get_client_id)):
    flags = supabase_select("agent_flags", filters={"client_id": client_id, "resolved": resolved}, limit=50)
    return {"flags": flags, "count": len(flags)}

# ═══════════════════════════════════════════════════════════════════════
#  CONTENT COMMAND CENTRE
# ═══════════════════════════════════════════════════════════════════════

@app.get("/content/review-queue")
async def review_queue(client_id: str = Depends(get_client_id)):
    pending   = supabase_select("content_queue",    filters={"client_id": client_id, "status": "pending_review"},                    limit=50)
    approved  = supabase_select("content_queue",    filters={"client_id": client_id, "status": "approved",   "published": False},    limit=20)
    scheduled = supabase_select("content_queue",    filters={"client_id": client_id, "status": "scheduled"},                         limit=20)
    requests  = supabase_select("content_requests", filters={"client_id": client_id, "status": "pending"},                           limit=20)
    return {"pending_review": pending, "approved": approved, "scheduled": scheduled, "client_requests": requests,
            "counts": {"pending_review": len(pending), "approved": len(approved), "scheduled": len(scheduled), "client_requests": len(requests)}}

@app.post("/content/request")
async def submit_content_request(payload: ContentRequest, client_id: str = Depends(get_client_id)):
    row = supabase_insert("content_requests", {
        "client_id": client_id, "topic": payload.topic, "format": payload.format,
        "platform": payload.platform, "vibe": payload.vibe or None,
        "requested_by": payload.requested_by, "schedule_for": payload.schedule_for or None, "status": "pending",
    })
    if payload.requested_by == "ceo":
        request_data = {**(row or {}), "topic": payload.topic, "format": payload.format,
                        "platform": payload.platform, "vibe": payload.vibe or "",
                        "requested_by": payload.requested_by, "id": (row or {}).get("id"), "client_id": client_id}
        def run_writer(cid, req):
            w = WriterAgent(client_id=cid)
            w._write_on_demand(req)
        asyncio.create_task(asyncio.to_thread(run_writer, client_id, request_data))
        return {"status": "triggered", "message": "Writer is working on it — check the review queue in ~30 seconds", "request_id": (row or {}).get("id")}
    def run_writer_cycle(cid):
        w = WriterAgent(client_id=cid)
        w.execute_cycle()
    asyncio.create_task(asyncio.to_thread(run_writer_cycle, client_id))
    return {"status": "queued", "message": "Your request is in the queue", "request_id": (row or {}).get("id")}

@app.post("/content/approve")
async def approve_content(payload: ContentApproval, client_id: str = Depends(get_client_id)):
    content = supabase_select("content_queue", filters={"id": payload.content_id, "client_id": client_id}, limit=1)
    if not content:
        raise HTTPException(status_code=404, detail="Content not found")
    if payload.action == "approve":
        update_data = {"approved": True, "status": "approved", "ceo_note": payload.ceo_note or None, "approved_at": date.today().isoformat()}
        if payload.schedule_for:
            update_data["status"] = "scheduled"
            update_data["scheduled_for"] = payload.schedule_for
        supabase_update("content_queue", row_id=payload.content_id, data=update_data)
        if payload.post_immediately:
            def post(cid):
                li = LinkedInAgent(client_id=cid)
                li.execute_cycle()
            asyncio.create_task(asyncio.to_thread(post, client_id))
            return {"status": "approved_and_posting", "content_id": payload.content_id}
        return {"status": "approved", "content_id": payload.content_id}
    elif payload.action == "reject":
        supabase_update("content_queue", row_id=payload.content_id, data={"status": "rejected", "ceo_note": payload.ceo_note or None, "approved": False})
        return {"status": "rejected", "content_id": payload.content_id}
    elif payload.action == "request_changes":
        supabase_update("content_queue", row_id=payload.content_id, data={"status": "changes_requested", "ceo_note": payload.ceo_note or "", "approved": False})
        # Trigger Writer immediately with the change request
        note = payload.ceo_note or ""
        def redraft(cid, content_id, change_note):
            w = WriterAgent(client_id=cid)
            w._write_on_demand({
                "id": None,
                "topic": f"Redraft content {content_id}. CEO feedback: {change_note}",
                "format": "linkedin_post",
                "platform": "linkedin",
                "vibe": "",
                "requested_by": "ceo",
                "client_id": cid,
            })
        asyncio.create_task(asyncio.to_thread(redraft, client_id, payload.content_id, note))
        return {"status": "changes_requested", "content_id": payload.content_id}
    raise HTTPException(status_code=400, detail=f"Unknown action '{payload.action}'")

@app.patch("/content/edit")
async def edit_content(payload: ContentEdit, client_id: str = Depends(get_client_id)):
    supabase_update("content_queue", row_id=payload.content_id, data={"draft": payload.new_draft, "edited_by": payload.edited_by, "edited_at": date.today().isoformat(), "status": "pending_review", "approved": False})
    return {"status": "updated", "content_id": payload.content_id}

@app.post("/content/post-now/{content_id}")
async def post_now(content_id: str, client_id: str = Depends(get_client_id)):
    rows = supabase_select("content_queue", filters={"id": content_id, "client_id": client_id}, limit=1)
    if not rows:
        raise HTTPException(status_code=404, detail="Content not found")
    if not rows[0].get("approved"):
        raise HTTPException(status_code=400, detail="Content must be approved before posting")
    def post(cid):
        li = LinkedInAgent(client_id=cid)
        li.execute_cycle()
    asyncio.create_task(asyncio.to_thread(post, client_id))
    return {"status": "posting", "content_id": content_id}

@app.post("/content/override")
async def ceo_override(payload: CEOOverride, client_id: str = Depends(get_client_id)):
    supabase_insert("instructions", {"client_id": client_id, "target_agent": payload.agent, "instruction": payload.instruction, "issued_by": "ceo", "priority": "urgent", "active": True})
    if payload.trigger_immediately:
        def run(name, cid):
            agents = get_agents(cid)
            if name in agents: agents[name].execute_cycle()
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
        li = LinkedInAgent(client_id=cid)
        li.seed_evergreen_reserve()
    asyncio.create_task(asyncio.to_thread(seed, client_id))
    return {"status": "seeding", "client_id": client_id, "message": f"Generating 10 evergreen posts for {client_id}"}

# ═══════════════════════════════════════════════════════════════════════
#  BRAND PROFILE
# ═══════════════════════════════════════════════════════════════════════

@app.get("/brand/profile")
async def get_brand_profile(client_id: str = Depends(get_client_id)):
    profiles = supabase_select("brand_profiles", filters={"client_id": client_id}, limit=1)
    if not profiles:
        raise HTTPException(status_code=404, detail="Brand profile not found")
    return profiles[0]

# ═══════════════════════════════════════════════════════════════════════
#  ADMIN — CLIENT ONBOARDING
# ═══════════════════════════════════════════════════════════════════════

@app.post("/admin/onboard")
async def onboard_client(payload: ClientOnboard, _=Depends(verify_api_key)):
    existing = supabase_select("clients", filters={"client_id": payload.client_id}, limit=1)
    if existing:
        raise HTTPException(status_code=409, detail=f"Client ID '{payload.client_id}' already exists")
    supabase_insert("clients", {"client_id": payload.client_id, "name": payload.name, "access_code": payload.access_code,
        "plan": payload.plan, "avatar": payload.name[0].upper(), "contact_name": payload.contact_name,
        "contact_email": payload.contact_email, "monthly_fee": payload.monthly_fee, "active": True})
    supabase_insert("brand_profiles", {"client_id": payload.client_id, "name": payload.name,
        "tone": payload.brand_tone or f"Professional and authoritative voice for {payload.name}",
        "target_audience": payload.target_audience, "content_goals": payload.content_goals,
        "avoid": payload.avoid, "linkedin_style": payload.linkedin_style or "Short paragraphs, punchy opener, ends with question or CTA",
        "email_style": "Clear subject line, 3 paragraphs max, one CTA",
        "blog_style": "1,500-2,000 words, SEO-optimised, H2 subheadings"})
    if payload.linkedin_make_webhook and payload.linkedin_company_id:
        from agents.linkedin import add_route
        add_route(client_id=payload.client_id, platform="linkedin", account_type="company_page",
                  account_name=payload.name, webhook_url=payload.linkedin_make_webhook, notes="Auto-provisioned")
    token = create_client_token(payload.client_id)
    logger.info(f"New client onboarded: {payload.client_id} ({payload.name})")
    return {"status": "onboarded", "client_id": payload.client_id, "name": payload.name, "token": token}

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

# ═══════════════════════════════════════════════════════════════════════
#  PLATFORMS & ROUTES
# ═══════════════════════════════════════════════════════════════════════

@app.get("/platforms/routes")
async def list_routes(client: Optional[str] = None, client_id: str = Depends(get_client_id)):
    from agents.linkedin import get_all_routes
    routes = get_all_routes(client or client_id)
    return {"routes": routes, "count": len(routes)}

@app.post("/platforms/routes")
async def create_route(payload: RouteCreate, _=Depends(verify_api_key)):
    from agents.linkedin import add_route
    row = add_route(client_id=payload.client_id or CLIENT_ID, platform=payload.platform,
                    account_type=payload.account_type, account_name=payload.account_name,
                    webhook_url=payload.webhook_url, notes=payload.notes or "")
    if not row:
        raise HTTPException(status_code=500, detail="Failed to create route")
    return {"status": "created", "route": row}

@app.patch("/platforms/routes/{route_id}")
async def update_route(route_id: str, payload: RouteToggle, _=Depends(verify_api_key)):
    from agents.linkedin import toggle_route
    toggle_route(route_id, payload.active)
    return {"status": "updated", "route_id": route_id, "active": payload.active}

# ── Helpers ───────────────────────────────────────────────────────────
def _build_briefing_summary(reports, pending, hot_leads) -> str:
    parts = []
    if reports:   parts.append(f"{len(reports)} agents reported today.")
    if hot_leads: parts.append(f"{len(hot_leads)} hot lead(s) flagged.")
    if pending:   parts.append(f"{len(pending)} piece(s) awaiting approval.")
    return " ".join(parts) if parts else "No activity yet today."
