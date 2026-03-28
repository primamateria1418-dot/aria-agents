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

from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from agents.jamie import JAMIEHunter
from agents.research import ResearchAgent
from agents.writer import WriterAgent
from agents.reporting import ReportingAgent
from agents.linkedin import LinkedInAgent
from agents.publisher import Publisher
from core.memory import supabase_select, supabase_insert, supabase_update

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("aria.main")

# ── Agent instances ───────────────────────────────────────────────────
CLIENT_ID = os.environ.get("CLIENT_ID", "aria_internal")

jamie     = JAMIEHunter()
research  = ResearchAgent(client_id=CLIENT_ID)
writer    = WriterAgent(client_id=CLIENT_ID)
reporting = ReportingAgent(client_id=CLIENT_ID)
linkedin  = LinkedInAgent(client_id=CLIENT_ID)

# ── Scheduler ─────────────────────────────────────────────────────────
scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"ARIA starting — client: {CLIENT_ID}")

    # JAMIE — every 30 minutes
    scheduler.add_job(
        lambda: jamie.execute_cycle(),
        IntervalTrigger(minutes=30),
        id="jamie", replace_existing=True
    )

    # Research — 6am daily
    scheduler.add_job(
        lambda: research.execute_cycle(),
        CronTrigger(hour=6, minute=0),
        id="research", replace_existing=True
    )

    # Writer — 7am daily
    scheduler.add_job(
        lambda: writer.execute_cycle(),
        CronTrigger(hour=7, minute=0),
        id="writer", replace_existing=True
    )

    # LinkedIn — 12pm daily
    scheduler.add_job(
        lambda: linkedin.execute_cycle(),
        CronTrigger(hour=12, minute=0),
        id="linkedin", replace_existing=True
    )

    # Reporting — Monday 7am
    scheduler.add_job(
        lambda: reporting.execute_cycle(),
        CronTrigger(day_of_week="mon", hour=7, minute=0),
        id="reporting", replace_existing=True
    )

    scheduler.start()
    logger.info("Scheduler started — Research 6am · Writer 7am · LinkedIn 12pm · Reporting Mon 7am")
    yield

    scheduler.shutdown()
    logger.info("ARIA shutdown complete")


# ── App ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="ARIA™ Orchestrator",
    description="OUP International Ltd — Automated Research Intelligence Agency",
    version="3.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Auth ──────────────────────────────────────────────────────────────
ARIA_API_KEY = os.environ.get("ARIA_API_KEY", "")


def verify_api_key(x_api_key: str = Header(default="")):
    if ARIA_API_KEY and x_api_key != ARIA_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return x_api_key


# ── Models ────────────────────────────────────────────────────────────
class CEOInstruction(BaseModel):
    agent: str
    instruction: str
    priority: str = "normal"


class ContentRequest(BaseModel):
    topic: str
    format: str = "linkedin_post"       # linkedin_post | email | blog | thread
    platform: str = "linkedin"
    vibe: Optional[str] = ""            # authority | education | social_proof | etc.
    requested_by: str = "client"        # client | ceo
    schedule_for: Optional[str] = None  # ISO date string — None = no schedule


class ContentApproval(BaseModel):
    content_id: str
    action: str          # approve | reject | request_changes
    ceo_note: Optional[str] = ""
    post_immediately: bool = False
    schedule_for: Optional[str] = None  # ISO datetime for scheduling
    publish_profiles: Optional[list] = None  # ["company"] | ["company","ceo"] | etc.


class ContentEdit(BaseModel):
    content_id: str
    new_draft: str
    edited_by: str = "ceo"


class CEOOverride(BaseModel):
    agent: str
    instruction: str
    trigger_immediately: bool = True


# ═══════════════════════════════════════════════════════════════════════
#  EXISTING ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════

@app.get("/status")
async def status():
    jobs = [
        {"id": j.id, "next_run": str(j.next_run_time) if j.next_run_time else "paused"}
        for j in scheduler.get_jobs()
    ]
    return {
        "status":         "online",
        "client_id":      CLIENT_ID,
        "agents":         ["jamie", "research", "writer", "linkedin", "reporting"],
        "scheduled_jobs": jobs,
        "version":        "3.0.0"
    }


@app.post("/webhook/ceo")
async def ceo_webhook(payload: CEOInstruction, _=Depends(verify_api_key)):
    supabase_insert("instructions", {
        "client_id":    CLIENT_ID,
        "target_agent": payload.agent,
        "instruction":  payload.instruction,
        "issued_by":    "ceo",
        "active":       True
    })

    agent_map = {
        "jamie": jamie, "research": research,
        "writer": writer, "linkedin": linkedin, "reporting": reporting
    }

    if payload.priority == "urgent" and payload.agent in agent_map:
        asyncio.create_task(
            asyncio.to_thread(agent_map[payload.agent].execute_cycle)
        )
        return {"status": "instruction_stored_and_triggered", "agent": payload.agent}

    return {
        "status": "instruction_stored",
        "agent":  payload.agent,
        "note":   "Agent will apply on next scheduled cycle"
    }


