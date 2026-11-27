import re

from agent.llm import Router, load_agent_instructions


async def generate_diagram(router: Router, text: str) -> str:
    result = await router.complete(
        load_agent_instructions("architect"),
        text,
    )
    match = re.fullmatch(r"```(?:mermaid)?\s*\n(.*?)\n```", result, re.DOTALL | re.IGNORECASE)
    if match:
        result = match.group(1).strip()
    if not re.match(r"^(flowchart|graph)\s+(TD|TB|BT|RL|LR)\b", result):
        raise ValueError("Architect response is not a Mermaid flowchart")
    if len(result.encode("utf-8")) > 100_000:
        raise ValueError("Architect response exceeds the diagram size limit")
    return result + "\n"
