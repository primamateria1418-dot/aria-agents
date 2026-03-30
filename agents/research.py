"""
agents/research.py
ARIA™ — Research Agent (Agent 02)
Runs at 06:00 daily. Scans web for market angles, trends, news.
Stores content angles to content_queue for the Writer.
OUP International Ltd, 2026
"""

import os
import json
import logging
import httpx
from datetime import date
from core.base_agent import BaseAgent
from core.llm import call_llm
from core.memory import supabase_insert, supabase_select

logger = logging.getLogger("aria.research")

TAVILY_KEY  = os.environ.get("TAVILY_API_KEY", "")
SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://localhost:8080")


class ResearchAgent(BaseAgent):
    def __init__(self, client_id: str = "aria_internal"):
        super().__init__(name="research", client_id=client_id)

    def run(self):
        flags = self.check_flags(resolved=False)
        urgent_requests = [f for f in flags if f.get("priority") == "urgent"]
        if urgent_requests:
            self._log_action(f"Urgent flags from peers: {[f['from_agent'] for f in urgent_requests]}")

        instructions = self.read_instructions()
        extra_topics  = self._parse_topic_instructions(instructions)
        topics        = self._build_topics(extra_topics, urgent_requests)
        self._log_action(f"Scanning {len(topics)} topics: {', '.join(topics[:4])}")

        angles = []
        for topic in topics:
            results      = self._search(topic)
            topic_angles = self._extract_angles(topic, results)
            angles.extend(topic_angles)

        self._log_action(f"Extracted {len(angles)} raw angles")
        scored     = self._score_angles(angles)
        top_angles = sorted(scored, key=lambda x: x.get("score", 0), reverse=True)[:8]

        stored = 0
        urgent_count = 0
        for angle in top_angles:
            if angle.get("score", 0) >= 6:
                self._store_angle(angle)
                stored += 1
                if angle.get("score", 0) >= 8:
                    urgent_count += 1
                    self.raise_flag(
                        "writer",
                        f"Urgent angle: {angle['headline']} (score {angle['score']})",
                        priority="urgent",
                        context={"angle_id": angle.get("id"), "topic": angle.get("topic")}
                    )

        self._log_action(f"Stored {stored} angles to content_queue")
        self._log_outcome(f"{stored} angles ready for Writer · {urgent_count} flagged urgent")
        self._set_metric("angles_stored", stored)
        self._set_metric("urgent_angles", urgent_count)
        self._set_metric("topics_scanned", len(topics))

    def _build_topics(self, extra_topics: list, urgent_flags: list) -> list:
        base_topics = self._get_base_topics()
        for flag in urgent_flags:
            msg = flag.get("message", "")
            if "angle" in msg.lower() or "topic" in msg.lower() or "research" in msg.lower():
                extra_topics.append(msg.replace("Need more research on", "").strip())
        all_topics = list(dict.fromkeys(base_topics + extra_topics))
        return all_topics[:10]

    def _get_base_topics(self) -> list:
        """Load research topics from brand_profiles. Falls back to ARIA defaults."""
        try:
            profiles = supabase_select("brand_profiles", filters={"client_id": self.client_id}, limit=1)
            if profiles and profiles[0].get("research_topics"):
                topics = [t.strip() for t in profiles[0]["research_topics"].split(",") if t.strip()]
                if topics:
                    return topics
        except Exception as e:
            logger.warning(f"Could not load research_topics for {self.client_id}: {e}")
        return [
            "AI marketing automation 2025",
            "content marketing trends this week",
            "B2B SaaS lead generation strategies",
            "LinkedIn marketing best practices",
            "AI agency news",
            "startup marketing budget",
            "fractional marketing AI tools",
        ]

    def _parse_topic_instructions(self, instructions: list) -> list:
        topics = []
        for inst in instructions:
            text = inst.get("instruction", "").lower()
            if "research" in text or "scan" in text or "topic" in text:
                for keyword in ["research ", "scan ", "topic "]:
                    if keyword in text:
                        topic = text.split(keyword, 1)[1].strip().rstrip(".")
                        if len(topic) > 3:
                            topics.append(topic)
        return topics

    def _search(self, query: str) -> list:
        if TAVILY_KEY:
            results = self._search_tavily(query)
            if results:
                return results
        return self._search_searxng(query)

    def _search_tavily(self, query: str) -> list:
        try:
            with httpx.Client(timeout=10) as client:
                res = client.post(
                    "https://api.tavily.com/search",
                    json={"api_key": TAVILY_KEY, "query": query, "search_depth": "basic", "max_results": 5, "include_answer": False}
                )
                res.raise_for_status()
                return [{"title": r.get("title",""), "url": r.get("url",""), "snippet": r.get("content","")[:500], "source": "tavily"}
                        for r in res.json().get("results", [])]
        except Exception as e:
            logger.warning(f"Tavily search failed for '{query}': {e}")
            return []

    def _search_searxng(self, query: str) -> list:
        try:
            with httpx.Client(timeout=10) as client:
                res = client.get(f"{SEARXNG_URL}/search",
                                 params={"q": query, "format": "json", "categories": "general", "language": "en"})
                res.raise_for_status()
                return [{"title": r.get("title",""), "url": r.get("url",""), "snippet": r.get("content","")[:500], "source": "searxng"}
                        for r in res.json().get("results", [])[:5]]
        except Exception as e:
            logger.warning(f"SearXNG search failed for '{query}': {e}")
            return []

    def _extract_angles(self, topic: str, results: list) -> list:
        if not results:
            return []
        snippets = "\n\n".join([f"Title: {r['title']}\nSnippet: {r['snippet']}" for r in results[:4]])
        prompt = f"""You are the Research agent for ARIA, an AI marketing agency.

Topic scanned: {topic}
Today's date: {date.today().isoformat()}

Search results:
{snippets}

Extract 2-3 strong content angles from this research. Each angle should be a specific,
opinionated idea for a LinkedIn post, email, or blog article that would resonate with
startup founders, marketing leaders, or B2B decision-makers.

Respond ONLY with a JSON array:
[
  {{
    "headline": "<punchy 8-12 word headline>",
    "angle": "<2-3 sentence explanation of the content direction>",
    "format": "linkedin_post|email|blog|thread",
    "topic": "{topic}",
    "urgency": "high|medium|low",
    "source_url": "<most relevant URL from results>"
  }}
]"""
        try:
            raw   = call_llm(prompt, max_tokens=600, temperature=0.8)
            clean = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            angles = json.loads(clean)
            return angles if isinstance(angles, list) else []
        except Exception as e:
            logger.warning(f"Angle extraction failed for '{topic}': {e}")
            return []

    def _score_angles(self, angles: list) -> list:
        if not angles:
            return []
        angle_list = json.dumps(
            [{"headline": a.get("headline"), "angle": a.get("angle"), "urgency": a.get("urgency")} for a in angles],
            indent=2
        )
        prompt = f"""Score each content angle for an AI marketing agency's content strategy.

Scoring criteria (1-10):
- Relevance to B2B marketing / startup audience (high weight)
- Timeliness / newsworthiness (medium weight)
- Originality / differentiation (medium weight)
- Engagement potential on LinkedIn or email (high weight)

Angles to score:
{angle_list}

Respond ONLY with a JSON array of scores (same order as input):
[{{"score": 8}}, {{"score": 6}}, ...]"""
        try:
            raw    = call_llm(prompt, max_tokens=200, temperature=0.2)
            clean  = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            scores = json.loads(clean)
            for i, angle in enumerate(angles):
                if i < len(scores):
                    angle["score"] = scores[i].get("score", 5)
            return angles
        except Exception as e:
            logger.warning(f"Scoring failed: {e} — assigning default scores")
            for angle in angles:
                angle["score"] = 5
            return angles

    def _store_angle(self, angle: dict) -> dict | None:
       row = supabase_insert("content_queue", {
            "client_id":    self.client_id,
            "content_type": angle.get("format", "linkedin_post"),
            "platform":     self._format_to_platform(angle.get("format", "linkedin_post")),
            "draft":        f"ANGLE: {angle['headline']}\n\n{angle['angle']}\n\nSource: {angle.get('source_url', '')}",
            "approved":     False,
            "published":    False,
            "created_by":   "research",
            "status":       "research_angle",
        })
        if row:
            angle["id"] = row.get("id")
        return row

    def _format_to_platform(self, fmt: str) -> str:
        return {"linkedin_post": "linkedin", "email": "email", "blog": "blog", "thread": "twitter"}.get(fmt, "linkedin")


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv
    load_dotenv()
    agent = ResearchAgent(client_id="aria_internal")
    if "--test" in sys.argv:
        print("=== RESEARCH AGENT — TEST MODE ===")
        agent._log_action("TEST: Scanned 3 dummy topics")
        agent._log_outcome("TEST: 4 angles stored")
        agent._set_metric("angles_stored", 4)
        print(json.dumps(agent.self_review(), indent=2))
        print("done")
    else:
        print("=== RESEARCH AGENT — LIVE RUN ===")
        agent.execute_cycle()
        print("done")
