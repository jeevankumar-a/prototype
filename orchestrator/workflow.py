from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from orchestrator.models import Diagram, FileSnapshot


@workflow.defn
class DocumentToRepoWorkflow:
    @workflow.run
    async def run(self, snapshot: FileSnapshot) -> str:
        retry = RetryPolicy(
            initial_interval=timedelta(seconds=5),
            maximum_interval=timedelta(minutes=5),
            maximum_attempts=8,
        )
        workflow.logger.info("Starting document workflow", extra={"digest": snapshot.digest})
        valid = await workflow.execute_activity(
            "ingest_file", snapshot, result_type=FileSnapshot,
            start_to_close_timeout=timedelta(seconds=30), retry_policy=retry,
        )
        diagram = await workflow.execute_activity(
            "run_architect_agent", valid, result_type=Diagram,
            start_to_close_timeout=timedelta(minutes=5), retry_policy=retry,
        )
        path = await workflow.execute_activity(
            "run_developer_agent", diagram, result_type=str,
            start_to_close_timeout=timedelta(minutes=5), retry_policy=retry,
        )
        workflow.logger.info("Document published", extra={"path": path})
        return path
