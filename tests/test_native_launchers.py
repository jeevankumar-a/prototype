import base64
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts import run_collector, run_proxy


def test_proxy_launches_with_localhost_and_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {
        "GEMINI_API_KEY": "test-key",
        "LITELLM_MASTER_KEY": "sk-test",
        "DATABASE_URL": "postgresql://test:test@localhost/test",
        "LANGFUSE_HOST": "https://example.invalid",
        "LANGFUSE_PUBLIC_KEY": "public",
        "LANGFUSE_SECRET_KEY": "secret",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(run_proxy, "load_dotenv", lambda: None)
    execute = Mock()
    monkeypatch.setattr(run_proxy.subprocess, "run", execute)
    run_proxy.main()
    command = execute.call_args.args[0]
    assert command[command.index("--host") + 1] == "127.0.0.1"
    assert os.environ["PYTHONUTF8"] == "1"
    assert os.environ["OTEL_ENDPOINT"] == "http://127.0.0.1:4318/v1/traces"
    assert execute.call_args.kwargs["check"] is True


def test_collector_builds_authorization_and_runs_native_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "public")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "secret")
    monkeypatch.setenv("LANGFUSE_HOST", "https://example.invalid")
    monkeypatch.setattr(run_collector, "load_dotenv", lambda: None)
    monkeypatch.setattr(run_collector.sys, "argv", ["run_collector.py", "collector-test"])
    execute = Mock()
    monkeypatch.setattr(run_collector.subprocess, "run", execute)
    run_collector.main()
    assert os.environ["LANGFUSE_BASIC_AUTH"] == base64.b64encode(b"public:secret").decode()
    execute.assert_called_once_with(
        ["collector-test", f"--config={Path('observability') / 'collector.yaml'}"], check=True
    )


def test_proxy_rejects_missing_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(run_proxy, "load_dotenv", lambda: None)
    with pytest.raises(ValueError, match="DATABASE_URL"):
        run_proxy.main()
