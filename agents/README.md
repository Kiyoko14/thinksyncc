# Agents

This directory is reserved for future AI agent implementations.

## Planned

- **deploy-agent** — watches a Git branch and triggers deploys on push
- **monitor-agent** — polls server health and sends alerts
- **pipeline-agent** — orchestrates multi-step CI/CD workflows
- **diagnostic-agent** — analyses logs and suggests fixes using an LLM

Agents will be implemented as standalone Python services that communicate
with the backend API and optionally publish events via Redis Pub/Sub.
