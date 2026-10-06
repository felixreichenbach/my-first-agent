# Requirements: A Basic Agent Loop (Containerized)

A hands-on tutorial project that teaches how an agentic application works by building the smallest useful one: an LLM that can call tools in a loop until a task is done. Everything runs in containers, so the only host prerequisites are Docker and an API key.

## 1. Goals

- Show that an "agent" is a simple loop: **call model → run requested tools → feed results back → repeat until done**.
- Keep the loop explicit and readable (no agent framework) so learners see every step.
- Run the agent and its supporting services as containers with one command.
- Make the loop observable, so learners can watch each iteration happen.

## 2. Non-goals

- Multi-agent orchestration, planning frameworks, or RAG pipelines.
- Production hardening (auth, scaling, multi-tenancy).
- Supporting multiple model providers. One provider (Anthropic Claude API) keeps the code small.

## 3. Audience and prerequisites

Technical readers (developers, SREs, data engineers) who can read Python and use a terminal, but have not built an agent before.

Host prerequisites: Docker with Compose v2, an `ANTHROPIC_API_KEY`, and a terminal. No local Python install required.

## 4. Functional requirements

### 4.1 Agent loop

| ID | Requirement |
|----|-------------|
| F1 | Accept a task from the user (CLI argument or interactive prompt). |
| F2 | Send the conversation (system prompt, messages, tool definitions) to the model. |
| F3 | If the response contains tool-use requests, execute each one and append the results as tool-result messages, then call the model again. |
| F4 | If the response contains no tool-use requests (`stop_reason == "end_turn"`), print the final answer and exit. |
| F5 | Enforce a maximum iteration count (default 10, configurable) to prevent runaway loops; exit with a clear message when hit. |
| F6 | Handle tool errors by returning the error text to the model as a tool result (flagged as an error) instead of crashing, so the model can recover. |
| F7 | Handle model/API errors with bounded retries and backoff, then fail with a clear message. |
| F8 | Print each step (model request, tool call, tool result, final answer) in a human-readable trace. |
| F9 | Keep the loop in a single, short, well-commented module (target: under ~150 lines). |

### 4.2 Tools

Provide a small set of tools that illustrate different tool patterns. Each tool has a name, description, and JSON-schema input definition.

| ID | Tool | Purpose | Where it runs |
|----|------|---------|---------------|
| T1 | `calculator` | Evaluate arithmetic safely (no `eval`). Shows a pure, local function. | In the agent container |
| T2 | `fetch_url` | Fetch a web page and return trimmed text. Shows a tool with network side effects and output truncation. | In the tools service |
| T3 | `read_note` / `write_note` | Read and write text files in a sandboxed workspace. Shows state that persists across runs. | In the tools service, backed by a volume |

- T-NF1: Adding a new tool must require only one new function and one registry entry (no loop changes). Document this as a tutorial exercise.
- T-NF2: Tool output is size-limited (default 10 KB) with an explicit truncation marker.

### 4.3 Conversation state

- Messages are held in memory for the life of one run.
- Out of scope for the base version: persistence across runs. Mention it as a follow-up exercise (for example, store history in a volume or database).

## 5. Container and service requirements

| ID | Requirement |
|----|-------------|
| C1 | The whole stack starts with `docker compose up` and stops with `docker compose down`. |
| C2 | **`agent`** service: runs the loop. Slim Python base image, non-root user, no published ports. Run as a one-shot task (`docker compose run --rm agent "your task"`). |
| C3 | **`tools`** service: a small HTTP API (for example FastAPI) exposing `fetch_url` and the note tools. Reachable only on the internal Compose network. Isolating side-effecting tools from the agent teaches the trust boundary. |
| C4 | **`workspace` volume**: named volume mounted into `tools` only, as the sandbox for note files. |
| C5 | **Grafana is not part of the Compose stack.** The agent exports telemetry to any external OTLP endpoint (Grafana Cloud, a self-hosted Grafana/Alloy/Tempo, or an OTel Collector) configured purely through environment variables (see section 6). If no endpoint is configured, telemetry is disabled and the agent runs normally. |
| C6 | The `tools` service has a healthcheck, and `agent` waits for it (`depends_on: condition: service_healthy`). |
| C7 | Configuration comes from environment variables loaded from a `.env` file (`ANTHROPIC_API_KEY`, `MODEL`, `MAX_ITERATIONS`, `TOOLS_URL`, plus the OTLP variables in section 6). Commit a `.env.example` with placeholder values and comments; never commit `.env`. Compose passes values through with `env_file`, so no connection details are hard-coded in images or `docker-compose.yml`. |
| C8 | Images pin versions (no `latest`), and `tools` runs with a read-only root filesystem except for the workspace volume. |
| C9 | Only `agent` receives the API key and the OTLP credentials; `tools` must not receive either (give each service its own `env_file`/`environment` list). |

### Target architecture

```
 user ──► agent container ──HTTPS──► Claude API
              │   │
              │   └──OTLP (endpoint + auth from .env)──► external Grafana
              │                        (Cloud, self-hosted, or Collector/Alloy)
              │
              └──HTTP (internal network)──► tools container ──► workspace volume
                                                   │
                                                   └──► the internet (fetch_url)

 Inside Compose: agent, tools, workspace volume.   Outside Compose: Claude API, Grafana.
```

## 6. Observability requirements

