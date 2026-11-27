"""Persist the sample policies for LiteLLM's Policies UI without creating keys."""
import os
from typing import Any

import httpx
from dotenv import load_dotenv


TEAM_ALIAS = "document-workflow"
POLICIES = (
    {
        "policy_name": "document-input-limits",
        "description": "Limit serialized chat input to 100,000 UTF-8 bytes; require non-streaming text.",
        "guardrails_add": ["document-input-policy"],
        "guardrails_remove": [],
    },
    {
        "policy_name": "document-secret-protection",
        "description": "Block recognizable credentials and private keys in chat input and output.",
        "guardrails_add": ["document-secret-guard"],
        "guardrails_remove": [],
    },
)


def get_records(
    client: httpx.Client, path: str, field: str | None = None,
) -> list[dict[str, Any]]:
    response = client.get(path)
    response.raise_for_status()
    payload = response.json()
    if field is not None:
        if not isinstance(payload, dict) or field not in payload:
            raise ValueError(f"{path} did not return '{field}'")
        payload = payload[field]
    if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
        raise ValueError(f"{path} did not return a list of records")
    return payload


def matches_policy(record: dict[str, Any], expected: dict[str, Any]) -> bool:
    return (
        record.get("policy_name") == expected["policy_name"]
        and record.get("guardrails_add") == expected["guardrails_add"]
        and not record.get("guardrails_remove")
        and not record.get("inherit")
        and not record.get("condition")
    )


def matches_attachment(record: dict[str, Any], policy_name: str) -> bool:
    return (
        record.get("policy_name") == policy_name
        and record.get("teams") == [TEAM_ALIAS]
        and not record.get("scope")
        and not record.get("keys")
        and not record.get("models")
    )


def provision(client: httpx.Client) -> None:
    loaded = {
        item.get("guardrail_name")
        for item in get_records(client, "/guardrails/list", "guardrails")
    }
    required = {name for policy in POLICIES for name in policy["guardrails_add"]}
    if not required.issubset(loaded):
        raise ValueError("Sample guardrails are not loaded. Restart LiteLLM with litellm_config.yaml first.")
    teams = get_records(client, "/team/list")
    if not any(team.get("team_alias") == TEAM_ALIAS for team in teams):
        raise ValueError("The document-workflow team is missing. Provision the team/key first.")

    policies = get_records(client, "/policies/list", "policies")
    attachments = get_records(client, "/policies/attachments/list", "attachments")
    for expected in POLICIES:
        existing = [item for item in policies if item.get("policy_name") == expected["policy_name"]]
        if existing and (len(existing) != 1 or not matches_policy(existing[0], expected)):
            raise ValueError(
                f"Existing policy '{expected['policy_name']}' differs from the sample; no policies changed."
            )

    for expected in POLICIES:
        name = expected["policy_name"]
        if not any(item.get("policy_name") == name for item in policies):
            response = client.post("/policies", json=expected)
            response.raise_for_status()
        if not any(matches_attachment(item, name) for item in attachments):
            response = client.post("/policies/attachments", json={
                "policy_name": name, "teams": [TEAM_ALIAS],
            })
            response.raise_for_status()

    persisted = get_records(client, "/policies/list", "policies")
    attached = get_records(client, "/policies/attachments/list", "attachments")
    for expected in POLICIES:
        name = expected["policy_name"]
        if not any(matches_policy(item, expected) for item in persisted):
            raise RuntimeError(f"Policy '{name}' was not persisted as expected")
        if not any(matches_attachment(item, name) for item in attached):
            raise RuntimeError(f"Policy '{name}' was not attached to {TEAM_ALIAS}")
    print("Verified persisted LiteLLM policies attached to document-workflow:")
    for expected in POLICIES:
        print(f"  {expected['policy_name']}")
    print("Refresh the LiteLLM Policies page. No keys, budgets, or existing policies were modified.")


def main() -> None:
    load_dotenv()
    master = os.environ.get("LITELLM_MASTER_KEY", "")
    if not master.startswith("sk-"):
        raise ValueError("Set LITELLM_MASTER_KEY in .env")
    with httpx.Client(
        base_url="http://localhost:4000",
        headers={"Authorization": f"Bearer {master}"},
        timeout=30, trust_env=False, follow_redirects=False,
    ) as client:
        provision(client)


if __name__ == "__main__":
    main()
