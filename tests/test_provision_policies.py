import json

import httpx
import pytest

from scripts.provision_policies import POLICIES, provision


class PolicyAPI:
    def __init__(self) -> None:
        self.policies: list[dict] = []
        self.attachments: list[dict] = []
        self.writes: list[str] = []
        self.guardrails_loaded = True
        self.team_exists = True
        self.fail_attachment = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET":
            payloads = {
                "/guardrails/list": {"guardrails": [
                    {"guardrail_name": name}
                    for policy in POLICIES for name in policy["guardrails_add"]
                ] if self.guardrails_loaded else []},
                "/team/list": [{"team_alias": "document-workflow"}] if self.team_exists else [],
                "/policies/list": {"policies": self.policies},
                "/policies/attachments/list": {"attachments": self.attachments},
            }
            return httpx.Response(200, json=payloads[path])
        self.writes.append(path)
        if path == "/policies/attachments" and self.fail_attachment:
            return httpx.Response(500, json={"error": "Database unavailable"})
        data = json.loads(request.content)
        if path == "/policies":
            self.policies.append(data)
        elif path == "/policies/attachments":
            self.attachments.append(data)
        else:
            raise AssertionError(f"Unexpected mutation: {path}")
        return httpx.Response(200, json=data)

    def client(self) -> httpx.Client:
        return httpx.Client(base_url="http://localhost:4000", transport=httpx.MockTransport(self.handle))


def test_creates_and_verifies_policies_without_duplicates(capsys: pytest.CaptureFixture) -> None:
    api = PolicyAPI()
    unrelated = {"policy_name": "unrelated", "guardrails_add": ["other"]}
    api.policies.append(unrelated.copy())
    with api.client() as client:
        provision(client)
        first_writes = list(api.writes)
        provision(client)
    assert api.writes == first_writes
    assert len(api.policies) == 3
    assert len(api.attachments) == 2
    assert api.policies[0] == unrelated
    assert all(item["teams"] == ["document-workflow"] for item in api.attachments)
    assert "Verified persisted" in capsys.readouterr().out


def test_recovers_partial_provisioning_without_recreating_policy() -> None:
    api = PolicyAPI()
    api.fail_attachment = True
    with api.client() as client:
        with pytest.raises(httpx.HTTPStatusError):
            provision(client)
        api.fail_attachment = False
        provision(client)
    assert len(api.policies) == 2
    assert len(api.attachments) == 2
    assert api.writes.count("/policies") == 2


@pytest.mark.parametrize("missing", ["guardrails", "team"])
def test_preflight_failure_makes_no_changes(missing: str) -> None:
    api = PolicyAPI()
    api.guardrails_loaded = missing != "guardrails"
    api.team_exists = missing != "team"
    with api.client() as client:
        with pytest.raises(ValueError):
            provision(client)
    assert api.writes == []


def test_conflicting_existing_policy_is_not_overwritten() -> None:
    api = PolicyAPI()
    api.policies.append({"policy_name": POLICIES[1]["policy_name"], "guardrails_add": ["other"]})
    with api.client() as client:
        with pytest.raises(ValueError, match="differs"):
            provision(client)
    assert api.writes == []


def test_does_not_report_success_when_readback_is_missing(capsys: pytest.CaptureFixture) -> None:
    api = PolicyAPI()

    def handler(request: httpx.Request) -> httpx.Response:
        response = api.handle(request)
        if request.method == "POST":
            api.policies.clear()
            api.attachments.clear()
        return response

    with httpx.Client(base_url="http://localhost:4000", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="not persisted"):
            provision(client)
    assert "Verified persisted" not in capsys.readouterr().out
