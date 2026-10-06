# Requirements: A Basic Agent Loop (Containerized)

A hands-on tutorial project that teaches how an agentic application works by building the smallest useful one: an LLM that can call tools in a loop until a task is done. Everything runs in containers, so the only host prerequisites are Docker and an API key.

## 1. Goals

- Show that an "agent" is a simple loop: **call model → run requested tools → feed results back → repeat until done**.
- Keep the loop explicit and readable (no agent framework) so learners see every step.
- Run the agent and its supporting services as containers with one command.
- Make each iteration visible to learners in a terminal trace and in a web chat UI.

## 2. Non-goals

- Multi-agent orchestration, planning frameworks, or RAG pipelines.
- Production hardening (auth, scaling, multi-tenancy).
- Supporting multiple model providers. One provider (Anthropic Claude API) keeps the code small.
- Telemetry (tracing/metrics). The app ships uninstrumented; adding OpenTelemetry or Grafana SDKs is a follow-up.

## 3. Audience and prerequisites

Technical readers (developers, SREs, data engineers) who can read Python and use a terminal, but have not built an agent before.

Host prerequisites: Docker with Compose v2, an `ANTHROPIC_API_KEY`, and a terminal. No local Python install required.

## 4. Functional requirements

### 4.1 Agent loop

| ID | Requirement |
|----|-------------|
| F1 | Accept a task from the user (CLI argument, interactive prompt, or the web chat UI, see section 5). |
| F2 | Send the conversation (system prompt, messages, tool definitions) to the model. |
| F3 | If the response contains tool-use requests, execute each one and append the results as tool-result messages, then call the model again. |
| F4 | If the response contains no tool-use requests (`stop_reason == "end_turn"`), print the final answer and exit. |
| F5 | Enforce a maximum iteration count (default 10, configurable) to prevent runaway loops; exit with a clear message when hit. |
| F6 | Handle tool errors by returning the error text to the model as a tool result (flagged as an error) instead of crashing, so the model can recover. |
| F7 | Handle model/API errors with bounded retries and backoff (SDK `max_retries=3`), then fail with a clear message. |
| F8 | Report each step (iteration, model text, tool call, tool result, final answer) as events: printed as a human-readable trace by the CLI, streamed by the web API. |
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

- CLI: messages are held in memory for the life of one run.
- Web UI: the browser holds the plain-text chat history (user and assistant turns) and sends it with every request; the server keeps no session state. Tool calls from earlier turns are not replayed.
- Out of scope for the base version: server-side persistence across runs. Mention it as a follow-up exercise (for example, store history in a volume or database).

## 5. Container and service requirements

| ID | Requirement |
|----|-------------|
| C1 | The web stack (`ui`, `agent`, `tools`) starts with `docker compose up` and stops with `docker compose down`. The CLI runs on demand via `docker compose run`. |
| C2 | **`agent`** service: the agent loop behind HTTP for the web UI (see W1). Slim Python base image, non-root user, no published ports. **`agent-cli`** service (profile `cli`): runs the same loop once from the command line as `docker compose run --rm agent-cli "your task"`. It shares its image with `agent`. |
| C3 | **`tools`** service: a small HTTP API (for example FastAPI) exposing `fetch_url` and the note tools. Reachable only on the internal Compose network. Isolating side-effecting tools from the agent teaches the trust boundary. |
| C4 | **`workspace` volume**: named volume mounted into `tools` only, as the sandbox for note files. |
| C6 | The `tools` and `agent` services have healthchecks; `agent` and `agent-cli` wait for `tools`, and `ui` waits for `agent` (`depends_on: condition: service_healthy`). |
| C7 | Configuration comes from environment variables loaded from a `.env` file (`ANTHROPIC_API_KEY`, `MODEL`, `MAX_ITERATIONS`, `TOOLS_URL`, `UI_PORT`). Commit a `.env.example` with placeholder values and comments; never commit `.env`. Compose passes values through with `env_file`, so no connection details are hard-coded in images or `docker-compose.yml`. |
| C8 | Images pin versions (no `latest`). `tools` and `ui` run with a read-only root filesystem (plus `/tmp`, and the workspace volume for `tools`). |
| C9 | Only the agent containers (`agent`, `agent-cli`) receive the API key; `tools` and `ui` must not receive it (give each service its own `env_file`/`environment` list). |

### Web chat UI

| ID | Requirement |
|----|-------------|
| W1 | **`agent`** service (`api.py`): the agent loop behind HTTP (`POST /chat`, `GET /health`), streaming loop events as Server-Sent Events. Holds all secrets; never published to the host. |
| W2 | **`ui`** service: static HTML/JS page served by nginx, proxying `/api/` to `agent`. No `env_file`, no secrets, read-only filesystem. The only service published to the host, bound to `127.0.0.1` (configurable port via `UI_PORT`). |
| W3 | The UI shows each loop step (iteration, model text, tool call, tool result) live and supports multi-turn chat; the browser holds the plain-text history and sends it on every request. |
| W4 | Model and tool output is rendered as text only (no `innerHTML`). Requests are validated (roles, max 50 turns, max 10,000 characters each). |
| W5 | The loop reports progress through an `on_event` callback so the CLI and web API share one implementation. The CLI is an opt-in Compose profile (`cli`) so `docker compose up` starts only `agent`, `ui` and `tools`. |

