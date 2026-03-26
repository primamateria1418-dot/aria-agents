"""
main.py
ARIA™ — FastAPI Orchestrator
All agents scheduled here. CEO webhook, briefing, lead endpoints.
OUP International Ltd, 2026
"""

import os
import logging
from contextlib import asynccontextmanager
from datetime import date
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from agents.jamie import JAMIEHunter
from agents.research import ResearchAgent
from agents.writer import WriterAgent
from agents.reporting import ReportingAgent
from core.memory import supabase_select, supabase_insert

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("aria.main")

# ── Agent instances ───────────────────────────────────────────────────
CLIENT_ID = os.environ.get("CLIENT_ID", "aria_internal")

jamie    = JAMIEHunter(client_id=CLIENT_ID)
research = ResearchAgent(client_id=CLIENT_ID)
writer   = WriterAgent(client_id=CLIENT_ID)
reporting = ReportingAgent(client_id=CLIENT_ID)

# ── Scheduler ─────────────────────────────────────────────────────────
scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"ARIA starting — client: {CLIENT_ID}")

    # JAMIE — every 30 minutes
    scheduler.add_job(
        lambda: jamie.execute_cycle(),
        IntervalTrigger(minutes=30),
        id="jamie",
        replace_existing=True
    )

    # Research — 6am daily
    scheduler.add_job(
        lambda: research.execute_cycle(),
        CronTrigger(hour=6, minute=0),
        id="research",
        replace_existing=True
    )

    # Writer — 7am daily (after Research)
    scheduler.add_job(
        lambda: writer.execute_cycle(),
        CronTrigger(hour=7, minute=0),
        id="writer",
        replace_existing=True
    )

    # Reporting — Monday 7am
    scheduler.add_job(
        lambda: reporting.execute_cycle(),
        CronTrigger(day_of_week="mon", hour=7, minute=0),
        id="reporting",
        replace_existing=True
    )

    scheduler.start()
    logger.info("Scheduler started — all agents active")
    yield

    scheduler.shutdown()
    logger.info("ARIA shutdown complete")


# ── App ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="ARIA™ Orchestrator",
    description="OUP International Ltd — Automated Research Intelligence Agency",
    version="2.0.0",
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
    agent: str          # 'jamie', 'writer', 'research', 'all'
    instruction: str
    priority: str = "normal"


class TriggerRequest(BaseModel):
    agent: str
    client_id: str = None


# ═══════════════════════════════════════════════════════════════════════
#  ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════

@app.get("/status")
async def status():
    """Health check — lists all active agents and scheduler jobs."""
    jobs = [
        {
            "id": job.id,
            "next_run": str(job.next_run_time) if job.next_run_time else "paused"
        }
        for job in scheduler.get_jobs()
    ]
    return {
        "status": "online",
        "client_id": CLIENT_ID,
        "agents": ["jamie", "research", "writer", "reporting"],
        "scheduled_jobs": jobs,
        "version": "2.0.0"
    }


@app.post("/webhook/ceo")
async def ceo_webhook(payload: CEOInstruction, _=Depends(verify_api_key)):
    """
    CEO Dashboard → agent instructions.
    Writes to instructions table. Agent reads at next cycle start.
    """
    supabase_insert("instructions", {
        "client_id": CLIENT_ID,
        "target_agent": payload.agent,
        "instruction": payload.instruction,
        "issued_by": "ceo",
        "active": True
    })

    # For urgent instructions, also trigger immediate cycle
    if payload.priority == "urgent":
        agent_map = {
            "jamie": jamie,
            "research": research,
            "writer": writer,
            "reporting": reporting
        }
        if payload.agent in agent_map:
            import asyncio
            asyncio.create_task(
                asyncio.to_thread(agent_map[payload.agent].execute_cycle)
            )
            return {
                "status": "instruction_stored_and_triggered",
                "agent": payload.agent,
                "instruction": payload.instruction
            }

    return {
        "status": "instruction_stored",
        "agent": payload.agent,
        "instruction": payload.instruction,
        "note": "Agent will apply on next scheduled cycle"
    }


