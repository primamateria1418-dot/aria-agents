"""
ARIA™ — ResearchAgent
Reads research topics from Supabase brand_profiles per client.
Runs daily at 6am via scheduler.
Version: 30 March 2026 — Session 2
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from core.base_agent import BaseAgent
from core.llm import groq_chat
from core.memory import supabase_select, supabase_insert

logger = logging.getLogger(__name__)

# Fallback topics if brand_profiles has no research_topics
DEFAULT_TOPICS = {
    "aria_internal": (
        "AI marketing automation, AI agency trends, marketing technology, "
        "founder productivity, SME growth strategies"
    ),
    "asset_club": (
        "alternative investments, fractional property, HNW wealth management, "
        "asset diversification, UK property market, family office trends"
    ),
    "oup_intl": (
        "humanitarian technology, blockchain transparency, AI impact measurement, "
        "sustainable development, impact investing, ESG, NGO innovation"
    ),
}


class ResearchAgent(BaseAgent):
    def __init__(self, client_id: str):
        super().__init__(agent_name="research", client_id=client_id)

    def _get_research_topics(self) -> str:
        """Fetch research topics from brand_profiles. Falls back to defaults."""
        try:
            profiles = supabase_select(
                "brand_profiles",
                filters={"client_id": self.client_id},
                limit=1,
            )
            if profiles:
                topics = profiles[0].get("research_topics", "")
                if topics and topics.strip():
                    return topics.strip()
        except Exception as e:
            logger.warning(
                f"ResearchAgent._get_research_topics: DB fetch failed for "
                f"{self.client_id}: {e}"
            )

        fallback = DEFAULT_TOPICS.get(self.client_id, DEFAULT_TOPICS["aria_internal"])
        logger.info(f"ResearchAgent: using fallback topics for {self.client_id}")
        return fallback

    async def _research_topic(self, topic: str) -> Optional[dict]:
        """Research a single topic and return a structured insight."""
        system = (
            "You are a market research analyst. Given a topic, produce a concise "
            "research summary in the following JSON format (no markdown fences):\n"
            "{\n"
            '  "headline": "One sentence insight",\n'
            '  "summary": "2–3 sentence analysis",\n'
            '  "content_angle": "How a brand could use this insight in a LinkedIn post",\n'
            '  "source_type": "trend|news|data|opinion"\n'
            "}"
        )
        user = f"Research this topic and provide a marketing insight:\n{topic}"

        try:
            raw = await groq_chat(system=system, user=user)
            import json
            # Strip any accidental markdown fences
            clean = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            data = json.loads(clean)
            return data
        except Exception as e:
            logger.warning(f"_research_topic failed for topic '{topic}': {e}")
            return None

    async def run(self):
        """Run research scan for this client. Saves insights to agent_memory."""
        topics_str = self._get_research_topics()
        topics = [t.strip() for t in topics_str.split(",") if t.strip()]

        if not topics:
            logger.warning(f"ResearchAgent.run: no topics for {self.client_id}")
            return

        logger.info(
            f"ResearchAgent.run: scanning {len(topics)} topics for {self.client_id}"
        )

        insights = []
        for topic in topics[:5]:  # Cap at 5 per run to stay within Groq limits
            insight = await self._research_topic(topic)
            if insight:
                insights.append(insight)

        if insights:
            supabase_insert(
                "agent_memory",
                {
                    "agent": "research",
                    "client_id": self.client_id,
                    "memory_type": "research_insights",
                    "content": str(insights),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            logger.info(
                f"ResearchAgent.run: saved {len(insights)} insights for {self.client_id}"
            )
        else:
            logger.warning(
                f"ResearchAgent.run: no insights produced for {self.client_id}"
            )
