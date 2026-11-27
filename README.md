# Document-to-repository workflow

Headless Python watcher and Temporal worker. A stable UTF-8 document is snapshotted
into Temporal; an architect role generates Mermaid through the local LiteLLM proxy;
a developer role creates a commit directly on a configured GitHub branch through
the GitHub MCP server. Both agent LLM calls use only the proxy and its Gemini
models. There is no custom UI.

**Important design change:** GitHub Copilot CLI makes independent model calls via
GitHub, so it cannot meet the strict Gemini-only proxy requirement. The architect
and developer are Gemini-backed roles, not GitHub Copilot agents. GitHub is used
only for repository access.

The editable profiles are `.github/agents/architect.agent.md` and
`.github/agents/developer.agent.md`, in VS Code's custom-agent format. The
worker reads their Markdown bodies as system instructions; it does not execute
their YAML metadata or invoke Copilot. Python modules still execute the
activities, validate outputs, and publish the diagram. Instructions are read
when each agent runs, so changes can affect retries and in-flight workflows.
Keep output requirements compatible with Python validation.

In VS Code Copilot, these profiles can also be selected independently from the
agent dropdown. That is a separate chat session using its selected Copilot model,
not the worker's LiteLLM routing or automatic publishing. The developer profile
generates a commit subject; only the Python worker performs publishing. Markdown
profiles do not grant or restrict GitHub permissions; the token's GitHub
configuration determines repository access.

**Models and billing:** Gemini 2.5 Flash and Pro are unavailable to some new
users, so this configuration uses Gemini 3.8 Flash with Gemini 3.5 Flash-Lite as
fallback. Google's [pricing page](https://aistudio.google.com/docs/pricing) lists
a free tier for both. Check model availability and free-tier quotas for your
project before use. This configuration cannot enforce the provider's billing
tier. Use an AI Studio project that is genuinely free-tier and not billing-enabled;
LiteLLM budgets are not a guarantee of zero provider charges.

## Prerequisites

- Windows, Python 3.10-3.12 and internet access. This prototype runs natively;
  no Docker installation is needed.
- PostgreSQL running locally or a dedicated managed PostgreSQL database for
  LiteLLM's virtual keys and budgets. LiteLLM will initialize its schema. Use
  a new empty database, not an existing application's database.