Known limitation: the page has no authentication, so exposure is limited by binding to localhost. Anyone who can reach it can spend API tokens.

### Target architecture

```
 browser ──► ui (nginx, 127.0.0.1:8080) ──► agent ──┐
                                                    │   (same agent loop in both)
 terminal ──► agent-cli ────────────────────────────┤
                                                    ├──HTTPS──► Claude API
                                                    │
                                                    └──HTTP (Compose network)──► tools ──► workspace volume
                                                                                 └──► the internet (fetch_url)

 Inside Compose: ui, agent, agent-cli, tools, workspace volume.   Outside Compose: Claude API.
 The API key exists only in agent and agent-cli.
```

## 6. Non-functional requirements

- **Readability over cleverness**: type hints, short functions, and comments explaining *why*.
- **Reproducibility**: exact-pinned dependencies (`requirements.txt` with `==` versions) and pinned image tags.
- **Safety**: no `eval`/`exec` of model output; `fetch_url` restricted to `http(s)`, blocks private/link-local address ranges, has a timeout; note tools reject path traversal.
- **Cost control**: iteration cap (F5), a default of a small/cheap model, and token usage printed at the end of each run.
- **Portability**: works on macOS, Linux, and Windows (WSL2) with Docker.
- **Secrets**: `.env` is git-ignored and never copied into an image; no secret is baked into an image or printed in the trace.

## 7. Proposed repository layout

```
my-first-agent/
├── README.md              # concise how-to and reference (see section 8)
├── REQUIREMENTS.md        # this document
├── docker-compose.yml     # ui, agent, tools; plus `agent-cli` (profile: cli) and `test` (profile: test)
├── .env.example
├── ui/
│   ├── Dockerfile
│   ├── nginx.conf
│   └── index.html         # chat page (no framework, no build step)
├── agent/
│   ├── Dockerfile         # targets: runtime (default) and test
│   ├── requirements.txt
│   ├── requirements-dev.txt
│   ├── main.py            # CLI entry point
│   ├── api.py             # HTTP/SSE service for the web UI
│   ├── loop.py            # the agent loop (the star of the show)
│   └── tools.py           # tool definitions + registry
├── tools_service/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app.py             # fetch_url, read_note, write_note
└── tests/
    ├── conftest.py        # import paths
    ├── test_loop.py       # loop logic with a fake model client
    ├── test_api.py        # SSE stream and request validation
    └── test_tools.py      # calculator and tools-service safety checks
```

## 8. Documentation deliverables

The README is a concise reference, not a long tutorial: short sections, tables and diagrams over prose. It contains, in order:

1. **How it works**: a flow diagram of one prompt through the loop (prompt → model decides → use a tool or reply → loop back), using a small worked example (`3 * 7`), plus three bullets on statelessness, who runs tools, and the stop condition.
2. **Quick start**: prerequisites, `.env` setup, the CLI command, and the web chat UI (`docker compose up`).
3. **Architecture**: the service layout and a table of what each service does and which hold secrets, with the localhost-only warning for the UI.
4. **Tools**: a table of the tools and where they run, and a one-line description of how to add one.
5. **Configuration**: a table of the `.env` variables and defaults.
6. **Tests**: the command to run them.
7. **Troubleshooting**: a short symptom/fix table.
8. **Next steps**: a single line (instrumentation, persistent history, streaming, human approval, evals, guardrails, multi-agent).

Detailed explanation lives in the code comments (especially `agent/loop.py`), not in the README.

## 9. Testing and acceptance criteria

- **Unit tests** run in a container (`docker compose run --rm --build test`) using a fake model client that returns scripted responses. They cover: no-tool answer, single tool call, multi-step tool calls, tool error recovery, unknown tools, iteration cap, API failure, history handling, the web API's SSE event stream and request validation, the calculator, and the tools service's URL and path checks.
- **Acceptance**:
  1. From a clean checkout with only Docker and an API key, `docker compose run --rm agent-cli "What is 17 * 23, and what is the title of https://example.com?"` returns a correct answer and the trace shows both tool calls.
  2. A note written in one run can be read in a later run (volume persistence).
  3. `docker compose up --build`, then http://localhost:8080 shows a live chat: asking the question from criterion 1 displays the calculator and fetch_url steps and then the answer; a follow-up question can refer to the earlier answer.
  4. `docker compose config` shows `ANTHROPIC_API_KEY` only for `agent` and `agent-cli` (not `ui` or `tools`), and only `ui` publishes a port, on `127.0.0.1`.
  5. `docker compose down -v` leaves no containers or volumes behind.

## 10. Open decisions (defaults assumed)

| Decision | Default | Alternative |
|----------|---------|-------------|
| Language | Python 3.12 | TypeScript |
| Model provider | Anthropic API | Local model via Ollama container (removes API key and cost) |
| Loop implementation | Hand-written against the Anthropic SDK | SDK tool runner (shorter, hides the loop) |
| Observability | None built in; learner adds OpenTelemetry/Grafana SDKs | Ship pre-instrumented |
