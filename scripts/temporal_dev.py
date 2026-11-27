"""Start a local Temporal dev server with a persistent SQLite database."""
import asyncio
from pathlib import Path

from temporalio.testing import WorkflowEnvironment


async def main() -> None:
    data = Path(".local")
    data.mkdir(exist_ok=True)
    async with await WorkflowEnvironment.start_local(
        ip="127.0.0.1",
        port=7233,
        ui=True,
        ui_port=8233,
        dev_server_database_filename=str((data / "temporal.db").resolve()),
    ):
        print("Temporal ready: localhost:7233; UI: http://localhost:8233", flush=True)
        await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
