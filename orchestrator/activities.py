import hashlib

from opentelemetry import metrics
from temporalio import activity
from temporalio.exceptions import ApplicationError

from agent.architect import generate_diagram
from agent.developer import publish_diagram
from agent.github import GitHubPublisher
from agent.llm import Router
from observability.telemetry import activity_span
from orchestrator.models import Diagram, FileSnapshot


def digest_snapshot(name: str, text: str) -> str:
    return hashlib.sha256(name.encode("utf-8") + b"\0" + text.encode("utf-8")).hexdigest()


@activity.defn
async def ingest_file(snapshot: FileSnapshot) -> FileSnapshot:
    with activity_span("ingest_file", snapshot.traceparent, snapshot.digest, snapshot.name):
        if not snapshot.text.strip() or len(snapshot.text.encode("utf-8")) > 65536:
            raise ApplicationError("File must contain 1-65536 bytes of UTF-8 text", non_retryable=True)
        if digest_snapshot(snapshot.name, snapshot.text) != snapshot.digest:
            raise ApplicationError("Snapshot digest mismatch", non_retryable=True)
        return snapshot


@activity.defn
async def run_architect_agent(snapshot: FileSnapshot) -> Diagram:
    with activity_span("run_architect_agent", snapshot.traceparent, snapshot.digest, snapshot.name):
        router = Router()
        try:
            return Diagram(snapshot, await generate_diagram(router, snapshot.text))
        except Exception:
            metrics.get_meter("document-workflow").create_counter(
                "copilot_handoff_failures_total"
            ).add(1, {"step": "architect"})
            raise
        finally:
            await router.close()


class DeveloperActivities:
    def __init__(self, publisher: GitHubPublisher) -> None:
        self.publisher = publisher

    @activity.defn(name="run_developer_agent")
    async def run_developer_agent(self, diagram: Diagram) -> str:
        with activity_span("run_developer_agent", diagram.source.traceparent, diagram.source.digest, diagram.source.name):
            try:
                router = Router()
                try:
                    result = await publish_diagram(router, self.publisher, diagram)
                finally:
                    await router.close()
                metrics.get_meter("document-workflow").create_counter(
                    "workflow_files_processed_total"
                ).add(1)
                return result
            except Exception:
                metrics.get_meter("document-workflow").create_counter(
                    "copilot_handoff_failures_total"
                ).add(1, {"step": "developer"})
                raise
