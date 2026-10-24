from __future__ import annotations

import json

import httpx
from pydantic import BaseModel, Field

from backend.app.agents.script_writer.contracts import ScriptDraft
from backend.app.db.models import Article, CredibilityAssessment, ResearchBrief


class ExtractiveScriptWriter:
    """A local, deterministic writer that only reorders stored evidence."""

    name = "extractive"

    async def write(
        self,
        article: Article,
        assessment: CredibilityAssessment,
        research_brief: ResearchBrief,
    ) -> ScriptDraft:
        source_label = (
            f"{assessment.independent_source_count} independent sources"
            if assessment.independent_source_count != 1
            else "one independent source"
        )
        main_story = research_brief.key_facts[:4] or [article.normalized_text]
        return ScriptDraft(
            title=article.title,
            hook=f"Here is what is known so far about {article.title}.",
            introduction=(f"This report is based on information corroborated by {source_label}."),
            main_story=main_story,
            context=research_brief.context[:3],
            closing=(
                "This report reflects information available from the cited sources at this time."
            ),
        )


class OllamaScriptPayload(BaseModel):
    title: str = Field(min_length=1)
    hook: str = Field(min_length=1)
    introduction: str = Field(min_length=1)
    main_story: list[str] = Field(min_length=1, max_length=6)
    context: list[str] = Field(max_length=4)
    closing: str = Field(min_length=1)


class OllamaScriptWriter:
    """Optional local writer; structured output is validated before a script is persisted."""

    name = "ollama"

    def __init__(self, base_url: str, model: str, target_seconds: int) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._target_seconds = target_seconds

    async def write(
        self,
        article: Article,
        assessment: CredibilityAssessment,
        research_brief: ResearchBrief,
    ) -> ScriptDraft:
        evidence = {
            "title": article.title,
            "verified_claims": assessment.claims,
            "research_key_facts": research_brief.key_facts,
            "research_context": research_brief.context,
            "uncertainties": research_brief.uncertainties,
        }
        prompt = (
            "Write a factual, non-controversial news narration from ONLY the evidence below. "
            "Do not add facts, opinions, speculation, emotional language, or calls to action. "
            "Keep uncertainty explicit. Create a script for approximately "
            f"{self._target_seconds} seconds. Return JSON matching this schema: "
            f"{json.dumps(OllamaScriptPayload.model_json_schema())}.\n\nEvidence:\n"
            f"{json.dumps(evidence)}"
        )
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(
                f"{self._base_url}/api/chat",
                json={
                    "model": self._model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "format": "json",
                },
            )
        response.raise_for_status()
        content = response.json().get("message", {}).get("content", "")
        payload = OllamaScriptPayload.model_validate_json(content)
        return ScriptDraft(**payload.model_dump())