@app.post("/trigger/{agent_name}")
async def trigger_agent(agent_name: str, _=Depends(verify_api_key)):
    agent_map = {
        "jamie": jamie, "research": research,
        "writer": writer, "linkedin": linkedin, "reporting": reporting
    }
    if agent_name not in agent_map:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_name}' not found")

    asyncio.create_task(asyncio.to_thread(agent_map[agent_name].execute_cycle))
    return {"status": "triggered", "agent": agent_name}


@app.get("/agents/{name}/leads")
async def get_leads(name: str, limit: int = 50, _=Depends(verify_api_key)):
    leads = supabase_select("leads", filters={"client_id": CLIENT_ID}, order_by="created_at", limit=limit)
    return {"agent": name, "leads": leads, "count": len(leads)}


@app.get("/agents/{name}/report")
async def get_agent_report(name: str, _=Depends(verify_api_key)):
    reports = supabase_select(
        "reports",
        filters={"client_id": CLIENT_ID, "agent_name": name},
        order_by="cycle_date", limit=1
    )
    if not reports:
        raise HTTPException(status_code=404, detail=f"No reports for agent '{name}'")
    return reports[0]


@app.get("/briefing")
async def morning_briefing(_=Depends(verify_api_key)):
    today   = date.today().isoformat()
    reports = supabase_select("reports", filters={"client_id": CLIENT_ID, "cycle_date": today}, limit=20)
    flags   = supabase_select("agent_flags", filters={"client_id": CLIENT_ID, "resolved": False}, limit=20)
    pending = supabase_select("content_queue", filters={"client_id": CLIENT_ID, "approved": False, "published": False}, limit=10)
    leads   = supabase_select("leads", filters={"client_id": CLIENT_ID}, order_by="created_at", limit=5)
    hot_leads = [l for l in leads if l.get("score", 0) >= 8]

    return {
        "date":             today,
        "client_id":        CLIENT_ID,
        "agent_reports":    reports,
        "active_flags":     flags,
        "pending_approval": len(pending),
        "hot_leads_today":  hot_leads,
        "summary":          _build_briefing_summary(reports, pending, hot_leads)
    }


@app.get("/content/queue")
async def content_queue(approved: bool = None, _=Depends(verify_api_key)):
    filters = {"client_id": CLIENT_ID}
    if approved is not None:
        filters["approved"] = approved
    content = supabase_select("content_queue", filters=filters, limit=50)
    return {"content": content, "count": len(content)}


@app.get("/memory/{agent_name}")
async def get_agent_memory(agent_name: str, days: int = 7, _=Depends(verify_api_key)):
    from datetime import timedelta
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    memory = supabase_select(
        "agent_memory",
        filters={"client_id": CLIENT_ID, "agent_name": agent_name},
        order_by="cycle_date", limit=days
    )
    return {"agent": agent_name, "memory": [m for m in memory if m.get("cycle_date", "") >= cutoff]}


@app.get("/flags")
async def get_flags(resolved: bool = False, _=Depends(verify_api_key)):
    flags = supabase_select("agent_flags", filters={"client_id": CLIENT_ID, "resolved": resolved}, limit=50)
    return {"flags": flags, "count": len(flags)}


# ═══════════════════════════════════════════════════════════════════════
#  NEW: CONTENT COMMAND CENTRE ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════

@app.get("/content/review-queue")
async def review_queue(_=Depends(verify_api_key)):
    """
    Dashboard content review queue.
    Returns all content pending CEO approval, grouped by status.
    """
    pending  = supabase_select("content_queue", filters={"client_id": CLIENT_ID, "status": "pending_review"}, limit=50)
    approved = supabase_select("content_queue", filters={"client_id": CLIENT_ID, "status": "approved", "published": False}, limit=20)
    scheduled= supabase_select("content_queue", filters={"client_id": CLIENT_ID, "status": "scheduled"}, limit=20)
    requests = supabase_select("content_requests", filters={"client_id": CLIENT_ID, "status": "pending"}, limit=20)

    return {
        "pending_review": pending,
        "approved":       approved,
        "scheduled":      scheduled,
        "client_requests":requests,
        "counts": {
            "pending_review": len(pending),
            "approved":       len(approved),
            "scheduled":      len(scheduled),
            "client_requests":len(requests),
        }
    }


