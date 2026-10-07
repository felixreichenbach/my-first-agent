# Agent Observability

## Prerequisites

### Tooling

- Claude Code, VS Code with LLM plugin or similar

### LLM Access (e.g. Anthropic)
1. Request Anthropic Developer role via Grafana License Manager
2. Navigate to `https://platform.claude.com/settings/keys`
3. Create the API key and put it into `.env`

## Coding Agent Instrumentation

Instrument your coding environment such as Claude Code or VS Code with LLM plugins etc. to track cost, quality and efficiency of your prompts.

TO BE CLARIFIED:
- Can I use evals etc. with coding agents as well?

1. Navigate to `Observability` -> `Agent` -> `Agents` -> `Add more agents`
2. Choose `Coding agents you use`

## Agent Application Instrumentation

Instrumenting an application which provides agentic workflows.

1. Navigate to `Observability` -> `Agent` -> `Agents` -> `Add more agents`
2. Choose `Agents you build`
   - Option A: Use Coding Agent with GCX
   - Option B: Use any Coding Agent without GCX
   - Option C: Manual instrumentation
3. Option A:
   - Copy & paste the API keys and OTEL endpoints into `.env`
   - Execute the prompt provided through the instrumentation wizard
   - Run the stack `docker compose up --build --remove-orphans` unless the coding agent did it already
   - Verify collected telemetry in Grafana under `Observability`-> `Agent` -> `Conversations`
4. Option B:
   - To be continued...
5. Option C:
   - To be continued...
