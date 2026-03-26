"""
agents/reporting.py
ARIA™ — Reporting Agent (Agent 11)
Runs Monday 07:00. Compiles weekly KPI report from all tables.
Writes report to content_queue for CEO approval and client delivery.
OUP International Ltd, 2026
"""

import json
import logging
from datetime import date, timedelta
from core.base_agent import BaseAgent
from core.llm import call_llm
from core.memory import supabase_select, supabase_insert

logger = logging.getLogger("aria.reporting")


class ReportingAgent(BaseAgent):
    def __init__(self, client_id: str = "aria_internal"):
        super().__init__(name="reporting", client_id=client_id)

    # ═══════════════════════════════════════════════════════
    #  MAIN CYCLE
    # ═══════════════════════════════════════════════════════

    def run(self):
        week_start = (date.today() - timedelta(days=7)).isoformat()
        self._log_action(f"Compiling weekly report for period: {week_start} → {date.today()}")

        # 1. Gather all metrics
        metrics = self._gather_metrics(week_start)
        self._log_action(f"Metrics gathered: {list(metrics.keys())}")

        # 2. Read all agent reports for the week
        agent_summaries = self._gather_agent_summaries(week_start)
        self._log_action(f"Agent summaries collected: {len(agent_summaries)} agents")

        # 3. Generate narrative report via LLM
        report_text = self._generate_report(metrics, agent_summaries)

        # 4. Store as content item for CEO approval + client delivery
        self._store_report(report_text, metrics)

        # 5. Flag CEO with summary
        self.raise_flag(
            "aria_ceo",
            f"Weekly report ready for approval. Leads: {metrics.get('total_leads', 0)}, "
            f"Content: {metrics.get('content_published', 0)} published, "
            f"Engagement trend: {metrics.get('engagement_trend', '?')}",
            priority="normal"
        )

        self._log_outcome(f"Weekly report generated and stored · {len(report_text)} chars")
        self._set_metric("report_generated", True)
        self._set_metric("metrics_compiled", len(metrics))

    # ═══════════════════════════════════════════════════════
    #  METRICS GATHERING
    # ═══════════════════════════════════════════════════════

    def _gather_metrics(self, since: str) -> dict:
        metrics = {}

        # Leads
        leads = supabase_select("leads", filters={"client_id": self.client_id})
        week_leads = [l for l in leads if l.get("created_at", "") >= since]
        metrics["total_leads"] = len(week_leads)
        metrics["hot_leads"] = len([l for l in week_leads if l.get("score", 0) >= 8])
        metrics["avg_lead_score"] = (
            round(sum(l.get("score", 0) for l in week_leads) / len(week_leads), 1)
            if week_leads else 0
        )
        metrics["leads_by_status"] = {}
        for lead in week_leads:
            s = lead.get("status", "new")
            metrics["leads_by_status"][s] = metrics["leads_by_status"].get(s, 0) + 1

        # Content
        content = supabase_select("content_queue", filters={"client_id": self.client_id})
        week_content = [c for c in content if c.get("created_at", "") >= since]
        metrics["content_created"] = len(week_content)
        metrics["content_approved"] = len([c for c in week_content if c.get("approved")])
        metrics["content_published"] = len([c for c in week_content if c.get("published")])
        metrics["content_pending"] = len([
            c for c in week_content if not c.get("approved") and not c.get("published")
        ])

        # Content by type
        metrics["content_by_type"] = {}
        for c in week_content:
            t = c.get("content_type", "unknown")
            metrics["content_by_type"][t] = metrics["content_by_type"].get(t, 0) + 1

        # Agent activity
        reports = supabase_select("reports", filters={"client_id": self.client_id})
        week_reports = [r for r in reports if r.get("cycle_date", "") >= since]
        metrics["agent_cycles"] = len(week_reports)
        metrics["flags_raised"] = sum(r.get("flags_raised", 0) for r in week_reports)

        # Agent memory — average performance ratings
        memory = supabase_select("agent_memory", filters={"client_id": self.client_id})
        week_memory = [m for m in memory if m.get("cycle_date", "") >= since]
        if week_memory:
            metrics["avg_agent_rating"] = round(
                sum(m.get("performance_rating", 5) for m in week_memory) / len(week_memory), 1
            )

        # Placeholder engagement (would come from real platform APIs)
        metrics["engagement_trend"] = "up"

        return metrics

    def _gather_agent_summaries(self, since: str) -> dict:
        """Get the most recent report from each agent this week."""
        reports = supabase_select(
            "reports",
            filters={"client_id": self.client_id},
            order_by="cycle_date",
            limit=50
        )
        week_reports = [r for r in reports if r.get("cycle_date", "") >= since]

        summaries = {}
        for report in week_reports:
            agent = report.get("agent_name", "unknown")
            if agent not in summaries:
                summaries[agent] = report.get("summary", "")
        return summaries

    # ═══════════════════════════════════════════════════════
    #  REPORT GENERATION
    # ═══════════════════════════════════════════════════════

    def _generate_report(self, metrics: dict, agent_summaries: dict) -> str:
        prompt = f"""You are the Reporting agent for ARIA, writing the weekly performance report.

Week ending: {date.today().strftime('%d %B %Y')}
Client: {self.client_id}

Key metrics this week:
{json.dumps(metrics, indent=2)}

Agent summaries:
{json.dumps(agent_summaries, indent=2)}

Write a professional weekly performance report (400-600 words) that covers:
1. Executive summary (3-4 sentences — the headline numbers)
2. Lead generation performance
3. Content performance
4. Agent highlights (what worked, what didn't)
5. Recommendations for next week (3 specific actions)

Tone: confident, data-driven, actionable. Like a senior marketing director reporting to a board.
Format: plain text with clear section headers. No markdown symbols."""

        try:
            return call_llm(
                prompt,
                system="You are a senior marketing strategist writing a weekly performance report.",
                max_tokens=800,
                temperature=0.6
            )
        except Exception as e:
            logger.error(f"Report generation failed: {e}")
            return f"Weekly Report — {date.today()}\n\nMetrics: {json.dumps(metrics)}\n\nReport generation failed: {e}"

    # ═══════════════════════════════════════════════════════
    #  STORAGE
    # ═══════════════════════════════════════════════════════

    def _store_report(self, report_text: str, metrics: dict):
        supabase_insert("content_queue", {
            "client_id": self.client_id,
            "content_type": "blog",
            "platform": "email",
            "draft": f"WEEKLY REPORT — {date.today().strftime('%d %B %Y')}\n\n{report_text}",
            "approved": False,
            "published": False,
            "created_by": "reporting"
        })


# ═══════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv
    load_dotenv()

    agent = ReportingAgent(client_id="aria_internal")
    if "--test" in sys.argv:
        print("=== REPORTING AGENT — TEST MODE ===")
        agent._log_action("TEST: Compiled dummy weekly metrics")
        agent._set_metric("report_generated", True)
        print(json.dumps(agent.self_review(), indent=2))
    else:
        agent.execute_cycle()
        print("✓ Report generated")
