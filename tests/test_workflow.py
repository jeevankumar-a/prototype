from unittest.mock import AsyncMock, Mock
from types import SimpleNamespace

import httpx
import pytest
from mcp import types
from openai import AsyncOpenAI, BadRequestError
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from temporalio import workflow
from temporalio.exceptions import ApplicationError
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

from agent.architect import generate_diagram
from agent.developer import publish_diagram
from agent.github import GitHubMCPError, GitHubPublisher
from agent.llm import Router, load_agent_instructions
from observability.telemetry import activity_span
from orchestrator.activities import digest_snapshot, ingest_file
from orchestrator.models import Diagram, FileSnapshot
from orchestrator.watcher import scan
from orchestrator.worker import authenticate, github_server_params
from orchestrator.workflow import DocumentToRepoWorkflow


class FakeRouter:
    def __init__(self, value: str) -> None:
        self.value = value

    async def complete(self, instruction: str, content: str) -> str:
        return self.value


@pytest.mark.asyncio
async def test_router_uses_local_proxy_and_propagates_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LITELLM_VIRTUAL_KEY", "sk-test")
    router = Router()
    await router.close()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={
            "id": "test", "object": "chat.completion", "created": 1, "model": "agent-router",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
        })

    router.client = AsyncOpenAI(
        base_url="http://localhost:4000", api_key="sk-test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with TracerProvider().get_tracer("test").start_as_current_span("test"):
        assert await router.complete("instruction", "document") == "ok"
    assert str(requests[0].url) == "http://localhost:4000/chat/completions"
    assert requests[0].headers["traceparent"].startswith("00-")
    assert requests[0].headers["langfuse_trace_id"] in requests[0].content.decode()
    await router.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", ["document_guardrail_violation", "invalid_request_error"])
async def test_router_does_not_retry_guardrail_rejections(
    monkeypatch: pytest.MonkeyPatch, error_type: str,
) -> None:
    monkeypatch.setenv("LITELLM_VIRTUAL_KEY", "sk-test")
    router = Router()
    await router.close()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {
            "message": "Request rejected", "type": error_type,
            "param": "document-secret-guard", "code": "400",
        }})

    router.client = AsyncOpenAI(
        base_url="http://localhost:4000", api_key="sk-test", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        if error_type == "document_guardrail_violation":
            with pytest.raises(ApplicationError) as error:
                await router.complete("instruction", "document")
            assert error.value.non_retryable is True
        else:
            with pytest.raises(BadRequestError):
                await router.complete("instruction", "document")
    finally:
        await router.close()


@pytest.mark.asyncio
async def test_ingest_rejects_modified_snapshot() -> None:
    from temporalio.exceptions import ApplicationError
    snapshot = FileSnapshot("doc.txt", "modified", digest_snapshot("doc.txt", "original"), "")
    with pytest.raises(ApplicationError, match="digest mismatch"):
        await ingest_file(snapshot)


@pytest.mark.asyncio
async def test_architect_requires_flowchart() -> None:
    assert await generate_diagram(FakeRouter("```mermaid\nflowchart TD\nA-->B\n```"), "x") == "flowchart TD\nA-->B\n"
    with pytest.raises(ValueError, match="not a Mermaid"):
        await generate_diagram(FakeRouter("Here is a diagram"), "x")


@pytest.mark.asyncio
async def test_agents_use_editable_markdown_instructions(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent.llm.AGENT_DIRECTORY", tmp_path)
    (tmp_path / "architect.agent.md").write_text(
        "---\nname: Architect\ntools: []\n---\nArchitect instructions\n", encoding="utf-8",
    )
    (tmp_path / "developer.agent.md").write_text(
        "---\nname: Developer\ntools: []\n---\nDeveloper instructions\n", encoding="utf-8",
    )
    router = AsyncMock()
    router.complete.return_value = "flowchart TD\nA-->B"
    await generate_diagram(router, "document")
    router.complete.assert_awaited_once_with("Architect instructions", "document")

    router.complete.reset_mock()
    router.complete.return_value = "Add diagram"
    publisher = AsyncMock()
    publisher.path = Mock(return_value="diagrams/test.mmd")
    publisher.exists_with_content.return_value = False
    publisher.publish.return_value = "diagrams/test.mmd"
    diagram = Diagram(FileSnapshot("doc.txt", "document", "a" * 64, ""), "flowchart TD\nA-->B\n")
    assert await publish_diagram(router, publisher, diagram) == "diagrams/test.mmd"
    router.complete.assert_awaited_once_with("Developer instructions", diagram.mermaid)


def test_missing_or_empty_agent_instructions_fail(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent.llm.AGENT_DIRECTORY", tmp_path)
    with pytest.raises(FileNotFoundError):
        load_agent_instructions("architect")
    profile = tmp_path / "architect.agent.md"
    profile.write_text("---\nname: Architect\n---\n \n", encoding="utf-8")
    with pytest.raises(ValueError, match="instructions are empty"):
        load_agent_instructions("architect")
    profile.write_text("---\nname: [invalid\n---\ninstructions\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid YAML"):
        load_agent_instructions("architect")
    profile.write_text("---\nname: Architect\ninstructions\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unclosed YAML"):
        load_agent_instructions("architect")


@pytest.mark.asyncio
async def test_publisher_reconciles_retry_and_refuses_overwrite() -> None:
    session = AsyncMock()
    publisher = GitHubPublisher(session, "owner/repo", "main", "diagrams")
    content = "flowchart TD\nA-->B\n"
    path = publisher.path("a" * 64)
    session.call_tool.return_value = file_result(path, content)
    assert await publisher.publish(path, content, "Add diagram") == path
    with pytest.raises(ValueError, match="different content"):
        await publisher.publish(path, "different", "Add diagram")
    assert [call.args[0] for call in session.call_tool.await_args_list] == ["get_file_contents"] * 2


@pytest.mark.asyncio
async def test_publisher_create_then_reconcile_conflict() -> None:
    session = AsyncMock()
    publisher = GitHubPublisher(session, "owner/repo", "main", "diagrams")
    path = publisher.path("b" * 64)
    session.call_tool.side_effect = [
        missing_result(),
        types.CallToolResult(isError=True, content=[types.TextContent(type="text", text="File already exists")]),
        file_result(path, "diagram"),
    ]
    assert await publisher.publish(path, "diagram", "Add diagram") == path
    assert [call.args[0] for call in session.call_tool.await_args_list] == [
        "get_file_contents", "create_or_update_file", "get_file_contents",
    ]
    assert "sha" not in session.call_tool.await_args_list[1].args[1]


def missing_result() -> types.CallToolResult:
    return types.CallToolResult(isError=True, content=[types.TextContent(
        type="text", text="Failed to get file contents. The path does not point to a file or "
                          "directory, or the file does not exist in the repository.",
    )])


def file_result(path: str, content: str, message: str = "success") -> types.CallToolResult:
    return types.CallToolResult(content=[
        types.TextContent(type="text", text=message),
        types.EmbeddedResource(type="resource", resource=types.TextResourceContents(
            uri=f"repo://owner/repo/sha/{'c' * 40}/contents/{path}", text=content,
        )),
    ])


@pytest.mark.asyncio
async def test_publisher_surfaces_read_errors_and_wrong_paths() -> None:
    session = AsyncMock()
    publisher = GitHubPublisher(session, "owner/repo", "main", "diagrams")
    path = publisher.path("d" * 64)
    session.call_tool.return_value = types.CallToolResult(
        isError=True, content=[types.TextContent(type="text", text="403 Forbidden")],
    )
    with pytest.raises(GitHubMCPError, match="403"):
        await publisher.publish(path, "diagram", "Add diagram")
    session.call_tool.return_value = file_result("other/file.mmd", "diagram")
    with pytest.raises(GitHubMCPError, match="different repository path"):
        await publisher.exists_with_content(path, "diagram")
    session.call_tool.return_value = file_result(path, "diagram", "default branch was used instead")
    with pytest.raises(GitHubMCPError, match="default branch"):
        await publisher.exists_with_content(path, "diagram")


@pytest.mark.asyncio
async def test_publisher_creates_only_after_missing_and_verifies() -> None:
    session = AsyncMock()
    publisher = GitHubPublisher(session, "owner/repo", "main", "diagrams")
    path = publisher.path("e" * 64)
    session.call_tool.side_effect = [
        missing_result(), types.CallToolResult(content=[types.TextContent(type="text", text="created")]),
        file_result(path, "diagram"),
    ]
    assert await publisher.publish(path, "diagram", "Add diagram") == path
    assert session.call_tool.await_args_list[1].args[1]["branch"] == "main"


@pytest.mark.asyncio
async def test_publisher_does_not_report_unverified_creation() -> None:
    session = AsyncMock()
    publisher = GitHubPublisher(session, "owner/repo", "main", "diagrams")
    path = publisher.path("f" * 64)
    session.call_tool.side_effect = [
        missing_result(), types.CallToolResult(content=[types.TextContent(type="text", text="created")]),
        missing_result(),
    ]
    with pytest.raises(GitHubMCPError, match="did not confirm"):
        await publisher.publish(path, "diagram", "Add diagram")


def test_github_server_uses_only_configured_fine_grained_token(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "old-token")
    monkeypatch.setenv("GITHUB_HOST", "unexpected-host")
    params = github_server_params(tmp_path / "server.exe", "github_pat_test")
    assert params.env == {"GITHUB_PERSONAL_ACCESS_TOKEN": "github_pat_test"}
    assert "--oauth-scopes" not in params.args
    for token in ("", "replace-me", "ghp_classic", "github_pat_", "github_pat_test\n"):
        with pytest.raises(ValueError, match="fine-grained"):
            github_server_params(tmp_path / "server.exe", token)


@pytest.mark.asyncio
async def test_authenticate_checks_token_and_surfaces_errors() -> None:
    session = AsyncMock()
    session.call_tool.return_value = types.CallToolResult(
        content=[types.TextContent(type="text", text='{"login":"example"}')],
    )
    await authenticate(session)
    session.call_tool.assert_awaited_once_with("get_me", {})
    session.call_tool.return_value = types.CallToolResult(
        isError=True, content=[types.TextContent(type="text", text="authorization denied")],
    )
    with pytest.raises(RuntimeError, match="authentication failed"):
        await authenticate(session)
    session.call_tool.return_value = types.CallToolResult(
        content=[types.TextContent(type="text", text="unexpected")],
    )
    with pytest.raises(RuntimeError, match="Unexpected"):
        await authenticate(session)
    session.call_tool.return_value = types.CallToolResult(content=[types.TextContent(type="text", text="{}")])
    with pytest.raises(RuntimeError, match="invalid user profile"):
        await authenticate(session)


@pytest.mark.asyncio
async def test_watcher_stable_snapshot_and_deduplication(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("orchestrator.watcher.INPUT", tmp_path)
    monkeypatch.setattr("orchestrator.watcher.trace.get_tracer", TracerProvider().get_tracer)
    path = tmp_path / "input.txt"
    path.write_text("first", encoding="utf-8")
    client = AsyncMock()
    previous: dict[str, str] = {}
    submitted: dict[str, str] = {}
    await scan(client, previous, submitted)
    assert client.start_workflow.await_count == 0
    await scan(client, previous, submitted)
    await scan(client, previous, submitted)
    assert client.start_workflow.await_count == 1
    snapshot = client.start_workflow.await_args.args[1]
    assert snapshot.text == "first"
    assert snapshot.traceparent.startswith("00-")
    path.write_text("second", encoding="utf-8")
    await scan(client, previous, submitted)
    await scan(client, previous, submitted)
    assert client.start_workflow.await_count == 2


@pytest.mark.asyncio
async def test_trace_metadata_correlates_document_and_activity_attempts(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    monkeypatch.setattr("orchestrator.watcher.INPUT", tmp_path)
    monkeypatch.setattr("opentelemetry.trace.get_tracer", lambda *args, **kwargs: tracer)
    monkeypatch.setattr("observability.telemetry.activity.in_activity", lambda: True)
    monkeypatch.setattr("observability.telemetry.activity.info", lambda: SimpleNamespace(attempt=2, workflow_run_id="run-1"))
    (tmp_path / "input.txt").write_text("document", encoding="utf-8")
    client = AsyncMock()
    previous: dict[str, str] = {}
    submitted: dict[str, str] = {}
    await scan(client, previous, submitted)
    await scan(client, previous, submitted)
    snapshot = client.start_workflow.await_args.args[1]
    with activity_span("run_architect_agent", snapshot.traceparent, snapshot.digest, snapshot.name):
        pass
    with pytest.raises(ValueError):
        with activity_span("run_developer_agent", snapshot.traceparent, snapshot.digest, snapshot.name):
            raise ValueError("test failure")

    root, success, failure = exporter.get_finished_spans()
    for span in (root, success, failure):
        assert span.attributes["langfuse.trace.metadata.filename"] == "input.txt"
        assert span.attributes["langfuse.trace.metadata.workflow_id"] == f"document-{snapshot.digest}"
        assert span.attributes["langfuse.session.id"] == f"document-{snapshot.digest}"
        assert span.context.trace_id == root.context.trace_id
    assert root.attributes["langfuse.observation.metadata.outcome"] == "started"
    assert success.attributes["langfuse.observation.metadata.outcome"] == "success"
    assert failure.attributes["langfuse.observation.metadata.outcome"] == "failure"
    assert failure.attributes["langfuse.observation.metadata.attempt"] == "2"
    assert failure.attributes["langfuse.observation.metadata.run_id"] == "run-1"
    assert failure.status.status_code == StatusCode.ERROR
    provider.shutdown()


@pytest.mark.asyncio
async def test_workflow_sandbox_prepares() -> None:
    SandboxedWorkflowRunner().prepare_workflow(workflow._Definition.must_from_class(DocumentToRepoWorkflow))
