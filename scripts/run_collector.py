"""Start an installed OpenTelemetry collector using the project environment."""
import base64
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv


def main() -> None:
    load_dotenv()
    public = os.getenv("LANGFUSE_PUBLIC_KEY", "")
    secret = os.getenv("LANGFUSE_SECRET_KEY", "")
    host = os.getenv("LANGFUSE_HOST", "")
    if not public or not secret or not host or "replace-me" in (public, secret):
        raise ValueError("Set LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY in .env")
    os.environ["LANGFUSE_BASIC_AUTH"] = base64.b64encode(f"{public}:{secret}".encode()).decode()
    executable = sys.argv[1] if len(sys.argv) > 1 else "otelcol-contrib"
    subprocess.run([executable, f"--config={Path('observability') / 'collector.yaml'}"], check=True)


if __name__ == "__main__":
    main()
