"""Start the locally installed LiteLLM proxy with the project environment."""
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv


def main() -> None:
    load_dotenv()
    required = ("GEMINI_API_KEY", "LITELLM_MASTER_KEY", "DATABASE_URL",
                "LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")
    missing = [key for key in required if not os.getenv(key) or os.environ[key] == "replace-me"]
    if missing:
        raise ValueError(f"Configure these values in .env: {', '.join(missing)}")
    executable = Path(sys.executable).with_name("litellm.exe" if os.name == "nt" else "litellm")
    os.environ["PATH"] = str(executable.parent) + os.pathsep + os.environ.get("PATH", "")
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    os.environ.setdefault("OTEL_EXPORTER", "otlp_http")
    os.environ.setdefault("OTEL_ENDPOINT", "http://127.0.0.1:4318/v1/traces")
    os.environ.setdefault("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "NO_CONTENT")
    subprocess.run(
        [str(executable), "--config", "litellm_config.yaml", "--host", "127.0.0.1", "--port", "4000",
         "--num_workers", "1", "--telemetry", "False"],
        check=True,
    )


if __name__ == "__main__":
    main()