@app.post("/trigger/{agent_name}")
async def trigger_agent(agent_name: str, _=Depends(verify_api_key)):
    """Manually trigger an agent cycle immediately."""
    import asyncio
    agent_map = {
        "jamie": jamie,
        "research": research,
        "writer": writer,
        "reporting": reporting
    }
    if agent_name not in agent_map:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_name}' not found")

    asyncio.create_task(
        asyncio.to_thread(agent_map[agent_name].execute_cycle)
    )
    return {"status": "triggered", "agent": agent_name}


@app.get("/agents/{name}/leads")
async def get_leads(name: str, limit: int = 50, _=Depends(verify_api_key)):
    """Live leads for a specific agent (JAMIE)."""
    leads = supabase_select(
        "leads",
        filters={"client_id": CLIENT_ID},
        order_by="created_at",
        limit=limit
    )
    return {"agent": name, "leads": leads, "count": len(leads)}


@app.get("/agents/{name}/report")
async def get_agent_report(name: str, _=Depends(verify_api_key)):
    """Latest cycle report for a specific agent."""
    reports = supabase_select(
        "reports",
        filters={"client_id": CLIENT_ID, "agent_name": name},
        order_by="cycle_date",
        limit=1
    )
    if not reports:
        raise HTTPException(status_code=404, detail=f"No reports for agent '{name}'")
    return reports[0]


@app.get("/briefing")
async def morning_briefing(_=Depends(verify_api_key)):
    """
    CEO morning briefing — all agent reports from today.
    Used by the CEO Dashboard morning briefing feed.
    """
    today = date.today().isoformat()
    reports = supabase_select(
        "reports",
        filters={"client_id": CLIENT_ID, "cycle_date": today},
        order_by="cycle_date",
        limit=20
    )

    # Get pending flags (unresolved inter-agent communications)
    flags = supabase_select(
        "agent_flags",
        filters={"client_id": CLIENT_ID, "resolved": False},
        limit=20
    )

    # Get content pending approval
    pending = supabase_select(
        "content_queue",
        filters={"client_id": CLIENT_ID, "approved": False, "published": False},
        limit=10
    )

    # Get recent hot leads
    leads = supabase_select(
        "leads",
        filters={"client_id": CLIENT_ID},
        order_by="created_at",
        limit=5
    )
    hot_leads = [l for l in leads if l.get("score", 0) >= 8]

    return {
        "date": today,
        "client_id": CLIENT_ID,
        "agent_reports": reports,
        "active_flags": flags,
        "pending_approval": len(pending),
        "hot_leads_today": hot_leads,
        "summary": _build_briefing_summary(reports, pending, hot_leads)
    }


def _build_briefing_summary(reports, pending, hot_leads) -> str:
    parts = []
    if reports:
        parts.append(f"{len(reports)} agents reported today.")
    if hot_leads:
        parts.append(f"{len(hot_leads)} hot lead(s) flagged.")
    if pending:
        parts.append(f"{len(pending)} piece(s) awaiting approval.")
    return " ".join(parts) if parts else "No activity yet today."


@app.get("/content/queue")
async def content_queue(approved: bool = None, _=Depends(verify_api_key)):
    """Content queue — filterable by approval status."""
    filters = {"client_id": CLIENT_ID}
    if approved is not None:
        filters["approved"] = approved

    content = supabase_select("content_queue", filters=filters, limit=50)
    return {"content": content, "count": len(content)}


@app.get("/memory/{agent_name}")
async def get_agent_memory(agent_name: str, days: int = 7, _=Depends(verify_api_key)):
    """Agent memory history — last N days."""
    from datetime import timedelta
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    memory = supabase_select(
        "agent_memory",
        filters={"client_id": CLIENT_ID, "agent_name": agent_name},
        order_by="cycle_date",
        limit=days
    )
    return {
        "agent": agent_name,
        "memory": [m for m in memory if m.get("cycle_date", "") >= cutoff]
    }


@app.get("/flags")
async def get_flags(resolved: bool = False, _=Depends(verify_api_key)):
    """Inter-agent flag queue."""
    flags = supabase_select(
        "agent_flags",
        filters={"client_id": CLIENT_ID, "resolved": resolved},
        limit=50
    )
    return {"flags": flags, "count": len(flags)}
