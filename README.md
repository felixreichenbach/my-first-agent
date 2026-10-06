# my-first-agent

A hands-on tutorial for building an agentic application: an LLM that calls tools in a loop until a task is done. The loop is hand-written against the Anthropic SDK (no framework), runs in containers, and can be watched in a terminal or a web chat UI.

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

The loop is in [agent/loop.py](agent/loop.py) (~110 lines).

## Quick start

Requires Docker with Compose v2 and an [Anthropic API key](https://console.anthropic.com/).

```bash
cp .env.example .env     # set ANTHROPIC_API_KEY
```

**CLI** (trace on stderr, answer on stdout):

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
 │    │ ANTHROPIC_API_KEY           │   │ ANTHROPIC_API_KEY            │          │
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
                                      ▼                                Claude API
                           public web (fetch_url)                   api.anthropic.com
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

Tools can live in two places: **in-process** (a plain function the loop calls) or in a **separate service** (called over HTTP). Side-effecting tools run in `tools`, which has no API key, so they stay isolated from the agent. The model sees one registry and cannot tell the difference.

**Add a tool**: write a function and add one `Tool(...)` entry in `build_registry` in [agent/tools.py](agent/tools.py). The loop does not change. Rebuild with `docker compose up --build -d` (web) or `docker compose run --rm --build agent-cli "..."` (CLI).

## Configuration

Set in `.env` (see [.env.example](.env.example)):

| Variable | Purpose | Default |
|----------|---------|---------|
| `ANTHROPIC_API_KEY` | Required | |
| `MODEL` | Model ID | `claude-haiku-4-5-20251001` |
| `MAX_ITERATIONS` | Loop cap | `10` |
| `UI_PORT` | Host port for the UI | `8080` |

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

## Next steps

Instrument with OpenTelemetry/Grafana SDKs, persistent history, streaming model output, human approval for risky tools, evals, guardrails, multiple agents.
