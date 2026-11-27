import asyncio
import os
from datetime import timedelta
from uuid import uuid4

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from orchestrator.models import Diagram, FileSnapshot
from orchestrator.workflow import DocumentToRepoWorkflow


@pytest.mark.skipif(os.getenv("RUN_TEMPORAL_INTEGRATION") != "1", reason="opt-in Temporal test server")
@pytest.mark.asyncio
async def test_retry_resumes_after_completed_ingest() -> None:
    calls = {"ingest": 0, "architect": 0, "developer": 0}
    failed = asyncio.Event()

    @activity.defn(name="ingest_file")
    async def ingest(snapshot: FileSnapshot) -> FileSnapshot:
        calls["ingest"] += 1
        return snapshot

    @activity.defn(name="run_architect_agent")
    async def architect(snapshot: FileSnapshot) -> Diagram:
        calls["architect"] += 1
        if calls["architect"] == 1:
            failed.set()
            raise RuntimeError("transient test failure")
        return Diagram(snapshot, "flowchart TD\nA-->B\n")

    @activity.defn(name="run_developer_agent")
    async def developer(diagram: Diagram) -> str:
        calls["developer"] += 1
        return "diagrams/result.mmd"

    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = f"test-{uuid4()}"
        async with Worker(env.client, task_queue=queue, workflows=[DocumentToRepoWorkflow],
                          activities=[ingest, architect, developer], max_cached_workflows=0):
            handle = await env.client.start_workflow(
                DocumentToRepoWorkflow.run, FileSnapshot("doc.txt", "content", "digest", ""),
                id=f"test-{uuid4()}", task_queue=queue,
            )
            await asyncio.wait_for(failed.wait(), timeout=10)
            await asyncio.sleep(0.5)
        async with Worker(env.client, task_queue=queue, workflows=[DocumentToRepoWorkflow],
                          activities=[ingest, architect, developer], max_cached_workflows=0):
            await env.sleep(timedelta(seconds=10))
            assert await asyncio.wait_for(handle.result(), timeout=30) == "diagrams/result.mmd"
    assert calls == {"ingest": 1, "architect": 2, "developer": 1}
