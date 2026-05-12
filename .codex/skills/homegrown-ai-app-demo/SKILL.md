---
name: homegrown-ai-app-demo
description: Use when working on, running, stopping, verifying, debugging, or configuring the HomeGrown AI App Demo project, including its Docker Compose stack, FastAPI app, PostgreSQL database, LiteLLM proxy, Prompt Security settings, model cache, provider keys, or custom inference endpoints.
---

# HomeGrown AI App Demo

## Overview

Use this skill for project-specific app operations in the HomeGrown AI App Demo repository. It captures the known Docker Compose workflow, verification checks, and inference configuration points for this repo.

## Project Facts

- App directory: the repository root containing `docker-compose.yml`, `app/`, and `litellm/`
- Main services: `app`, `db`, `litellm`
- URLs:
  - Chat UI: `http://localhost:9000`
  - Admin UI: `http://localhost:9000/admin`
  - LiteLLM proxy: `http://localhost:4000`
- If host port `4000` is occupied, set `LITELLM_HOST_PORT=4001` in `.env`; keep `LITELLM_BASE_URL=http://litellm:4000` for app-to-proxy traffic inside Docker.
- Default admin bootstrap is read from `.env`; current defaults are `admin@example.com` / `admin`.
- Local secrets and provider keys belong in `.env`, which is gitignored.
- Repo-level LiteLLM model routing lives in `litellm/config.yaml`.

## Helper Script

Prefer the bundled script for routine operations:

```bash
bash .codex/skills/homegrown-ai-app-demo/scripts/manage.sh status
bash .codex/skills/homegrown-ai-app-demo/scripts/manage.sh start
bash .codex/skills/homegrown-ai-app-demo/scripts/manage.sh stop
bash .codex/skills/homegrown-ai-app-demo/scripts/manage.sh verify
bash .codex/skills/homegrown-ai-app-demo/scripts/manage.sh refresh-models
```

Run the helper from anywhere inside the repo. If the repo cannot be discovered from the current directory or script location, set `HOMEGROWN_AI_APP_DIR=/path/to/homegrown-ai-app-demo`.

## Common Workflows

### Start or Restart

1. Check for dirty work before edits: `git status --short`.
2. Ensure `.env` exists. If missing, create it from `.env.example` with generated development secrets; do not commit `.env`.
3. Run `docker compose up -d --build`.
4. Wait for LiteLLM to finish first-run migrations if needed.
5. Refresh the app model cache with `POST /admin/refresh-models`.
6. Verify with `docker compose ps`, `GET /health`, `GET /`, `GET /admin`, and authenticated `GET /models`.

### Stop

Run `docker compose stop`, then verify `docker compose ps` shows no project containers running.

### Configure Inference

Ask for the missing details before wiring a custom endpoint:

- Base URL
- Auth method and API key/header
- Model ID exposed by the endpoint
- Whether the endpoint is OpenAI-compatible

For OpenAI-compatible endpoints, prefer adding environment variables to `.env` and a model entry to `litellm/config.yaml`; never hardcode API keys in repo files. After changing LiteLLM config, restart `litellm` and `app`, refresh models, then test a minimal chat request if a key is available.

For the project’s local OpenAI-compatible test server, use:

- Host URL: `http://localhost:8081/v1`
- Docker URL: `http://host.docker.internal:8081/v1`
- Current model ID: `huggingface/Qwen3VL-8B-Instruct-F16`
- Env vars: `LOCAL_OPENAI_BASE_URL`, `LOCAL_OPENAI_API_KEY`, `LOCAL_OPENAI_MODEL_IDS`

### Debug

- App logs: `docker compose logs --no-color --tail=120 app`
- LiteLLM logs: `docker compose logs --no-color --tail=160 litellm`
- DB logs: `docker compose logs --no-color --tail=120 db`
- Health: `curl -sS http://localhost:9000/health | python3 -m json.tool`
- LiteLLM models:
  `curl -sS http://localhost:4000/v1/models -H "Authorization: Bearer $(grep '^LITELLM_MASTER_KEY=' .env | cut -d= -f2-)"`

## Verification Rule

Before saying the app is running, stopped, fixed, or configured, run fresh verification in the current turn and report the actual output state. Use `superpowers:verification-before-completion` when making completion claims.
