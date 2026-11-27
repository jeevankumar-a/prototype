"""Create a limited LiteLLM team and key; print the key once."""
import os

import httpx
from dotenv import load_dotenv


def main() -> None:
    load_dotenv()
    master = os.environ.get("LITELLM_MASTER_KEY", "")
    if not master.startswith("sk-"):
        raise ValueError("Set LITELLM_MASTER_KEY in .env")
    with httpx.Client(
        base_url="http://localhost:4000",
        headers={"Authorization": f"Bearer {master}"},
        timeout=30,
    ) as client:
        team = client.post("/team/new", json={
            "team_alias": "document-workflow", "models": ["agent-router", "agent-fallback"],
            "max_budget": 1.0, "budget_duration": "30d", "rpm_limit": 5, "tpm_limit": 100000,
        })
        team.raise_for_status()
        key = client.post("/key/generate", json={
            "team_id": team.json()["team_id"], "models": ["agent-router", "agent-fallback"],
            "max_budget": 1.0, "budget_duration": "30d", "rpm_limit": 5, "tpm_limit": 100000,
        })
        key.raise_for_status()
        print("Copy this secret into LITELLM_VIRTUAL_KEY in .env (do not share it):")
        print(key.json()["key"])


if __name__ == "__main__":
    main()
