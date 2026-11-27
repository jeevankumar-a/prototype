import re

from agent.github import GitHubPublisher
from agent.llm import Router, load_agent_instructions
from orchestrator.models import Diagram


async def publish_diagram(router: Router, publisher: GitHubPublisher, diagram: Diagram) -> str:
    path = publisher.path(diagram.source.digest)
    if await publisher.exists_with_content(path, diagram.mermaid):
        return path
    title = await router.complete(
        load_agent_instructions("developer"),
        diagram.mermaid,
    )
    title = re.sub(r"\s+", " ", title).strip()[:100]
    if not title:
        raise ValueError("Developer returned an empty commit title")
    return await publisher.publish(path, diagram.mermaid, title)
