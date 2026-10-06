# my-first-agent

A hands-on tutorial for technical readers who want to understand how agentic applications work. It builds the smallest useful agent: an LLM that calls tools in a loop until a task is done.

- **Explicit agent loop**: call the model, run the tools it requests, feed the results back, repeat. It is hand-written against the Anthropic SDK with no framework, so every step is visible.
- **Containerized**: the agent and its tool service run with Docker Compose. You only need Docker and an API key.
- **Observable**: the agent emits OpenTelemetry traces to a Grafana of your choice, configured through `.env`. Grafana is not part of the Compose stack.

## Status

Planning stage. The full requirements are in [REQUIREMENTS.md](REQUIREMENTS.md). The code and the step-by-step tutorial are still to be written.

## Planned quick start

```bash
cp .env.example .env        # add your ANTHROPIC_API_KEY (and optional Grafana OTLP settings)
docker compose run --rm agent "What is 17 * 23, and what is the title of https://example.com?"
```

## What you will learn

1. What an agent is and how the loop works
2. How tool definitions, tool calls and tool results fit together
3. How to add a tool and handle errors and runaway loops
4. How to trace each loop iteration in Grafana