| ID | Requirement |
|----|-------------|
| O1 | Instrument with OpenTelemetry. One trace per run; one span per loop iteration; child spans for each model call and each tool call. |
| O2 | Span attributes: model, iteration number, tool name, stop reason, input/output token counts, error status. |
| O3 | Do not record full prompts or tool outputs by default (they may contain secrets); gate that behind `OTEL_CAPTURE_CONTENT=true`. |
| O4 | The tutorial includes a step to open Grafana, find the trace, and relate each span back to a loop iteration. |
| O5 | **Connection is configured only via standard OpenTelemetry environment variables** in `.env`; the code contains no Grafana-specific settings. |
| O6 | If `OTEL_EXPORTER_OTLP_ENDPOINT` is unset, telemetry is a no-op (traces optionally printed to the console with `OTEL_TRACES_EXPORTER=console`). A misconfigured or unreachable endpoint must log a warning and never fail or slow the agent run. |

### Grafana connection variables (`.env`)

| Variable | Purpose | Example |
|----------|---------|---------|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP base URL | Cloud: `https://otlp-gateway-<zone>.grafana.net/otlp` · Local Collector/Alloy on the host: `http://host.docker.internal:4318` |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | Transport | `http/protobuf` |
| `OTEL_EXPORTER_OTLP_HEADERS` | Auth | Cloud: `Authorization=Basic <base64(instanceID:token)>` (note: URL-encode the space as `%20` if the SDK requires it) |
| `OTEL_SERVICE_NAME` | Service name in Grafana | `my-first-agent` |
| `OTEL_RESOURCE_ATTRIBUTES` | Optional labels | `deployment.environment=dev` |
| `OTEL_CAPTURE_CONTENT` | Record prompts/tool output (see O3) | `false` |

`.env.example` documents each variable, with the Grafana Cloud values (where to find the OTLP endpoint and how to generate the token) and a local-collector alternative. The tutorial notes that `localhost` inside a container is the container itself, so a host-side Grafana stack needs `host.docker.internal` (with `extra_hosts: host.docker.internal:host-gateway` on Linux) or a shared Docker network.

## 7. Non-functional requirements

- **Readability over cleverness**: type hints, short functions, and comments explaining *why*.
- **Reproducibility**: pinned dependencies (`requirements.txt` with hashes or a lockfile) and pinned image tags.
- **Safety**: no `eval`/`exec` of model output; `fetch_url` restricted to `http(s)`, blocks private/link-local address ranges, has a timeout; note tools reject path traversal.
- **Cost control**: iteration cap (F5), a default of a small/cheap model, and token usage printed at the end of each run.
- **Portability**: works on macOS, Linux, and Windows (WSL2) with Docker.
- **Secrets**: `.env` is git-ignored (and docker-ignored); no secret is baked into an image or printed in the trace.

## 8. Proposed repository layout

```
my-first-agent/
├── README.md              # the how-to (see section 9)
├── REQUIREMENTS.md        # this document
├── docker-compose.yml
├── .env.example
├── agent/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── main.py            # CLI entry point
│   ├── loop.py            # the agent loop (the star of the show)
│   ├── tools.py           # tool definitions + registry
│   └── telemetry.py       # OpenTelemetry setup
├── tools_service/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app.py             # fetch_url, read_note, write_note
└── tests/
    ├── test_loop.py       # loop logic with a fake model client
    └── test_tools.py
```

## 9. Documentation deliverables (the "how-to")

The README walks a learner through, in order:

1. **Concepts**: what an agent is, the loop diagram, messages / tool definitions / tool results / stop reasons.
2. **Run it**: clone, copy `.env.example`, `docker compose run --rm agent "…"`, with sample tasks that need 0, 1, and several tool calls.
3. **Read the loop**: annotated walkthrough of `loop.py`.
4. **Add a tool**: guided exercise (for example, a `current_time` tool).
5. **Break it on purpose**: trigger the iteration cap, a tool error, and a bad tool input, and see how the loop responds.
6. **Observe it**: put your Grafana OTLP endpoint and credentials in `.env`, re-run the agent, and inspect the trace in Grafana.
7. **Where next**: persistent memory, streaming, human approval for risky tools, evals, guardrails, multi-agent.

## 10. Testing and acceptance criteria

- **Unit tests** run in a container (`docker compose run --rm agent pytest`) using a fake model client that returns scripted responses. They cover: no-tool answer, single tool call, multi-step tool calls, tool error recovery, and iteration cap.
- **Acceptance**:
  1. From a clean checkout with only Docker and an API key, `docker compose run --rm agent "What is 17 * 23, and what is the title of https://example.com?"` returns a correct answer and the trace shows both tool calls.
  2. A note written in one run can be read in a later run (volume persistence).
  3. With OTLP variables set in `.env` pointing at an external Grafana, a trace for the run is visible with per-iteration spans; with them unset, the agent still runs and prints a note that telemetry is disabled.
  4. `docker compose down -v` leaves no containers or volumes behind.

## 11. Open decisions (defaults assumed)

| Decision | Default | Alternative |
|----------|---------|-------------|
| Language | Python 3.12 | TypeScript |
| Model provider | Anthropic API | Local model via Ollama container (removes API key and cost) |
| Loop implementation | Hand-written against the Anthropic SDK | SDK tool runner (shorter, hides the loop) |
| Observability | OpenTelemetry → external Grafana via `.env` | Plain structured logs only |
