import os
from pathlib import Path
from typing import Literal

import httpx
import yaml
from openai import AsyncOpenAI, BadRequestError
from temporalio.exceptions import ApplicationError

from observability.telemetry import trace_headers


AGENT_DIRECTORY = Path(__file__).resolve().parent.parent / ".github" / "agents"


def load_agent_instructions(name: Literal["architect", "developer"]) -> str:
    path = AGENT_DIRECTORY / f"{name}.agent.md"
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"Agent profile is missing YAML frontmatter: {path}")
    try:
        end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration:
        raise ValueError(f"Agent profile has unclosed YAML frontmatter: {path}") from None
    try:
        metadata = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError as exc:
        raise ValueError(f"Agent profile has invalid YAML frontmatter: {path}") from exc
    if not isinstance(metadata, dict):
        raise ValueError(f"Agent profile frontmatter must be a mapping: {path}")
    instructions = "\n".join(lines[end + 1:]).strip()
    if not instructions:
        raise ValueError(f"Agent instructions are empty: {path}")
    return instructions


class Router:
    def __init__(self) -> None:
        key = os.environ.get("LITELLM_VIRTUAL_KEY")
        if not key:
            raise ValueError("LITELLM_VIRTUAL_KEY is required")
        self.client = AsyncOpenAI(
            base_url="http://localhost:4000", api_key=key, timeout=150, max_retries=0,
            http_client=httpx.AsyncClient(trust_env=False, follow_redirects=False),
        )

    async def close(self) -> None:
        await self.client.close()

    async def complete(self, instruction: str, content: str) -> str:
        headers = trace_headers()
        try:
            response = await self.client.chat.completions.create(
                model="agent-router",
                messages=[
                    {"role": "system", "content": instruction},
                    {"role": "user", "content": content},
                ],
                max_tokens=4096,
                extra_headers=headers,
                extra_body={"metadata": {"existing_trace_id": headers.get("langfuse_trace_id")}},
            )
        except BadRequestError as exc:
            if exc.type == "document_guardrail_violation":
                raise ApplicationError(
                    f"LiteLLM guardrail rejected the request ({exc.param})",
                    type="DocumentGuardrailViolation", non_retryable=True,
                ) from None
            raise
        if not response.choices or not response.choices[0].message.content:
            raise ValueError("LiteLLM returned an empty completion")
        if response.choices[0].finish_reason == "length":
            raise ValueError("LiteLLM response was truncated")
        return response.choices[0].message.content.strip()
