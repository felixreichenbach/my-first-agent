# my-first-agent

A hands-on tutorial for technical readers who want to understand how agentic applications work. It builds the smallest useful agent: an LLM that calls tools in a loop until a task is done.

- **Explicit agent loop**: call the model, run the tools it requests, feed the results back, repeat. It is hand-written against the Anthropic SDK with no framework, so every step is visible ([agent/loop.py](agent/loop.py)).
- **Containerized**: the agent, its tool service and a small web chat UI run with Docker Compose. You only need Docker and an API key.
- **Observable**: the agent emits OpenTelemetry traces to a Grafana of your choice, configured through `.env`. Grafana is not part of the Compose stack.

Requirements and design decisions: [REQUIREMENTS.md](REQUIREMENTS.md).

## 1. Concepts

An agent is a loop around a model that can ask for actions:

```
  task ──► messages + tool definitions ──► model ──► response
                    ▲                                    │
                    │                          tool_use blocks?
                    │                           yes │        │ no
                    └── append tool results ◄── run tools     └──► final answer
```

- **Messages**: the API is stateless. Every call resends the whole history: your task, the model's replies, and the tool results.
- **Tool definition**: a name, a description and a JSON schema. The model reads these to decide what to call.
- **Tool use / tool result**: the model never runs anything. It emits a `tool_use` block; *your code* runs the function and replies with a `tool_result`.
- **Stop condition**: when a response has no `tool_use` blocks the model is done. An iteration cap protects you from runaway loops and cost.

## 2. Run it

