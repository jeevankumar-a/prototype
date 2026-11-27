import json
import unittest
from pathlib import Path

try:
    import litellm
except ModuleNotFoundError as exc:
    if exc.name != "litellm":
        raise
    raise unittest.SkipTest("Run proxy guardrail tests in .venv-proxy") from exc

import yaml
from litellm import ModelResponse
from litellm.caching.caching import DualCache
from litellm.proxy._types import UserAPIKeyAuth
from litellm.types.guardrails import GuardrailEventHooks

from proxy_guardrails import DocumentInputPolicy, GuardrailRejected, SecretGuardrail


class ProxyGuardrailTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.auth = UserAPIKeyAuth()
        self.cache = DualCache()
        self.policy = DocumentInputPolicy(
            guardrail_name="document-input-policy", event_hook="pre_call", default_on=True,
        )
        self.guard = SecretGuardrail(
            guardrail_name="document-secret-guard",
            event_hook=["pre_call", "post_call"], default_on=True,
        )

    async def test_size_boundary_counts_utf8_and_system_messages(self) -> None:
        data = {"messages": [
            {"role": "system", "content": "instruction"},
            {"role": "user", "content": "\u00e9"},
        ]}
        size = len(json.dumps(data["messages"], ensure_ascii=False).encode("utf-8"))
        self.policy.max_input_bytes = size
        self.assertIs(
            await self.policy.async_pre_call_hook(self.auth, self.cache, data, "completion"), data,
        )
        self.policy.max_input_bytes = size - 1
        with self.assertRaises(GuardrailRejected) as error:
            await self.policy.async_pre_call_hook(self.auth, self.cache, data, "completion")
        self.assertEqual(error.exception.status_code, 400)

    async def test_secrets_blocked_in_both_directions_without_echoing(self) -> None:
        for secret in (
            "AIza" + "A" * 35, "ghp_" + "A" * 36,
            "github_pat_" + "A" * 22, "sk-" + "A" * 32,
            "-----BEGIN PRIVATE KEY-----",
        ):
            with self.subTest(secret_type=secret[:4]):
                data = {"messages": [{"role": "user", "content": secret}]}
                with self.assertLogs("document-workflow.guardrails", level="WARNING") as logs:
                    with self.assertRaises(GuardrailRejected) as error:
                        await self.guard.async_pre_call_hook(self.auth, self.cache, data, "completion")
                self.assertNotIn(secret, error.exception.detail)
                self.assertNotIn(secret, "\n".join(logs.output))
                response = ModelResponse(choices=[
                    {"index": 0, "message": {"role": "assistant", "content": secret}},
                ])
                with self.assertRaises(GuardrailRejected):
                    await self.guard.async_post_call_success_hook(data, self.auth, response)

    async def test_ordinary_diagram_and_placeholders_pass_unchanged(self) -> None:
        data = {"messages": [{"role": "user", "content": "GEMINI_API_KEY=replace-me"}]}
        self.assertIs(
            await self.guard.async_pre_call_hook(self.auth, self.cache, data, "completion"), data,
        )
        response = ModelResponse(choices=[
            {"index": 0, "message": {"role": "assistant", "content": "flowchart TD\nA-->B"}},
        ])
        self.assertIs(
            await self.guard.async_post_call_success_hook(data, self.auth, response), response,
        )

    async def test_streaming_and_non_text_rejected(self) -> None:
        for data in (
            {"messages": [{"role": "user", "content": "hello"}], "stream": True},
            {"messages": [{"role": "user", "content": [{"type": "image_url"}]}]},
        ):
            with self.assertRaises(GuardrailRejected):
                await self.policy.async_pre_call_hook(self.auth, self.cache, data, "completion")

    async def test_yaml_loads_through_installed_proxy_and_cannot_be_bypassed(self) -> None:
        from litellm.proxy.guardrails.guardrail_registry import IN_MEMORY_GUARDRAIL_HANDLER
        from litellm.proxy.utils import ProxyLogging

        config_path = Path(__file__).resolve().parents[1] / "litellm_config.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        callbacks = list(litellm.callbacks)
        registry = type(IN_MEMORY_GUARDRAIL_HANDLER)()
        try:
            for item in config["guardrails"]:
                registry.initialize_guardrail(item, config_file_path=str(config_path))
            proxy = ProxyLogging(user_api_key_cache=self.cache)
            data = {
                "messages": [{"role": "user", "content": "ghp_" + "A" * 36}],
                "metadata": {"disable_global_guardrail": True, "guardrails": []},
            }
            with self.assertRaises(Exception) as error:
                await proxy.pre_call_hook(self.auth, data, "completion")
            self.assertEqual(getattr(error.exception, "type", None), "document_guardrail_violation")
            response = ModelResponse(choices=[
                {"index": 0, "message": {"role": "assistant", "content": "ghp_" + "A" * 36}},
            ])
            with self.assertRaises(Exception) as output_error:
                await proxy.post_call_success_hook(
                    data=data, user_api_key_dict=self.auth, response=response,
                )
            self.assertEqual(
                getattr(output_error.exception, "type", None), "document_guardrail_violation",
            )
            for check in (self.policy, self.guard):
                self.assertTrue(check.should_run_guardrail(data, GuardrailEventHooks.pre_call))
        finally:
            litellm.callbacks = callbacks
