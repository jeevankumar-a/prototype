import asyncio
import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from temporalio.client import Client
from temporalio.worker import Worker

from agent.github import GitHubPublisher
from observability.telemetry import configure
from orchestrator.activities import DeveloperActivities, ingest_file, run_architect_agent
from orchestrator.workflow import DocumentToRepoWorkflow


async def authenticate(session: ClientSession) -> None:
    result = await session.call_tool("get_me", {})
    if result.isError:
        raise RuntimeError("GitHub MCP authentication failed; check the token and its expiration")
    text = "\n".join(item.text for item in result.content if isinstance(item, types.TextContent))
    try:
        profile = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Unexpected GitHub MCP authentication response") from exc
    if not isinstance(profile, dict) or not isinstance(profile.get("login"), str):
        raise RuntimeError("GitHub MCP returned an invalid user profile")
    logging.info("GitHub MCP authenticated as %s", profile["login"])


def github_server_params(server: Path, token: str) -> StdioServerParameters:
    if not token.startswith("github_pat_") or len(token) <= len("github_pat_") or any(c.isspace() for c in token):
        raise ValueError("GITHUB_PERSONAL_ACCESS_TOKEN must be a fine-grained GitHub token")
    return StdioServerParameters(
        command=str(server.resolve()),
        args=["stdio", "--tools", "get_me,get_file_contents,create_or_update_file"],
        env={"GITHUB_PERSONAL_ACCESS_TOKEN": token},
    )


async def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    configure("document-worker")
    server = Path(os.getenv("GITHUB_MCP_SERVER", str(Path(".local") / "github-mcp" / "github-mcp-server.exe")))
    if not server.is_file():
        raise FileNotFoundError(f"GitHub MCP server not found: {server}. See README setup.")
    publisher_repo = os.getenv("TARGET_REPO", "")
    branch = os.getenv("TARGET_BRANCH", "main")
    directory = os.getenv("TARGET_DIRECTORY", "diagrams")
    params = github_server_params(server, os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN", ""))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            info = await session.initialize()
            if info.serverInfo.name != "github-mcp-server" or info.serverInfo.version != "1.5.0":
                raise RuntimeError("GitHub MCP server version 1.5.0 is required; see README setup")
            publisher = GitHubPublisher(session, publisher_repo, branch, directory)
            await authenticate(session)
            client = await Client.connect(
                os.getenv("TEMPORAL_ADDRESS", "localhost:7233"),
                namespace=os.getenv("TEMPORAL_NAMESPACE", "default"),
            )
            developer = DeveloperActivities(publisher)
            worker = Worker(
                client, task_queue="document-to-repo",
                workflows=[DocumentToRepoWorkflow],
                activities=[ingest_file, run_architect_agent, developer.run_developer_agent],
            )
            await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
