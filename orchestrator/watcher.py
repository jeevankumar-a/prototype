import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from opentelemetry import trace
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from observability.telemetry import configure, set_document_attributes, trace_headers
from orchestrator.activities import digest_snapshot
from orchestrator.models import FileSnapshot
from orchestrator.workflow import DocumentToRepoWorkflow

logger = logging.getLogger(__name__)
INPUT = Path("input_files")


async def scan(client: Client, previous: dict[str, str], submitted: dict[str, str]) -> None:
    current: dict[str, str] = {}
    for path in sorted(INPUT.iterdir()):
        if path.name.startswith(".") or path.is_symlink() or not path.is_file():
            continue
        try:
            with path.open("rb") as file:
                raw = file.read(65537)
            if len(raw) > 65536:
                raise ValueError("file exceeds 65536 bytes")
            text = raw.decode("utf-8")
            if not text.strip():
                raise ValueError("file is empty")
            digest = digest_snapshot(path.name, text)
            current[path.name] = digest
            if previous.get(path.name) != digest or submitted.get(path.name) == digest:
                continue
            with trace.get_tracer("document-workflow").start_as_current_span("submit_document") as span:
                set_document_attributes(span, path.name, digest)
                snapshot = FileSnapshot(path.name, text, digest, trace_headers()["traceparent"])
                try:
                    await client.start_workflow(
                        DocumentToRepoWorkflow.run, snapshot,
                        id=f"document-{digest}", task_queue="document-to-repo",
                        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                    )
                    span.set_attribute("langfuse.observation.metadata.outcome", "started")
                    logger.info("Started workflow document-%s", digest)
                except WorkflowAlreadyStartedError:
                    span.set_attribute("langfuse.observation.metadata.outcome", "duplicate")
                    logger.info("Workflow already exists: document-%s", digest)
                submitted[path.name] = digest
        except (OSError, UnicodeError, ValueError):
            logger.exception("Cannot submit input file %s", path)
    previous.clear()
    previous.update(current)


async def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    configure("document-watcher")
    INPUT.mkdir(exist_ok=True)
    interval = float(os.getenv("POLL_INTERVAL_SECONDS", "5"))
    if interval <= 0:
        raise ValueError("POLL_INTERVAL_SECONDS must be positive")
    client = await Client.connect(
        os.getenv("TEMPORAL_ADDRESS", "localhost:7233"),
        namespace=os.getenv("TEMPORAL_NAMESPACE", "default"),
    )
    previous: dict[str, str] = {}
    submitted: dict[str, str] = {}
    while True:
        try:
            await scan(client, previous, submitted)
        except Exception:
            logger.exception("Polling failed; will retry")
        await asyncio.sleep(interval)


if __name__ == "__main__":
    asyncio.run(main())
