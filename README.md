# Humble Guard Demo v2

A multi-user AI chat application with deep [Prompt Security](https://www.prompt.security) integration, built with FastAPI, PostgreSQL, and LiteLLM.

---

## Features

- **Multi-user chat** — streaming responses, session history, per-user daily message limits
- **Prompt Security integration** — two modes:
  - **API mode** — explicit prompt/response scanning with violation details shown on click
  - **Gateway mode** — route all LLM traffic through the PS proxy
- **LiteLLM proxy** — unified gateway to OpenAI, Anthropic, Google, and OpenRouter (free models included)
- **Per-user LLM API keys** — users can supply their own provider keys, stored encrypted at rest
- **App-issued API keys** — users can create scoped bearer keys for the public test endpoint
- **Public test API** — optional `POST /v1/responses` endpoint for SaaS scanners and external prompt testing
- **Admin dashboard** — overview stats, charts, PS tenant management, user management, activity log with config change events
- **Audit log** — all config changes (PS settings, LLM keys, user/tenant CRUD) appear in the activity log alongside chat messages

---

## Architecture

```
Browser
  │
  ├── GET /        → index.html   (chat UI)
  ├── GET /admin   → admin.html   (admin dashboard)
  └── API calls
        │
        ▼
   FastAPI app  (port 9000)
        │
        ├── PostgreSQL  (chat sessions, messages, users, PS tenants, audit events)
        │
        └── LiteLLM proxy  (port 4000)
                │
                ├── OpenRouter  (free: llama-3.1, mistral-7b, nemotron-9b)
                ├── OpenAI      (gpt-4o, gpt-4o-mini)
                ├── Anthropic   (claude-3-5-sonnet, claude-3-5-haiku)
                └── Google      (gemini-2.0-flash, gemini-1.5-pro)
```

---

## Quick Start

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) & Docker Compose

### 1. Clone and configure

```bash
git clone https://github.com/carlos-payes/humble-demo.git
cd humble-demo

cp .env.example .env
```

