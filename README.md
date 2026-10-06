# my-first-agent

A hands-on tutorial for building an agentic application: an LLM that calls tools in a loop until a task is done. The loop is hand-written against the Anthropic SDK (no framework), runs in containers, and can be watched in a terminal, a web chat UI, and Grafana.

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

The loop is in [agent/loop.py](agent/loop.py) (~130 lines).

## Quick start

Requires Docker with Compose v2 and an [Anthropic API key](https://console.anthropic.com/).

```bash
cp .env.example .env     # set ANTHROPIC_API_KEY
```

**CLI** (trace on stderr, answer on stdout):

```bash
docker compose run --rm agent "What is 17 * 23, and what is the title of https://example.com?"
```

**Web chat UI** at http://localhost:8080 (change with `UI_PORT`):

```bash
docker compose up --build -d
docker compose down          # stop; add -v to delete saved notes
```

## Architecture

```
browser ──► ui (nginx, 127.0.0.1:8080) ──► api (agent loop) ──► tools (fetch_url, notes)
terminal ─► agent (CLI) ───────────────────┘                      └─► workspace volume
```

| Service | Role | Secrets |
|---------|------|---------|
| `ui` | Static chat page, proxies `/api/` to `api`. Only published service, localhost only. | none |
| `api` | Agent loop behind HTTP, streams steps as Server-Sent Events. | API key, OTLP credentials |
| `agent` | One-shot CLI (profile `cli`). | API key, OTLP credentials |
| `tools` | Side-effecting tools, read-only filesystem, no published ports. | none |

The web page has no login, so keep it bound to localhost: anyone who can reach it can spend your tokens.

## Tools

| Tool | Runs in | Purpose |
|------|---------|---------|
| `calculator` | agent | Safe arithmetic (no `eval`) |
| `fetch_url` | tools | Fetch a public web page; private addresses are blocked |
| `write_note` / `read_note` | tools | Notes persisted in a volume |

**Add a tool**: write a function and add one `Tool(...)` entry in `build_registry` in [agent/tools.py](agent/tools.py). The loop does not change. Rebuild with `docker compose up --build -d` (web) or `docker compose run --rm --build agent "..."` (CLI).

## Configuration

Set in `.env` (see [.env.example](.env.example)):

| Variable | Purpose | Default |
|----------|---------|---------|
| `ANTHROPIC_API_KEY` | Required | |
| `MODEL` | Model ID | `claude-haiku-4-5-20251001` |
| `MAX_ITERATIONS` | Loop cap | `10` |
| `UI_PORT` | Host port for the UI | `8080` |
| `OTEL_*` | Telemetry, see below | disabled |

## Observability

Telemetry uses standard OpenTelemetry variables only, for both CLI and web. Grafana is not part of the stack; point the agent at Grafana Cloud or a Collector/Alloy, or set `OTEL_TRACES_EXPORTER=console`. From a container, use `host.docker.internal` to reach a service on your host. Examples are in [.env.example](.env.example).

Each prompt produces one trace. Each line is a span (a timed step); indentation shows nesting. For the `3 * 7 = ?` example from above:

```
agent.run                         the whole prompt, start to finish
├─ agent.iteration  (1)           first pass through the loop
│  ├─ model.call                  2. model decides: stop_reason=tool_use (calculator)
│  └─ tool.call                   3. calculator runs: 3 * 7 → 21
└─ agent.iteration  (2)           second pass, tool result is in the conversation
   └─ model.call                  model decides: stop_reason=end_turn, 4. reply "3 * 7 = 21"
```

In Grafana you can see how many iterations a prompt took, which tools ran, how long each step took, and token counts per model call. A prompt that needs no tool has a single iteration with one `model.call`.

Prompts and tool output are recorded only if `OTEL_CAPTURE_CONTENT=true`.

## Tests

```bash
docker compose run --rm --build test
```

Uses a scripted fake model client: no API key, free, deterministic.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `Cannot connect to the Docker daemon` | Start Docker. |
| `ANTHROPIC_API_KEY is not set` | Create `.env` from `.env.example`. |
| Authentication error | Check the key and that the account has credit. |
| `port is already allocated` | Set another `UI_PORT`. |
| No traces | Check `OTEL_EXPORTER_OTLP_*`; try `OTEL_TRACES_EXPORTER=console`. |

## Next steps

Persistent history, streaming model output, human approval for risky tools, evals, guardrails, multiple agents.