@app.post("/content/request")
async def submit_content_request(payload: ContentRequest, _=Depends(verify_api_key)):
    """
    Client or CEO submits a content request.
    Saves to content_requests table → Writer picks it up immediately (urgent) or next cycle.
    """
    row = supabase_insert("content_requests", {
        "client_id":    CLIENT_ID,
        "topic":        payload.topic,
        "format":       payload.format,
        "platform":     payload.platform,
        "vibe":         payload.vibe or None,
        "requested_by": payload.requested_by,
        "schedule_for": payload.schedule_for or None,
        "status":       "pending",
    })

    # Trigger Writer immediately for CEO requests
    if payload.requested_by == "ceo":
        asyncio.create_task(
            asyncio.to_thread(writer._write_on_demand, {
                **(row or {}),
                "topic":        payload.topic,
                "format":       payload.format,
                "platform":     payload.platform,
                "vibe":         payload.vibe or "",
                "requested_by": payload.requested_by,
                "id":           (row or {}).get("id"),
            })
        )
        return {
            "status":     "triggered",
            "message":    "Writer is working on it — check the review queue in ~30 seconds",
            "request_id": (row or {}).get("id"),
        }

    # For client requests — trigger Writer cycle
    asyncio.create_task(asyncio.to_thread(writer.execute_cycle))

    return {
        "status":     "queued",
        "message":    "Your request is in the queue — Writer will draft this shortly",
        "request_id": (row or {}).get("id"),
    }


@app.post("/content/approve")
async def approve_content(payload: ContentApproval, _=Depends(verify_api_key)):
    """
    CEO approves, rejects, or requests changes on a content piece.
    If post_immediately=True → triggers LinkedIn agent right away.
    If schedule_for is set → marks as scheduled for that datetime.
    """
    content = supabase_select("content_queue", filters={"id": payload.content_id}, limit=1)
    if not content:
        raise HTTPException(status_code=404, detail="Content not found")

    if payload.action == "approve":
        update_data = {
            "approved":    True,
            "status":      "approved",
            "ceo_note":    payload.ceo_note or None,
            "approved_at": date.today().isoformat(),
        }

        if payload.schedule_for:
            update_data["status"]        = "scheduled"
            update_data["scheduled_for"] = payload.schedule_for

        supabase_update("content_queue", row_id=payload.content_id, data=update_data)

        if payload.post_immediately:
            asyncio.create_task(asyncio.to_thread(linkedin.execute_cycle))
            return {"status": "approved_and_posting", "content_id": payload.content_id}

        return {"status": "approved", "content_id": payload.content_id}

    elif payload.action == "reject":
        supabase_update("content_queue", row_id=payload.content_id, data={
            "status":   "rejected",
            "ceo_note": payload.ceo_note or None,
            "approved": False,
        })
        return {"status": "rejected", "content_id": payload.content_id}

    elif payload.action == "request_changes":
        supabase_update("content_queue", row_id=payload.content_id, data={
            "status":   "changes_requested",
            "ceo_note": payload.ceo_note or "",
            "approved": False,
        })
        # Flag Writer with the change request
        writer.raise_flag(
            "writer",
            f"CEO requests changes on content {payload.content_id}: {payload.ceo_note}",
            priority="urgent",
            context={"content_id": payload.content_id, "note": payload.ceo_note}
        )
        return {"status": "changes_requested", "content_id": payload.content_id}

    raise HTTPException(status_code=400, detail=f"Unknown action '{payload.action}'")


@app.patch("/content/edit")
async def edit_content(payload: ContentEdit, _=Depends(verify_api_key)):
    """
    CEO directly edits a draft before approving.
    Overwrites the draft field and marks as pending_review.
    """
    supabase_update("content_queue", row_id=payload.content_id, data={
        "draft":      payload.new_draft,
        "edited_by":  payload.edited_by,
        "edited_at":  date.today().isoformat(),
        "status":     "pending_review",
        "approved":   False,
    })
    return {"status": "updated", "content_id": payload.content_id}


@app.post("/content/post-now/{content_id}")
async def post_now(content_id: str, _=Depends(verify_api_key)):
    """
    CEO posts a specific approved piece immediately via LinkedIn agent.
    """
    content = supabase_select("content_queue", filters={"id": content_id, "client_id": CLIENT_ID}, limit=1)
    if not content:
        raise HTTPException(status_code=404, detail="Content not found")

    if not content[0].get("approved"):
        raise HTTPException(status_code=400, detail="Content must be approved before posting")

    asyncio.create_task(asyncio.to_thread(linkedin.execute_cycle))
    return {"status": "posting", "content_id": content_id, "message": "LinkedIn agent triggered"}


