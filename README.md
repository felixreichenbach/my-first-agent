# my-first-agent (Langchain)

A hands-on tutorial for building an agentic application: an LLM that calls tools in a loop until a task is done. The loop is run by LangChain's `create_agent` (see the note below), the app runs in containers, and it can be watched in a terminal or a web chat UI.

> **Branch `langchain-variant`.** The same application, but the agent loop is not hand-written:
> LangChain's `create_agent` runs it. Everything else (tools service, web UI, CLI, Compose setup)
> is unchanged. Compare with `main`, where the loop is explicit. See [Differences from `main`](#differences-from-main).

Design and requirements: [REQUIREMENTS.md](REQUIREMENTS.md)

## How it works

```
        ┌─────────────────────────────────────────────────────────┐
        │ 1. User prompt: "3 * 7 = ?"                             │
        └────────────────────────────┬────────────────────────────┘
                                     ▼
        ┌─────────────────────────────────────────────────────────┐
   ┌───►│ 2. Model decides:                                       │
   │    │    do I need a tool?                                    │
   │    └──────────┬──────────────────────────────┬───────────────┘
   │           yes │                              │ no
   │               ▼                              ▼
   │    ┌─────────────────────┐   ┌───────────────────────────────┐
   │    │ 3. Run the tool     │   │ 4. Reply to user              │
   │    │    e.g. calculator: │   │    "3 * 7 = 21"               │
   │    │    3 * 7 → 21       │   │    then wait for next prompt  │
   │    │                     │   │                               │
   │    └──────────┬──────────┘   └───────────────┬───────────────┘
   │               │                              ▼
   │               │                       reply is added
   └───────────────┘                     to the conversation

```

The loop repeats until the model replies without asking for a tool. The user can then send another prompt, which starts the cycle again with the earlier messages included. The result from each tool is added to the conversation, so the model sees it the next time it decides.

- The API is stateless: every call resends the full message history.
- The model never runs anything. It requests a tool; your code runs it and returns the result.
- A response with no tool request ends the loop for that prompt. `MAX_ITERATIONS` caps runaway loops.

`create_agent` (LangChain 1.x, built on LangGraph) runs this loop. [agent/loop.py](agent/loop.py) configures it, caps the iterations, and turns its stream into the events the CLI and web UI show.

## Quick start

Requires Docker with Compose v2 and an API key for your model provider (default: an [Anthropic API key](https://console.anthropic.com/)).

```bash
cp .env.example .env     # set ANTHROPIC_API_KEY (or the key of the provider you use)
```

**CLI** (trace on stderr, answer on stdout). Add `--build` after changing code: `up --build` doesn't rebuild this image.

```bash
docker compose run --rm agent-cli "What is 17 * 23, and what is the title of https://example.com?"
```

**Web chat UI** at http://localhost:8080 (change with `UI_PORT`):

```bash
docker compose up --build -d
docker compose down          # stop; add -v to delete saved notes
```

## Architecture

```
                ┌─────────┐
                │ browser │
                └─────────┘
                     │  http://localhost:8080
 ┌───────────────────┼─── Docker Compose network: my-first-agent_default ─────────┐
 │                   ▼                                                            │
 │    ┌─────────────────────────────┐                                             │
 │    │ ui  (nginx-unprivileged)    │                                             │
 │    │ published 127.0.0.1:8080    │                                             │
 │    └──────────────┬──────────────┘                                             │
 │                   │  /api/ → http://agent:8080 (SSE)                           │
 │                   ▼                                                            │
 │    ┌─────────────────────────────┐   ┌──────────────────────────────┐          │
 │    │ agent  (API mode)           │   │ agent-cli  (CLI mode)        │          │
 │    │ uvicorn :8080               │   │ profile: cli                 │          │
 │    │ provider API key            │   │ provider API key             │          │
 │    └──────────────┬──────────────┘   └──────────────┬───────────────┘          │
 │                   └────────────────┬────────────────┘                          │
 │                                    │  same agent loop in both                  │
 │                                    ├─────────────────────────────────────┐     │
 │                                    ▼  http://tools:8000            HTTPS │     │
 │                   ┌────────────────────────────────┐                     │     │
 │                   │ tools  (uvicorn :8000)         │                     │     │
 │                   │ volume workspace → /workspace  │                     │     │
 │                   │ read-only fs, no secrets       │                     │     │
 │                   └────────────────────────────────┘                     │     │
 │                                                                          │     │
 └────────────────────────────────────┬─────────────────────────────────────┬─────┘
                                      │  public addresses only              ▼
                                      ▼                                Model API
                           public web (fetch_url)                   default: Anthropic
```

| Service | Role | Secrets |
|---------|------|---------|
| `ui` | Static chat page, proxies `/api/` to `agent`. Only published service, localhost only. | none |
| `agent` | Agent loop behind HTTP, streams steps as Server-Sent Events. | API key |
| `agent-cli` | One-shot CLI (profile `cli`). | API key |
| `tools` | Side-effecting tools, read-only filesystem, no published ports. | none |

The web page has no login, so keep it bound to localhost: anyone who can reach it can spend your tokens.

## Tools

| Tool | Runs in | Purpose |
|------|---------|---------|
| `calculator` | `agent` / `agent-cli` (in-process) | Safe arithmetic (no `eval`) |
| `fetch_url` | `tools` service | Fetch a public web page; private addresses are blocked |
| `write_note` / `read_note` | `tools` service | Notes persisted in a volume |

Tools can live in two places: **in-process** (a plain function the loop calls) or in a **separate service** (called over HTTP). Side-effecting tools run in `tools`, which has no API key, so they stay isolated from the agent. The model sees one list of tools and cannot tell the difference.

**Add a tool**: write a function with type hints and a docstring, and add one entry to the list in `build_tools` in [agent/tools.py](agent/tools.py). LangChain builds the name, description and input schema from them. The loop does not change. Rebuild with `docker compose up --build -d` (web) or `docker compose run --rm --build agent-cli "..."` (CLI).

## Configuration

Set in `.env` (see [.env.example](.env.example)):

| Variable | Purpose | Default |
|----------|---------|---------|
| `ANTHROPIC_API_KEY` | Key for the default (Anthropic) model. Use your provider's key variable for another provider | |
| `MODEL` | Model name, or `<provider>:<model>` for another provider | `claude-haiku-4-5-20251001` |
| `MAX_ITERATIONS` | Loop cap | `10` |
| `UI_PORT` | Host port for the UI | `8080` |
| `AGENTO11Y_*`, `OTEL_EXPORTER_OTLP_*` | Grafana connection values. Only used once you instrument the app (see [Observability](#observability)); the shipped app ignores them | unset |

**Switching the model provider.** The loop contains no provider code: `get_model()` in [agent/main.py](agent/main.py) calls LangChain's `init_chat_model(MODEL)`. To try another provider, add its package to `agent/requirements.txt` (for example `langchain-openai`), set `MODEL=openai:<model>` and that provider's key in `.env`, then rebuild. Only Anthropic has been tested here.

## Observability

The app ships **uninstrumented**: it contains no tracing, metrics or telemetry code. Instrumenting it with Grafana Agent Observability is the exercise this repo is built for.

Follow [docs/agent-observability.md](docs/agent-observability.md). It covers the prerequisites, where to get the connection values in Grafana, and the ways to instrument the app: with a coding agent and gcx, with a coding agent without gcx, or by hand. The commented Grafana block in [.env.example](.env.example) is where those values go.

This variant runs on LangGraph, so the Agent Observability LangGraph adapter (`agento11y-langgraph`, with `capture_workflow_steps=True`) can record generations, tool calls and workflow steps through a single callback on `agent.stream(..., config=...)`. That is the natural way to instrument it here (not tried in this repo). The loop also reports every step through its `on_event` callback.

## Differences from `main`

| | `main` (hand-written loop) | `langchain-variant` |
|---|---|---|
| The loop | a `for` loop you can read: call model, run tools, append results | `create_agent` (a LangGraph graph); not visible in this repo |
| `agent/loop.py` | 110 lines of loop logic | 105 lines of configuration and event mapping |
| Iteration cap | a counter in the loop | `ModelCallLimitMiddleware` |
| Tool errors | returned to the model as a result | by default a tool exception **halts the run**; `ToolErrorMiddleware` is needed to return it to the model |
| Several tool calls in one turn | run one after another | run **concurrently** (a write and a read of the same note in one turn can race) |
| Tool definitions | `Tool(...)` with a hand-written JSON schema | a function with type hints and a docstring |
| Model client | `anthropic` SDK, Anthropic only | `init_chat_model(MODEL)`: provider-agnostic, no provider code in the loop |
| Events for the UI | emitted inside the loop | derived from `agent.stream(..., stream_mode="updates")`; tool calls and their results arrive in a slightly different order |
| Dependencies | `anthropic`, `httpx`, FastAPI | adds `langchain`, `langchain-anthropic`, `langchain-core`, `langgraph` |

The framework replaces the loop mechanics, not the work around them: the cap, error handling, history and streaming still had to be configured.

## Tests

```bash
docker compose run --rm --build test
```

Uses a scripted fake chat model: no API key, free, deterministic.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `Cannot connect to the Docker daemon` | Start Docker. |
| `agent run failed: ... no API key` | Create `.env` from `.env.example` and set your provider's key. |
| `could not create the model` | Unknown `MODEL`, or the provider's LangChain package isn't installed: add it to `agent/requirements.txt` and rebuild. |
| Authentication error | Check the key and that the account has credit. |
| `port is already allocated` | Set another `UI_PORT`. |

## Next steps

Instrument the app ([guide](docs/agent-observability.md)), persistent history, streaming model output, human approval for risky tools, evals, guardrails, multiple agents.