Edit `.env` and fill in the required values (see [Configuration](#configuration) below).

### 2. Start all services

```bash
docker compose up -d
```

> **Note:** On first run, LiteLLM applies ~110 database migrations. This takes 3–5 minutes. Subsequent starts are instant. Check progress with:
> ```bash
> docker logs -f demo-hgapp-v2-litellm-1
> ```

### 3. Open the app

| URL | Description |
|-----|-------------|
| http://localhost:9000 | Chat UI |
| http://localhost:9000/admin | Admin dashboard |
| http://localhost:4000 | LiteLLM proxy (direct) |

Default admin credentials (set in `.env`):
```
Email:    admin@example.com
Password: admin
```

---

## Configuration

Copy `.env.example` to `.env` and set the following:

```bash
# ── Database ──────────────────────────────────────────────────────────────────
POSTGRES_PASSWORD=your_secure_password
DATABASE_URL=postgresql+asyncpg://hgapp:your_secure_password@db:5432/hgapp

# ── Security ──────────────────────────────────────────────────────────────────
# Generate with: openssl rand -hex 32
SECRET_KEY=your_jwt_secret_here

# Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
ENCRYPTION_KEY=your_fernet_key_here

# ── Admin bootstrap ───────────────────────────────────────────────────────────
ADMIN_EMAIL=admin@example.com
ADMIN_PASSWORD=your_admin_password

# ── LiteLLM proxy ─────────────────────────────────────────────────────────────
LITELLM_BASE_URL=http://litellm:4000
# Generate with: python -c "import secrets; print(secrets.token_hex(32))"
LITELLM_MASTER_KEY=your_litellm_master_key

# ── Optional public test API ──────────────────────────────────────────────────
# Enables POST /v1/responses for app-issued bearer API keys
PUBLIC_API_ENABLED=false
PUBLIC_API_MAX_PROMPT_TOKENS=4000
PUBLIC_API_MAX_OUTPUT_TOKENS=600
PUBLIC_API_ALLOW_SYSTEM_PROMPT=false

# ── LLM provider keys ─────────────────────────────────────────────────────────
# At least one is required; OpenRouter covers free models with a single key.
OPENROUTER_API_KEY=sk-or-v1-...
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
GOOGLE_API_KEY=
```

### Generating secrets

```bash
# JWT secret
openssl rand -hex 32

# Fernet encryption key
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# LiteLLM master key
python3 -c "import secrets; print(secrets.token_hex(32))"
```

---

## LiteLLM

Models are configured in `litellm/config.yaml`. By default the following are enabled:

| Model | Provider | Notes |
|-------|----------|-------|
| `gpt-4o` / `gpt-4o-mini` | OpenAI | Requires `OPENAI_API_KEY` |
| `claude-3-5-sonnet-20241022` / `claude-3-5-haiku-20241022` | Anthropic | Requires `ANTHROPIC_API_KEY` |
| `gemini-2.0-flash` / `gemini-1.5-pro` | Google | Requires `GOOGLE_API_KEY` |
| `meta-llama/llama-3.1-8b-instruct:free` | OpenRouter | **Free** — requires `OPENROUTER_API_KEY` |
| `nvidia/nemotron-nano-9b-v2:free` | OpenRouter | **Free** — requires `OPENROUTER_API_KEY` |
| `mistralai/mistral-7b-instruct:free` | OpenRouter | **Free** — requires `OPENROUTER_API_KEY` |

Get a free OpenRouter key at [openrouter.ai](https://openrouter.ai).

---

## Prompt Security

### Setup

1. Log in as admin and go to **PS Tenants** in the admin dashboard.
2. Create a tenant with your PS `base_url` (API mode) and optionally a `gateway_url` (Gateway mode).
3. In ⚙ **Settings → Prompt Security**, select the tenant, enter your PS App ID, and choose API or Gateway mode.

### Modes

| Mode | How it works |
|------|-------------|
| **API mode** | The app calls the PS API explicitly before and after each LLM call. Violations are shown as clickable detail cards. |
| **Gateway mode** | All LLM traffic is routed through the PS proxy URL. No explicit scan calls — PS intercepts at the network layer. |

> **Important:** Each PS tenant has its own App ID. If you switch tenants, you must re-enter the App ID for the new tenant. The previous App ID is automatically cleared on tenant change.

---

## Public Test API

This app can optionally expose a narrow public endpoint for external scanners, SaaS tools, or simple prompt testing without exposing the full app surface.

### What it exposes

- `POST /v1/responses`
- Bearer auth using app-issued keys
- One prompt in, one text response out
- Existing user model restrictions and daily limits still apply
- Prompt Security still runs if configured for that user

### How to enable it

Set the following in `.env`:

```bash
PUBLIC_API_ENABLED=true
PUBLIC_API_MAX_PROMPT_TOKENS=4000
PUBLIC_API_MAX_OUTPUT_TOKENS=600
PUBLIC_API_ALLOW_SYSTEM_PROMPT=false
```

Then rebuild the app:

```bash
docker compose up -d --build app
```

### How to create an app API key

1. Open the chat UI.
2. Click the user menu in the top right.
3. Open **PS Settings**.
4. Go to **App API Keys**.
5. Create a key such as `saas-test`.
6. Copy the plaintext `hg_live_...` key immediately. It is shown only once.

### Example request

```bash
curl http://localhost:9000/v1/responses \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_APP_API_KEY" \
  -d '{
    "model": "meta-llama/llama-3.1-8b-instruct:free",
    "input": "Hello from the public test API"
  }'
```

### ngrok example

```bash
curl https://YOUR-NGROK-URL.ngrok-free.app/v1/responses \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_APP_API_KEY" \
  -d '{
    "model": "meta-llama/llama-3.1-8b-instruct:free",
    "input": "Hello from ngrok"
  }'
```

### Safety notes

- The endpoint is disabled by default.
- It does not expose admin routes.
- It is intended for temporary, low-risk external testing.
- Prefer using a dedicated low-privilege user, free models, and low daily limits.

---

## Admin Dashboard

Located at `/admin` (admin role required).

| Tab | Description |
|-----|-------------|
| **Overview** | Message volume chart, model distribution, PS action breakdown, top users |
| **Prompt Security** | PS mode stats, per-mode toggle cards |
| **Users** | User list with per-user stats, inline edit, user detail view with charts |
| **PS Tenants** | Create / edit / delete PS tenants |
| **Activity Log** | Combined view of all chat messages and config change events (PS config, LLM keys, user/tenant CRUD) |

---

## API Reference

### Auth
| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/auth/login` | Get JWT token |
| `GET` | `/auth/me` | Current user info |

### Chat
| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/chat/stream` | SSE streaming chat endpoint |
| `GET` | `/models` | Available models |
| `GET` | `/sessions` | User's chat sessions |
| `GET` | `/sessions/{id}/messages` | Messages in a session |

### User Settings
| Method | Path | Description |
|--------|------|-------------|
| `PATCH` | `/users/me/ps-config` | Update PS tenant, App ID, mode |
| `PATCH` | `/users/me/llm-keys` | Update per-user LLM API keys |
| `GET` | `/users/me/api-keys` | List app-issued API keys |
| `POST` | `/users/me/api-keys` | Create an app-issued API key |
| `DELETE` | `/users/me/api-keys/{id}` | Delete an app-issued API key |
| `GET` | `/users/me/stats` | Personal usage stats |

### Public Test API
| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v1/responses` | Narrow public prompt endpoint authenticated by app API key |

### Admin
| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/admin/stats` | Aggregate stats for dashboard |
| `GET/POST/PATCH/DELETE` | `/admin/users/*` | User management |
| `GET/POST/PATCH/DELETE` | `/admin/ps-tenants/*` | PS tenant management |
| `GET` | `/admin/activity` | Combined chat + audit event log |
| `GET` | `/admin/users/{id}/stats` | Per-user detailed stats |

---

## Development

To run locally without Docker:

```bash
# Start Postgres
docker compose up -d db

# Install dependencies
pip install -r requirements.txt

# Set env vars (or use a local .env)
export DATABASE_URL=postgresql+asyncpg://hgapp:change_me@localhost:5432/hgapp
export SECRET_KEY=dev_secret
export ENCRYPTION_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

# Run
cd app
uvicorn main:app --reload --port 8000
```

---

## Project Structure

```
.
├── app/
│   ├── main.py             # FastAPI app, all routes
│   ├── models.py           # SQLAlchemy ORM models
│   ├── schemas.py          # Pydantic request/response schemas
│   ├── auth.py             # JWT auth, API key auth, password hashing
│   ├── crypto.py           # Fernet encryption for stored API keys
│   ├── database.py         # Async SQLAlchemy engine + session
│   ├── prompt_security.py  # PS API client (protect_prompt / protect_response)
│   ├── token_counter.py    # Token estimation helpers via LiteLLM
│   └── static/
│       ├── index.html      # Chat UI
│       ├── admin.html      # Admin dashboard
│       └── login.html      # Login page
├── litellm/
│   └── config.yaml         # LiteLLM model list and settings
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── .env.example
```

---

## License

MIT
