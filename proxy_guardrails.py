"""Local guardrails for the workflow's non-streaming chat completions."""
import json
import logging
import re
from typing import Any

from fastapi import HTTPException
from litellm import Choices, ModelResponse
from litellm.caching.caching import DualCache
from litellm.integrations.custom_guardrail import CustomGuardrail
from litellm.proxy._types import UserAPIKeyAuth
from litellm.types.guardrails import GuardrailEventHooks
from litellm.types.utils import CallTypesLiteral, LLMResponseTypes


logger = logging.getLogger("document-workflow.guardrails")
SECRET_PATTERNS = (
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\bgh[pousr]_[0-9A-Za-z]{36,}\b"),
    re.compile(r"\bgithub_pat_[0-9A-Za-z_]{22,}\b"),
    re.compile(r"\bsk-[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
)


class GuardrailRejected(HTTPException):
    type = "document_guardrail_violation"

    def __init__(self, guardrail: str, reason: str) -> None:
        self.param = guardrail
        logger.warning("Request blocked: guardrail=%s reason=%s", guardrail, reason)
        super().__init__(status_code=400, detail=f"{guardrail}: {reason}")


class _RequiredGuardrail(CustomGuardrail):
    def should_run_guardrail(
        self, data: dict, event_type: GuardrailEventHooks,
    ) -> bool:
        # Clients cannot bypass these checks using disable_global_guardrail metadata.
        return self._event_hook_is_event_type(event_type)

    def reject(self, reason: str) -> None:
        raise GuardrailRejected(self.guardrail_name or type(self).__name__, reason)


def message_payload(data: dict) -> str:
    return json.dumps(data.get("messages", []), ensure_ascii=False)


class DocumentInputPolicy(_RequiredGuardrail):
    def __init__(self, max_input_bytes: int = 100000, **kwargs: Any) -> None:
        if isinstance(max_input_bytes, bool) or not isinstance(max_input_bytes, int) or max_input_bytes < 1:
            raise ValueError("max_input_bytes must be a positive integer")
        self.max_input_bytes = max_input_bytes
        super().__init__(**kwargs)

    async def async_pre_call_hook(
        self, user_api_key_dict: UserAPIKeyAuth, cache: DualCache,
        data: dict, call_type: CallTypesLiteral,
    ) -> dict:
        if call_type not in ("completion", "acompletion"):
            return data
        if data.get("stream"):
            self.reject("Streaming is disabled so output can be checked before delivery")
        messages = data.get("messages")
        if not isinstance(messages, list) or not messages:
            self.reject("Non-empty text messages are required")
        for message in messages:
            if not isinstance(message, dict) or not isinstance(message.get("content"), str):
                self.reject("Only text message content is supported")
        if len(message_payload(data).encode("utf-8")) > self.max_input_bytes:
            self.reject(f"Serialized messages exceed {self.max_input_bytes} UTF-8 bytes")
        return data


class SecretGuardrail(_RequiredGuardrail):
    def check(self, text: str) -> None:
        if any(pattern.search(text) for pattern in SECRET_PATTERNS):
            self.reject("Possible credential or private key detected; remove it before retrying")

    async def async_pre_call_hook(
        self, user_api_key_dict: UserAPIKeyAuth, cache: DualCache,
        data: dict, call_type: CallTypesLiteral,
    ) -> dict:
        if call_type in ("completion", "acompletion"):
            if data.get("stream"):
                self.reject("Streaming is disabled so output can be checked before delivery")
            self.check(message_payload(data))
        return data

    async def async_post_call_success_hook(
        self, data: dict, user_api_key_dict: UserAPIKeyAuth,
        response: LLMResponseTypes,
    ) -> LLMResponseTypes:
        if isinstance(response, ModelResponse):
            for choice in response.choices:
                if isinstance(choice, Choices):
                    if isinstance(choice.message.content, str):
                        self.check(choice.message.content)
                    for tool_call in choice.message.tool_calls or []:
                        self.check(tool_call.function.arguments)
        return response