- The [OpenTelemetry Collector Contrib](https://github.com/open-telemetry/opentelemetry-collector-releases/releases)
  executable. Download the Windows amd64 archive for v0.120.0, extract it, and
  note the path to `otelcol-contrib.exe`. Do not install it as a system service
  unless you want to manage it separately.
- A free-tier Google AI Studio API key with access to the configured models.
- A Langfuse Cloud free-tier project, or a reachable Langfuse instance accepting
  OTLP HTTP traces. Use the project region's host.
- A GitHub account with write access to the target repository. Ask its owner
  for collaborator access and accept the invitation first. Direct commits fail
  on protected branches; this application does not open pull requests.
- A GitHub fine-grained personal access token restricted to the target
  repository, with repository Contents read and write permission. GitHub allows
  tokens to read public repositories even when restricted. The application
  checks the token format, but cannot independently verify its permissions.
- The official GitHub MCP Server v1.5.0 native binary. The worker starts its
  own MCP process; it does not reuse VS Code's MCP connection or credentials.

## Start (PowerShell)

Run commands from this folder. For example, with PostgreSQL's `bin` directory
on your PATH and an existing PostgreSQL administrator account, create a dedicated
database with `createdb -h localhost -U postgres litellm_prototype`. PostgreSQL
will prompt for that account's password. Prefer a dedicated database role with
only the permissions required for that database. Set `DATABASE_URL` accordingly.

To download the Windows x64 collector into this workspace, run:

```powershell
New-Item -ItemType Directory -Force .local\collector | Out-Null
Invoke-WebRequest -Uri "https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v0.120.0/otelcol-contrib_0.120.0_windows_amd64.tar.gz" -OutFile .local\collector\collector.tar.gz
tar -xzf .local\collector\collector.tar.gz -C .local\collector
```

For ARM64, download the corresponding Windows arm64 archive instead.

Install and verify the official GitHub MCP Server v1.5.0 (Windows x64):

```powershell
New-Item -ItemType Directory -Force .local\github-mcp | Out-Null
$release = "https://github.com/github/github-mcp-server/releases/download/v1.5.0"
Invoke-WebRequest "$release/github-mcp-server_Windows_x86_64.zip" -OutFile .local\github-mcp\server.zip
Invoke-WebRequest "$release/github-mcp-server_1.5.0_checksums.txt" -OutFile .local\github-mcp\checksums.txt
$expected = ((Get-Content .local\github-mcp\checksums.txt | Where-Object { $_ -match "github-mcp-server_Windows_x86_64\.zip$" }) -split "\s+")[0]
if (-not $expected -or (Get-FileHash .local\github-mcp\server.zip -Algorithm SHA256).Hash -ne $expected) { throw "GitHub MCP checksum mismatch" }
Expand-Archive .local\github-mcp\server.zip -DestinationPath .local\github-mcp -Force
```

For ARM64, use the `Windows_arm64` archive and its corresponding checksum.
Use the official release binary.

1. Copy the environment template if needed and edit `.env`. Existing users can
   keep their current `.env`; this command does not overwrite it.

   ```powershell
   if (-not (Test-Path .env)) { Copy-Item .env.example .env }
   notepad .env
   ```

   Set `GEMINI_API_KEY`, an `sk-`-prefixed random `LITELLM_MASTER_KEY`,
   `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST`,
   `GITHUB_PERSONAL_ACCESS_TOKEN` (your fine-grained token),
   `TARGET_REPO` (`owner/repo`), `TARGET_BRANCH`, and `DATABASE_URL` pointing
   at the dedicated PostgreSQL database. URL-encode special characters in the
   database username and password. Leave `LITELLM_VIRTUAL_KEY` empty until step 6.
   The collector launcher derives its authorization header from the Langfuse keys.
   `GITHUB_MCP_SERVER` defaults to `.local\github-mcp\github-mcp-server.exe`;
   change it if the executable is elsewhere. The previous `GITHUB_TOKEN`
   setting is not used. Create a fine-grained token in GitHub settings, select
   your personal account as resource owner and only the intended repository,
   and grant Contents read and write. Keep it secret and rotate it before expiry.

2. Create and install into a local environment (if the environments already
   exist, run the two `pip install` commands to install updated dependencies):

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   python -m venv .venv-proxy
   .\.venv-proxy\Scripts\python.exe -m pip install -r requirements-proxy.txt
   ```

3. Generate the proxy's Prisma client once after installing or upgrading the
   proxy. This command downloads Prisma's tools if necessary. It does not create
   the database; create that separately with PostgreSQL first.

   ```powershell
   $env:PATH = (Resolve-Path .\.venv-proxy\Scripts).Path + ";" + $env:PATH
   $env:DATABASE_URL = "postgresql://unused:unused@localhost:5432/unused"
   .\.venv-proxy\Scripts\prisma.exe generate --schema .\.venv-proxy\Lib\site-packages\litellm\proxy\schema.prisma
   Remove-Item Env:\DATABASE_URL
   ```

   Use a fresh terminal for the remaining commands so that any inherited
   `DATABASE_URL` does not override the value in `.env`.

4. Start these commands in **three separate terminals**, all from this folder:

   ```powershell
   .\.venv\Scripts\python.exe scripts\temporal_dev.py
   ```

   ```powershell
   .\.venv\Scripts\python.exe scripts\run_collector.py .local\collector\otelcol-contrib.exe
   ```

   ```powershell
   .\.venv-proxy\Scripts\python.exe scripts\run_proxy.py
   ```

   The Temporal SDK downloads the official CLI on first start; history persists
   in `.local\temporal.db`. The dev UI is at http://localhost:8233, and gRPC
   is at localhost:7233. The collector listens on localhost:4318 and the proxy
   on localhost:4000. Leave each terminal open. Wait until the proxy reports
   that startup is complete; it applies database migrations and schema changes.
   The proxy should only be pointed at its dedicated database.

5. Check readiness from another terminal:

   ```powershell
   Invoke-WebRequest http://localhost:8233 -UseBasicParsing
   curl.exe http://localhost:4000/health/liveliness
   ```

6. Provision a LiteLLM team and virtual key once:

   ```powershell
   .\.venv\Scripts\python.exe scripts\provision_key.py
   ```

   Copy the printed key into `LITELLM_VIRTUAL_KEY` in `.env`, without quotes or
   extra whitespace. The command creates a new team and key each time; it does
   not update existing ones. Revoke unused keys and teams through LiteLLM's admin
   API. The worker does not use the master key; in a deployed system give it a
   separate environment containing only the secrets it needs.
   The provisioning script configures both key and team with a $1/30-day
   LiteLLM accounting budget and RPM/TPM limits. LiteLLM's cost accounting can
   count nominal model prices even on free-tier calls.

### Sample LiteLLM policy and guardrail

Two local checks are enabled in `litellm_config.yaml`, implemented in
`proxy_guardrails.py`. They require no additional API keys or paid services:

- **`document-input-policy`**: rejects chat requests whose serialized `messages`
  exceed **100,000 UTF-8 bytes**, including system instructions. Edit
  `max_input_bytes` in the YAML to change this ceiling. This is a byte-size
  policy, not a token limit; the existing ingestion limit remains 65,536 bytes.
  Only non-streaming text chat is supported, so output can be checked before
  delivery.
- **`document-secret-guard`**: rejects input or generated output containing
  recognizable Google API keys, GitHub tokens, `sk-` keys, or private-key
  headers. Detection uses local patterns, not an external model. Placeholders
  such as `GEMINI_API_KEY=replace-me` are allowed. This is a sample scanner,
  not complete secret/PII or prompt-injection protection.

Both checks are mandatory for chat completions, including existing virtual keys;
client `disable_global_guardrail` metadata cannot bypass them. Rejections return
HTTP 400 with type `document_guardrail_violation`. The worker marks these as
non-retryable Temporal failures and does not publish a rejected result.
Guardrail warnings contain only the check name and reason, not matched content.
These checks do not sanitize existing Temporal history, input files, or telemetry;
post-call scanning does not guarantee sensitive output was never logged.

Restart the LiteLLM proxy using the existing startup command to load changes.
Restart the worker to load non-retryable error handling. Do not rerun key
provisioning. To demonstrate rejection, submit a document containing a **fake**
token made from `ghp_` followed by 36 `A` characters; never use a real credential.
Use a different filename/content for a corrected document because workflow IDs
are deterministic.

Run the local proxy checks without invoking Gemini or GitHub:

```powershell
.\.venv-proxy\Scripts\python.exe -m unittest discover -s tests -p test_proxy_guardrails.py
```

### Populate LiteLLM's Policies page

Guardrail hooks, key/team budgets, and router settings do not automatically
create entries in the separate **Policies** page. With LiteLLM running with the
current config and the `document-workflow` team already provisioned, run:

```powershell
.\.venv\Scripts\python.exe scripts\provision_policies.py
```

This uses the master key locally to create two database-backed policies:

| Policy name in the UI | Guardrail it references |
| --- | --- |
| `document-input-limits` | `document-input-policy` |
| `document-secret-protection` | `document-secret-guard` |

Each has a team-alias attachment to `document-workflow`. Refresh LiteLLM's
**Policies** page and open either policy to inspect its guardrail and attachment.
These records persist in LiteLLM's PostgreSQL database; adding YAML-only
definitions is not sufficient for this version's database-backed Policies list.

The command reads back and verifies both policies and attachments before reporting
success. It can be rerun after a partial failure without duplicating records.
It does not create or rotate keys, change budgets, overwrite differing policies,
or delete unrelated entries. If the guardrails are missing, restart LiteLLM first;
if the team is missing, complete initial key/team provisioning. Do not reprovision
an existing key just to add these policies.

The two policies expose the existing checks in the policy-management UI.
The mandatory global guardrail hooks remain enabled as defense in depth;
removing a team attachment does not disable those hooks. To change the actual
byte ceiling, edit `max_input_bytes` in the YAML and restart the proxy.

### Start the worker and watcher

7. In two separate terminals:

   ```powershell
   $env:OTEL_EXPORTER_OTLP_ENDPOINT = "http://127.0.0.1:4318"
   .\.venv\Scripts\python.exe -m orchestrator.worker
   ```

   The worker passes your configured token to its MCP subprocess and checks
   authentication at startup; no browser OAuth sign-in is needed. A successful
   profile check does not verify repository access or write permission.

   ```powershell
   $env:OTEL_EXPORTER_OTLP_ENDPOINT = "http://127.0.0.1:4318"
   .\.venv\Scripts\python.exe -m orchestrator.watcher
   ```

   Set the endpoint in **both** terminals before starting the processes:
   an inherited environment variable takes precedence over `.env` and may
   otherwise send application traces to a different collector. These settings
   apply only to the current terminals and their child processes.

8. Submit a test document:

   ```powershell
   Set-Content -Encoding utf8 input_files\example.txt "A client calls an API, which reads a database."
   ```

   After two matching scans, find `document-<digest>` in Temporal's UI. The
   resulting `diagrams/<digest>.mmd` appears as a commit in the target branch.
   View trace and token information in Langfuse. Stop the watcher, worker,
   proxy, collector, and Temporal server with Ctrl+C in their terminals.
   PostgreSQL is managed separately. The Temporal database remains on disk.

## Behavior and operations

- Input is top-level, non-hidden, regular UTF-8 text files, at most 64 KiB.
  Symlinks are skipped. Write a temporary hidden file and atomically rename it
  into place to avoid submitting a partially written document. Two identical
  scans are only a heuristic, not a file lock.
- Identity is SHA-256 of filename, a zero byte, and exact UTF-8 bytes. The
  workflow ID is deterministic and rejects duplicate starts while retained in
  Temporal; files are not moved or deleted. Changing contents or filename starts
  another workflow. Workflow ID deduplication is bounded by Temporal's retention
  period, not permanent storage.
- Workflow history stores the input, intermediate diagram, retries and final path.
  Activity retries use exponential backoff and stop after eight attempts. Once
  retries are exhausted, inspect the failure in the Temporal UI and use Temporal's
  workflow reset operation to resume from an appropriate prior event; simply
  restarting the watcher does not retry an already existing failed workflow.
  Retain workflow-compatible code when deploying changes.
- Activities run at least once. A worker crash after an LLM response but before
  Temporal records it can repeat the LLM call and produce a different response.
  Publishing uses a deterministic path and compares existing content: a retry
  after a successful commit is a no-op, and conflicting content fails rather
  than overwriting. This is not a distributed exactly-once transaction.
  The worker allows only the `get_me`, `get_file_contents`, and
  `create_or_update_file` MCP tools. Restart the worker if its MCP subprocess
  terminates or after changing the token. Token restrictions are configured on
  GitHub, not enforced by `TARGET_REPO` or the agent profiles. MCP cannot bypass
  repository permissions or branch protection.
  Generated Mermaid is model output, not syntactically or security validated;
  review it before rendering or relying on it. Input is untrusted and may contain
  prompt injection; system instructions alone are not a security boundary.
- `workflow_files_processed_total` counts successful publishing activity
  attempts, not exact unique workflows; retries can overcount.
  `agent_step_duration_seconds` measures activity attempts, and
  `copilot_handoff_failures_total` is retained as a metric name for compatibility
  but counts Gemini-role failures, not actual Copilot calls.
  The collector terminal logs metric names and values at detailed verbosity;
  the worker exports them approximately every 60 seconds by default. Metrics
  are aggregated by step, not filename, and no metric history is stored.
- The watcher propagates a W3C trace context as workflow input; activities create
  child spans and forward it on proxy requests. LiteLLM has both `otel` and
  `langfuse_otel` callbacks: the collector sends OTLP traces to Langfuse and metrics
  to its debug exporter (collector logs), while LiteLLM's Langfuse OTEL integration
  supplies generation/usage data. The older `langfuse` callback uses a legacy
  ingestion API removed from Langfuse Cloud. Duplicate observations are possible;
  exact correlation and Langfuse ingestion depend on the pinned proxy and Langfuse
  versions and should be checked in the dashboard. No metrics backend is bundled.
  Callback logging may transmit prompts and responses to Langfuse even though
  the OTEL content capture setting is disabled.
- For new submissions, Langfuse trace metadata includes `filename` and
  `workflow_id`; use them to distinguish documents and match Temporal's UI.
  The workflow ID is also the Langfuse session ID on watcher and activity
  observations, grouping each document workflow under Sessions. Different
  documents have different sessions; retries share the same session.
  Proxy-generated spans may not have the session ID.
  The submission observation records `started` or `duplicate`. Activity
  observations record `outcome`, Temporal run ID and attempt number; failed
  attempts are error-level observations. In Langfuse, open Tracing to inspect
  a document, its activity durations and errors, and use observation analytics
  to aggregate by activity name or error level. Successful developer activity
  indicates publishing succeeded, not a separately recorded final workflow
  status. These counts describe attempts, not unique completed workflows, and
  retries, crashes, exporter loss or duplicate callbacks can affect results.
  Existing traces are not updated. Filenames are sent to Langfuse; don't use
  sensitive filenames. Proxy-generated spans may lack these metadata fields.
- Default deployment limits (3.8 Flash 5 RPM/100000 TPM, 3.5 Flash-Lite 1 RPM/10000 TPM) are
  conservative examples, **not** verified limits for every project or region.
  Check AI Studio's per-model RPM, TPM and daily limits and lower them as needed.
  Per-minute limits do not enforce daily quota; Gemini may return 429. Multiple
  proxy replicas need shared rate-limit state. Retries consume quota.
- The Temporal dev server uses a persistent local SQLite file but is not
  production HA, has no TLS/authentication, and is not a substitute for a
  production Temporal deployment. This native setup is a prototype, not a
  production deployment: use managed/HA Temporal and PostgreSQL, backups,
  secured network/TLS and secret storage, production monitoring, bounded retention,
  and validated image/dependency upgrades before operating it in production.
  Publishing straight to a branch is unsuitable where review is required.
- Documents and model output are sent to Gemini, GitHub, and potentially Langfuse;
  workflow history contains their plaintext. Google's free tier may use content to
  improve its products. Do not process sensitive information without an approved
  data policy. `.env` and input files are excluded from Git, but protect local
  permissions, volumes, console output and observability backends too.

## Upload this project to GitHub

Create a new empty GitHub repository (preferably private). Do not initialize it
with a README, license, or `.gitignore`, because this folder already contains
project files. From PowerShell, replace the remote URL below with your repository:

```powershell
Set-Location "C:\Users\JeevanKumarAnanthula\Desktop\t+l+lf"
git init
git add --all
git status --short
git diff --cached --stat
```

Review the staged file list before proceeding. `.gitignore` excludes local
environment files, virtual environments, downloaded services, input documents,
runtime databases/logs, and Python caches. It keeps `.env.example` and the
input directory's `.gitkeep`. Ignore rules do not remove previously tracked files
or detect secrets embedded in source; review your files and ensure the environment
template contains placeholders only.

```powershell
git commit -m "Initial commit: headless document workflow" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git push -u origin main
```

Authenticate through Git's credential manager/browser prompt; never put a token
in the remote URL or commit it. The workflow's repository-scoped GitHub token
may not grant access to this new repository. If Git requires an author identity,
set `git config user.name` and `git config user.email` for this repository, using
your preferred name and GitHub privacy/noreply email.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Tests use fake LLM and GitHub responses; a real end-to-end check requires the
services, credentials and quotas above. To additionally exercise a real
Temporal test server (the SDK downloads a test binary on first use):

```powershell
$env:RUN_TEMPORAL_INTEGRATION = "1"
.\.venv\Scripts\python.exe -m pytest -q tests\test_temporal_integration.py
```