Prerequisites: Docker with Compose v2 and an [Anthropic API key](https://console.anthropic.com/).

```bash
cp .env.example .env          # then set ANTHROPIC_API_KEY
docker compose run --rm agent "What is 17 * 23, and what is the title of https://example.com?"
```

Try tasks that need different amounts of work:

| Task | Tool calls |
|------|-----------|
| `"Name three primary colors."` | none |
| `"What is 2 to the power of 20?"` | `calculator` |
| `"Summarize https://example.com and save the summary as a note called example"` | `fetch_url`, `write_note` |
| `"What did I save in the note called example?"` (new run) | `read_note` (notes persist in a volume) |

The step-by-step trace goes to stderr, the final answer to stdout. Run without an argument for an interactive prompt.

### Web chat UI

```bash
docker compose up --build -d
```

Then open http://localhost:8080 in your browser. (If the port is taken, set `UI_PORT` in `.env`.)

Each loop step (model text, tool call, tool result) appears live as a collapsible card above the answer. The conversation is multi-turn: the browser keeps the history and sends it with every request, which shows that an agent's "memory" is just the messages list. Only plain text is carried between turns; earlier tool calls are not replayed.

```
browser ──► ui (nginx, no secrets) ──► api (agent loop, holds the keys) ──► tools
 :8080          published on 127.0.0.1         internal network only
```

The UI container is the only one published to your machine, and it has no access to `ANTHROPIC_API_KEY` or the telemetry credentials; those live only in `api`. It is bound to `127.0.0.1` because the page has no login: anyone who can reach it can spend your tokens. Change the port with `UI_PORT` in `.env`.

Stop and clean up with `docker compose down` (add `-v` to also delete saved notes).

### Layout

```
agent/            the agent image: loop.py (the loop), tools.py, telemetry.py,
                  main.py (CLI), api.py (HTTP/SSE service used by the web UI)
ui/               web chat UI: static page + nginx proxy, no secrets
tools_service/    separate container for side-effecting tools (fetch_url, notes)
tests/            unit tests (fake model client, no API key needed)
docker-compose.yml, .env.example
```

Secrets stay in one place: only the agent (`agent` CLI / `api`) gets `ANTHROPIC_API_KEY` and the OTLP credentials. The `tools` service has no secrets, a read-only filesystem and no published ports, so a misbehaving tool cannot reach your API key; the `ui` container is the one exposed to the host, so it holds none either.

## 3. Read the loop

Open [agent/loop.py](agent/loop.py). The loop does no I/O of its own: it reports progress through an `on_event` callback, which the CLI prints and the web API streams. The numbered comments in `run_agent` map to the diagram above:

1. **Ask the model**: `client.messages.create(...)` with the full history and the tool definitions.
2. **Check for tool requests**: no `tool_use` blocks means return the answer.
3. **Run tools, append results**: all results go back in a single `user` message, matched to requests by `tool_use_id`.

Details worth noticing: tool errors become `is_error` results instead of exceptions so the model can recover; tool output is truncated; the loop is capped by `MAX_ITERATIONS`.

## 4. Add a tool

Exercise: give the agent a clock. In [agent/tools.py](agent/tools.py) add a function and a registry entry. The loop needs no changes.

```python
from datetime import datetime, timezone

def current_time() -> str:
    return datetime.now(timezone.utc).isoformat()

# inside build_registry's list:
Tool("current_time", "Get the current UTC date and time.",
     {"type": "object", "properties": {}}, current_time),
```

Rebuild and ask: `docker compose run --rm --build agent "What time is it in UTC?"`. For the web UI, rebuild with `docker compose up --build -d`.

## 5. Break it on purpose

| Experiment | What to observe |
|------------|-----------------|
| `docker compose run --rm -e MAX_ITERATIONS=1 agent "What is 3*4?"` | The cap triggers, the agent exits with code 2 and a clear message. |
| Ask it to fetch `http://localhost:8000/health` or `http://169.254.169.254/` | The tools service refuses (non-public address); the model sees the error and explains. |
| Ask for `"read the note called ../etc/passwd"` | Path traversal is rejected; the model gets the error text. |
| Ask for `"calculate __import__('os').getcwd()"` | The calculator accepts only arithmetic. |
| Set a wrong `ANTHROPIC_API_KEY` | The run fails with a clear API error. |

## 6. Observe it

Telemetry is driven by standard OpenTelemetry variables in `.env`; nothing is Grafana-specific. See [.env.example](.env.example) for Grafana Cloud, a local Collector/Alloy on your host (`host.docker.internal`, since `localhost` inside a container is the container), and a console-only mode (`OTEL_TRACES_EXPORTER=console`) that needs no backend.

Each run produces one trace:

```
agent.run
├─ agent.iteration  (agent.iteration=1)
│  ├─ model.call    (stop_reason=tool_use, token counts)
│  └─ tool.call     (agent.tool=calculator)
└─ agent.iteration  (agent.iteration=2)
   └─ model.call    (stop_reason=end_turn)
```

In Grafana, open Explore with your traces datasource, search for `service.name = my-first-agent`, and match each iteration span to a line in the terminal trace. Prompts and tool output are not recorded unless you set `OTEL_CAPTURE_CONTENT=true`.

Telemetry is set up in both the CLI and the `api` service, so web chats are traced too.

## 7. Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| `Cannot connect to the Docker daemon` | Docker is not running; start Docker Desktop (or the daemon). |
| `error: ANTHROPIC_API_KEY is not set` | Create `.env` from `.env.example` and set the key. |
| Run fails with an authentication error | The key is wrong, revoked, or has no credit. |
| Web UI shows `Error: ...` in the chat | Read the message; check `docker compose logs api`. |
| `port is already allocated` | Set a different `UI_PORT` in `.env`. |
| No traces in Grafana | Check the endpoint and `OTEL_EXPORTER_OTLP_HEADERS` in `.env`; use `OTEL_TRACES_EXPORTER=console` to confirm spans are produced. |

The default model is a small, inexpensive one (`MODEL` in `.env`); a typical task here uses a few thousand tokens, and each run prints its token counts.

## 8. Tests

```bash
docker compose run --rm --build test
```

Tests replay scripted model responses, so they are free and deterministic. They cover a plain answer, single and multi-step tool use, tool errors, unknown tools, the iteration cap, API failure, chat history, the SSE stream of the web API, the calculator and the tool service's safety checks.

## 9. Where next

Persist conversation history across runs, stream responses, require human approval for risky tools, add evals and guardrails, split into multiple cooperating agents.
