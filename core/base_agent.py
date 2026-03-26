"""
core/base_agent.py
ARIA™ — BaseAgent class
All 11 agents inherit from this.
OUP International Ltd, 2026
"""

import os
import json
import logging
from datetime import date, datetime
from typing import Optional
from core.memory import (
    supabase_insert, supabase_select, supabase_update
)
from core.llm import call_llm

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")


class BaseAgent:
    def __init__(self, name: str, client_id: str = "aria_internal"):
        self.name = name
        self.client_id = client_id
        self.logger = logging.getLogger(f"aria.{name}")
        self._actions: list[str] = []
        self._outcomes: list[str] = []
        self._metrics: dict = {}

    # ═══════════════════════════════════════════════════════
    #  CORE — override run() in every subclass
    # ═══════════════════════════════════════════════════════

    def run(self):
        raise NotImplementedError("Each agent must implement run()")

    def execute_cycle(self):
        """Full cycle wrapper — called by FastAPI scheduler."""
        self.logger.info(f"Cycle start — client: {self.client_id}")
        self._actions = []
        self._outcomes = []
        self._metrics = {}

        try:
            self.run()
        except Exception as e:
            self.logger.error(f"Cycle error: {e}", exc_info=True)
            self.log("error", str(e))

        reflection = self.self_review()
        self.post_report(reflection)
        self.logger.info("Cycle complete")
        return reflection

    # ═══════════════════════════════════════════════════════
    #  LEARNING — self_review and peer_review
    # ═══════════════════════════════════════════════════════

    def self_review(self) -> dict:
        """LLM reflects on this cycle → stores to agent_memory."""
        history = self.read_memory(days=7)
        history_text = json.dumps(history[-3:], indent=2) if history else "No prior history."

        flags_received = self.check_flags(resolved=False)
        flags_text = "\n".join(
            [f"- From {f['from_agent']}: {f['message']}" for f in flags_received]
        ) if flags_received else "None."

        prompt = f"""You are {self.name.upper()}, an AI agent in the ARIA marketing system.

Reflect on your performance this cycle.

Actions taken this cycle:
{chr(10).join(self._actions) if self._actions else 'None recorded.'}

Outcomes:
{chr(10).join(self._outcomes) if self._outcomes else 'None recorded.'}

Flags received from peers:
{flags_text}

Your last 3 cycles of memory:
{history_text}

Respond ONLY with a JSON object — no markdown, no explanation:
{{
  "performance_rating": <1-10 integer>,
  "what_worked": "<one sentence>",
  "what_to_change": "<one sentence>",
  "peer_observations": "<response to any peer flags, or 'None'>",
  "next_cycle_intent": "<one sentence plan for next cycle>"
}}"""

        try:
            raw = call_llm(prompt, max_tokens=400)
            # Strip any markdown fences
            clean = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            reflection = json.loads(clean)
        except Exception as e:
            self.logger.warning(f"self_review LLM failed: {e} — using default")
            reflection = {
                "performance_rating": 5,
                "what_worked": "Cycle completed.",
                "what_to_change": "LLM reflection unavailable this cycle.",
                "peer_observations": "None",
                "next_cycle_intent": "Continue standard operation."
            }

        # Resolve flags we responded to
        for f in flags_received:
            self.resolve_flag(f["id"])

        # Store to agent_memory
        supabase_insert("agent_memory", {
            "client_id": self.client_id,
            "agent_name": self.name,
            "cycle_date": date.today().isoformat(),
            "actions_taken": "\n".join(self._actions),
            "outcomes": "\n".join(self._outcomes),
            "performance_rating": reflection.get("performance_rating", 5),
            "what_worked": reflection.get("what_worked", ""),
            "what_to_change": reflection.get("what_to_change", ""),
            "peer_observations": reflection.get("peer_observations", ""),
            "next_cycle_intent": reflection.get("next_cycle_intent", ""),
            "raw_reflection": reflection
        })

        self.logger.info(f"self_review complete — rating: {reflection.get('performance_rating')}")
        return reflection

    def read_memory(self, days: int = 7) -> list:
        """Read own performance history."""
        from datetime import timedelta
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        rows = supabase_select(
            "agent_memory",
            filters={"client_id": self.client_id, "agent_name": self.name},
            order_by="cycle_date",
            limit=days
        )
        return [r for r in rows if r.get("cycle_date", "") >= cutoff]

    def read_peer_memory(self, peer: str, days: int = 7) -> list:
        """Read another agent's memory."""
        from datetime import timedelta
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        rows = supabase_select(
            "agent_memory",
            filters={"client_id": self.client_id, "agent_name": peer},
            order_by="cycle_date",
            limit=days
        )
        return [r for r in rows if r.get("cycle_date", "") >= cutoff]

    def peer_review(self, peer_names: list[str]):
        """Weekly cross-agent review — reads all peers, raises flags."""
        self.logger.info(f"peer_review starting — peers: {peer_names}")

        peer_summaries = {}
        for peer in peer_names:
            mem = self.read_peer_memory(peer, days=7)
            if mem:
                peer_summaries[peer] = {
                    "avg_rating": sum(m.get("performance_rating", 5) for m in mem) / len(mem),
                    "recent_intent": mem[-1].get("next_cycle_intent", ""),
                    "recent_changes": mem[-1].get("what_to_change", "")
                }

        prompt = f"""You are {self.name.upper()}, an AI agent in the ARIA marketing system.

Review your peer agents' recent performance and identify anything to flag.

Peer summaries (last 7 days):
{json.dumps(peer_summaries, indent=2)}

Your recent memory:
{json.dumps(self.read_memory(days=3), indent=2)}

Respond ONLY with a JSON array of flags to raise (empty array if none):
[
  {{
    "to_agent": "<agent name>",
    "priority": "urgent|normal|low",
    "message": "<specific request or observation>"
  }}
]"""

        try:
            raw = call_llm(prompt, max_tokens=500)
            clean = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            flags = json.loads(clean)
            for flag in flags:
                self.raise_flag(
                    flag["to_agent"],
                    flag["message"],
                    priority=flag.get("priority", "normal")
                )
            self.logger.info(f"peer_review raised {len(flags)} flags")
        except Exception as e:
            self.logger.warning(f"peer_review failed: {e}")

    # ═══════════════════════════════════════════════════════
    #  COORDINATION — flags
    # ═══════════════════════════════════════════════════════

    def raise_flag(self, to_agent: str, message: str,
                   priority: str = "normal", context: dict = None):
        """Raise a flag to another agent."""
        supabase_insert("agent_flags", {
            "client_id": self.client_id,
            "from_agent": self.name,
            "to_agent": to_agent,
            "priority": priority,
            "message": message,
            "context": context or {},
            "resolved": False
        })
        self.logger.info(f"Flag raised → {to_agent} [{priority}]: {message[:60]}")

    def check_flags(self, resolved: bool = False) -> list:
        """Check flags addressed to this agent."""
        rows = supabase_select(
            "agent_flags",
            filters={
                "client_id": self.client_id,
                "to_agent": self.name,
                "resolved": resolved
            }
        )
        return rows

    def resolve_flag(self, flag_id: str):
        """Mark a flag as resolved."""
        supabase_update(
            "agent_flags",
            row_id=flag_id,
            data={
                "resolved": True,
                "resolved_at": datetime.utcnow().isoformat(),
                "resolved_by": self.name
            }
        )

    # ═══════════════════════════════════════════════════════
    #  REPORTING
    # ═══════════════════════════════════════════════════════

    def post_report(self, reflection: dict):
        """Post cycle summary to reports table."""
        flags_raised = len([
            a for a in self._actions if "flag" in a.lower()
        ])
        supabase_insert("reports", {
            "client_id": self.client_id,
            "agent_name": self.name,
            "cycle_date": date.today().isoformat(),
            "summary": self._build_summary(reflection),
            "metrics": self._metrics,
            "flags_raised": flags_raised,
            "flags_received": len(self.check_flags(resolved=True))
        })

    def _build_summary(self, reflection: dict) -> str:
        parts = []
        if self._actions:
            parts.append("Actions: " + " | ".join(self._actions[:3]))
        if self._outcomes:
            parts.append("Outcomes: " + " | ".join(self._outcomes[:3]))
        parts.append(f"Rating: {reflection.get('performance_rating', '?')}/10")
        parts.append(f"Next: {reflection.get('next_cycle_intent', '')}")
        return " · ".join(parts)

    # ═══════════════════════════════════════════════════════
    #  OPERATIONS
    # ═══════════════════════════════════════════════════════

    def read_instructions(self) -> list:
        """Read active CEO instructions for this agent."""
        rows = supabase_select(
            "instructions",
            filters={
                "client_id": self.client_id,
                "active": True
            }
        )
        return [
            r for r in rows
            if r.get("target_agent") in (self.name, "all")
        ]

    def log(self, event_type: str, message: str, metadata: dict = None):
        """Write to the logs table."""
        supabase_insert("logs", {
            "client_id": self.client_id,
            "agent_name": self.name,
            "event_type": event_type,
            "message": message,
            "metadata": metadata or {}
        })

    # ═══════════════════════════════════════════════════════
    #  CYCLE HELPERS
    # ═══════════════════════════════════════════════════════

    def _log_action(self, action: str):
        self._actions.append(action)
        self.logger.info(f"ACTION: {action}")

    def _log_outcome(self, outcome: str):
        self._outcomes.append(outcome)
        self.logger.info(f"OUTCOME: {outcome}")

    def _set_metric(self, key: str, value):
        self._metrics[key] = value