@app.post("/content/override")
async def ceo_override(payload: CEOOverride, _=Depends(verify_api_key)):
    """
    CEO makes an immediate demand of any agent.
    Stores instruction and optionally triggers the agent right now.
    """
    supabase_insert("instructions", {
        "client_id":    CLIENT_ID,
        "target_agent": payload.agent,
        "instruction":  payload.instruction,
        "issued_by":    "ceo",
        "priority":     "urgent",
        "active":       True,
    })

    agent_map = {
        "jamie": jamie, "research": research,
        "writer": writer, "linkedin": linkedin, "reporting": reporting
    }

    if payload.trigger_immediately and payload.agent in agent_map:
        asyncio.create_task(
            asyncio.to_thread(agent_map[payload.agent].execute_cycle)
        )
        return {
            "status":  "override_issued_and_triggered",
            "agent":   payload.agent,
            "message": f"Instruction sent. {payload.agent.capitalize()} is running now."
        }

    return {
        "status":  "override_issued",
        "agent":   payload.agent,
        "message": f"Instruction stored. {payload.agent.capitalize()} will apply on next cycle."
    }


@app.get("/content/schedule")
async def get_schedule(_=Depends(verify_api_key)):
    """
    Returns the upcoming content schedule — all approved/scheduled pieces.
    Used by dashboard calendar view.
    """
    scheduled = supabase_select(
        "content_queue",
        filters={"client_id": CLIENT_ID, "approved": True, "published": False},
        order_by="scheduled_for",
        limit=30
    )
    return {"schedule": scheduled, "count": len(scheduled)}


@app.post("/linkedin/seed-evergreen")
async def seed_evergreen(_=Depends(verify_api_key)):
    """
    Trigger LinkedIn agent to seed the evergreen reserve.
    Called once on first deployment.
    """
    asyncio.create_task(asyncio.to_thread(linkedin.seed_evergreen_reserve))
    return {"status": "seeding", "message": "Writer is generating 10 evergreen posts — check evergreen_reserve table in ~60 seconds"}


@app.get("/publish/profiles")
async def get_publish_profiles(platform: str = "linkedin", _=Depends(verify_api_key)):
    """
    Returns available publish profiles for the current client.
    Used by dashboard profile selector in approval panel.
    Scaleable — add new platforms/profiles in publisher.py DEFAULT_PROFILES.
    """
    pub = Publisher(client_id=CLIENT_ID)
    profiles = pub.get_profiles(platform=platform if platform != "all" else None)
    return {
        "client_id": CLIENT_ID,
        "platform":  platform,
        "profiles":  profiles,
        "count":     len(profiles),
    }


# ── Helpers ───────────────────────────────────────────────────────────
def _build_briefing_summary(reports, pending, hot_leads) -> str:
    parts = []
    if reports:    parts.append(f"{len(reports)} agents reported today.")
    if hot_leads:  parts.append(f"{len(hot_leads)} hot lead(s) flagged.")
    if pending:    parts.append(f"{len(pending)} piece(s) awaiting approval.")
    return " ".join(parts) if parts else "No activity yet today."

# ═══════════════════════════════════════════════════════════════════════
#  PLATFORMS & ACCOUNTS — Zapier route management
# ═══════════════════════════════════════════════════════════════════════

class RouteCreate(BaseModel):
    platform:     str
    account_type: str
    account_name: str
    webhook_url:  str
    client_id:    Optional[str] = None
    notes:        Optional[str] = ""

class RouteToggle(BaseModel):
    active: bool


@app.get("/platforms/routes")
async def list_routes(client: Optional[str] = None, _=Depends(verify_api_key)):
    """All Zapier routes — optionally filter by client."""
    from agents.linkedin import get_all_routes
    cid    = client or CLIENT_ID
    routes = get_all_routes(cid)
    return {"routes": routes, "count": len(routes)}


@app.post("/platforms/routes")
async def create_route(payload: RouteCreate, _=Depends(verify_api_key)):
    """Add a new Zapier route (connect a new account/platform)."""
    from agents.linkedin import add_route
    row = add_route(
        client_id    = payload.client_id or CLIENT_ID,
        platform     = payload.platform,
        account_type = payload.account_type,
        account_name = payload.account_name,
        webhook_url  = payload.webhook_url,
        notes        = payload.notes or "",
    )
    if not row:
        raise HTTPException(status_code=500, detail="Failed to create route")
    return {"status": "created", "route": row}


@app.patch("/platforms/routes/{route_id}")
async def update_route(route_id: str, payload: RouteToggle, _=Depends(verify_api_key)):
    """Enable or pause a Zapier route."""
    from agents.linkedin import toggle_route
    row = toggle_route(route_id, payload.active)
    return {"status": "updated", "route_id": route_id, "active": payload.active}
