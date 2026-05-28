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
- **There is no `.env` file and there never will be.** All configuration goes in the `environment:` sections of `docker-compose.yml`, or through the Admin → Settings panel at runtime.
- Default admin credentials and all secrets (encryption key, API keys, etc.) are set directly in `docker-compose.yml`.
- Repo-level LiteLLM model routing lives in `litellm/config.yaml`.
- Provider API keys (OpenAI, Anthropic, Google, Perplexity, OpenRouter) can be set as env vars in `docker-compose.yml` or saved at runtime via **Admin → Settings → LLM API Keys** — runtime-saved keys take precedence and trigger automatic model discovery.

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
2. Run `docker compose up -d --build`.
3. Wait for LiteLLM to finish first-run migrations if needed (~3–5 min on first run).
4. Refresh the app model cache with `POST /admin/refresh-models`.
5. Verify with `docker compose ps`, `GET /health`, `GET /`, `GET /admin`, and authenticated `GET /models`.

### Stop

Run `docker compose stop`, then verify `docker compose ps` shows no project containers running.

### Configure Inference

There are two ways to add models:

**1. LiteLLM config (static)** — add a model entry to `litellm/config.yaml` and set any required API key in the `environment:` section of `docker-compose.yml`. Restart `litellm` and `app`, then refresh the model cache.

**2. Direct provider routing (dynamic)** — save an API key for OpenAI, Anthropic, Google, Perplexity, or OpenRouter in **Admin → Settings → LLM API Keys**. The app queries the provider's `/models` endpoint and adds all available models to the picker automatically as `provider/model-id` (e.g. `openai/gpt-4.1`). These bypass LiteLLM entirely.

For custom OpenAI-compatible endpoints, add the base URL and model ID via `litellm/config.yaml`. Never hardcode API keys in repo files — use `docker-compose.yml` environment sections or the admin Settings panel.

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
  `curl -sS http://localhost:4000/v1/models -H "Authorization: Bearer $(docker compose exec app printenv LITELLM_MASTER_KEY)"`

## Verification Rule

Before saying the app is running, stopped, fixed, or configured, run fresh verification in the current turn and report the actual output state. Use `superpowers:verification-before-completion` when making completion claims.
