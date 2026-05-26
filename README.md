# HomeGrown App Demo

A multi-user AI chat application with deep [Prompt Security](https://www.prompt.security) integration, built with FastAPI, PostgreSQL, and LiteLLM.

---

## Features

### Core Chat
- **Multi-user streaming chat** — real-time SSE responses with full session history
- **Per-user daily message limits** — configurable caps to control usage
- **Multiple LLM providers** — OpenAI, Anthropic, Google, Perplexity, and OpenRouter (including free models) via LiteLLM

### Prompt Security Integration
- **API mode** — explicit prompt and response scanning before and after each LLM call; violations shown as clickable detail cards with full PS response JSON
- **Gateway mode** — all LLM traffic routed through the PS proxy URL; no explicit scan calls, PS intercepts at the network layer
- **PS API inspector** — collapsible panel beneath each violation card shows raw PS request/response JSON, syntax-highlighted
- **File sanitization** — dedicated File Scan tab in the Demo panel; drag-and-drop a PDF, DOCX, XLSX, or TXT file through the PS `/api/sanitizeFile` endpoint

### Demo & Education
- **Interactive walkthrough** — step-by-step tour showing the exact Python code running at each stage: user input → PS prompt scan → LLM call → PS response scan → display
- **Side-by-side compare mode** — splits the chat into two live columns: left with PS active, right with raw LLM output, so the impact of PS is immediately visible
- **Pre-built demo scenarios** — ready-to-load prompts for PII detection, topic policy, token DoS, and prompt injection, each with Load and Compare buttons
- **API Flow diagram** — custom diagram showing the full request path (User → App + PS Engine → LLM Providers) with bidirectional arrows

### Admin & Audit
- **Admin dashboard** — message volume charts, model distribution, PS action breakdown, top users, per-user detail views
- **User management** — create, edit, and delete users; set per-user daily message limits and model restrictions
- **PS tenant management** — create and manage multiple Prompt Security tenants with separate App IDs and URLs
- **Audit log** — all config changes (PS settings, user/tenant CRUD) appear in the activity log alongside chat messages

### Optional: Public Test API
- **Narrow public endpoint** — `POST /v1/responses` for external scanners or SaaS tools, authenticated by app-issued bearer keys
- **App-issued API keys** — users can create scoped `hg_live_...` keys from the settings menu; shown once at creation
- **Safety by default** — disabled unless explicitly enabled; does not expose admin routes

---

## Quick Start

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and Docker Compose

### 1. Clone the repo

```bash
git clone https://github.com/prompt-security/homegrown-ai-app-demo.git
cd homegrown-ai-app-demo
```

### 2. Build and Start all services

```bash
docker compose up -d --build
```

> **Note:** On first run, LiteLLM applies ~110 database migrations. This takes 3–5 minutes. Subsequent starts are instant. Check progress with:
>
> ```bash
> docker compose logs -f
> ```

### 3. Complete the Setup Wizard

Open [http://localhost:9100](http://localhost:9100). On first run you'll be prompted through the Setup Wizard, which:

- Generates and stores the encryption key and JWT secret
- Sets the initial admin email and password
- Configures app-level settings

Once complete, the app is fully operational.

### 4. Open Mode vs User Mode

The app supports two ways to use the chat interface:

| | Open Mode (Guest) | User Mode |
| --- | --- | --- |
| **Login required** | No | Yes |
| **Who uses it** | Walk-up visitors, demo audiences | Named accounts created by admin |
| **Identity** | Optional name + email (or anonymous by IP) | Email + password |
| **PS config** | Supplied per-request in the UI | Saved in user profile |
| **Session history** | Stored by guest ID for that session | Persistent across sessions |
| **Activity logging** | Logged in admin as guest events | Logged in admin under user account |
| **Daily limits** | Not enforced | Enforced per-user if set |

**Open Mode** is ideal for demos and live events, or a hosted instance of this application — visitors can start chatting immediately without creating an account. Prompt Security can still be configured and tested in real time.

**User Mode** is for recurring users who need persistent history, saved PS settings, and usage tracking. Admin creates accounts from the dashboard. This is ideal for local installations that isn't hosted.

Both modes can be active simultaneously — the chat UI shows a identification option while still allowing guest access.

### 5. Available URLs

| URL | Description |
| --- | ----------- |
| [http://localhost:9100](http://localhost:9100) | Chat UI |
| [http://localhost:9100/admin](http://localhost:9100/admin) | Admin dashboard |
| [http://localhost:4000](http://localhost:4000) | LiteLLM proxy (direct) |

---

## LiteLLM Models

Models are configured in `litellm/config.yaml`. The following are enabled by default:

| Model | Provider | Notes |
| ----- | -------- | ----- |
| `gpt-4o` / `gpt-4o-mini` / `gpt-5-nano` | OpenAI | Requires `OPENAI_API_KEY` |
| `claude-sonnet-4-5-20250929` | Anthropic | Requires `ANTHROPIC_API_KEY` |
| `gemini-2.0-flash` / `gemini-1.5-pro` | Google via OpenRouter | Requires `OPENROUTER_API_KEY` |
| `sonar` | Perplexity | Requires `PERPLEXITY_API_KEY` |
| `meta-llama/llama-3.3-70b-instruct:free` | OpenRouter | **Free** |
| `meta-llama/llama-3.1-8b-instruct:free` | OpenRouter | **Free** |
| `deepseek/deepseek-r1:free` | OpenRouter | **Free** |
| `qwen/qwen-2.5-72b-instruct:free` | OpenRouter | **Free** |
| `mistralai/mistral-7b-instruct:free` | OpenRouter | **Free** |
| `nvidia/nemotron-nano-9b-v2:free` | OpenRouter | **Free** |
| + more | OpenRouter | **Free** |
| `gemma3:270m` | Ollama (local) | See [Ollama](#ollama-local-models) |
| `huggingface/Qwen3VL-8B-Instruct-F16` | Local OpenAI-compatible | See [Local endpoint](#local-openai-compatible-endpoint) |

To add or remove models, edit `litellm/config.yaml` and restart the `litellm` service:

```bash
docker compose restart litellm
```

---

## Prompt Security

### Setup

1. Log in as admin and go to **PS Tenants** in the admin dashboard.
2. Create a tenant with your PS `base_url` (API mode) and optionally a `gateway_url` (Gateway mode). Both URLs must be public HTTPS hostnames — localhost, `.local`, private IPs, and reserved networks are rejected.
3. In ⚙ **Settings → Prompt Security**, select the tenant, enter your PS App ID, and choose API or Gateway mode.

### Modes

| Mode | How it works |
| ---- | ------------ |
| **API mode** | The app calls the PS API explicitly before and after each LLM call. Violations are shown as clickable detail cards. |
| **Gateway mode** | All LLM traffic is routed through the PS proxy URL. No explicit scan calls — PS intercepts at the network layer. |

> **Important:** Each PS tenant has its own App ID. If you switch tenants, you must re-enter the App ID for the new tenant. The previous App ID is automatically cleared on tenant change.

---

## Ollama (local models)

Ollama lets you run open-weight models locally — no cloud API key needed.

### Prerequisites

**Hardware:**
- **Apple Silicon (M1/M2/M3/M4):** Works out of the box via Metal. 8 GB RAM minimum; 16 GB+ recommended for 7B+ models.
- **NVIDIA GPU:** Requires [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html) installed on the host. The Ollama container will use the GPU automatically.
- **CPU only:** Works but is slow. Stick to small models (1B–3B).

**Model size guide:**

| Model size | Min RAM/VRAM |
| ---------- | ------------ |
| 1B–3B | 4 GB |
| 7B–8B | 8 GB |
| 13B | 16 GB |
| 70B | 48 GB |

### Option A — Via Docker Compose (recommended)

Ollama is included in `docker-compose.yml` as an **optional service** using a Docker Compose profile. It does not start with the rest of the stack unless you explicitly enable it.

**Start Ollama:**
```bash
docker compose --profile ollama up -d ollama
```

**Pull a model into the container:**
```bash
docker exec homegrown-ai-app-demo-ollama-1 ollama pull gemma3:270m
```

**Tell LiteLLM where Ollama is** — add to the `litellm` service `environment` block in `docker-compose.yml`:
```yaml
OLLAMA_BASE_URL: "http://ollama:11434"
```
Then restart LiteLLM: `docker compose restart litellm`

**Stop Ollama when not needed:**
```bash
docker compose --profile ollama stop ollama
```

Models are stored in the `ollama_data` Docker volume and persist across container restarts.

### Option B — Ollama on the host machine

Install [Ollama](https://ollama.com) directly on the host and pull models with `ollama pull <model>`. Docker containers reach the host at `host.docker.internal`:

```yaml
OLLAMA_BASE_URL: "http://host.docker.internal:11434"
```

### Option C — Remote Ollama server

Point to any Ollama instance reachable over the network:

```yaml
OLLAMA_BASE_URL: "http://<remote-host>:11434"
```

### Adding more Ollama models

1. Pull the model: `ollama pull <model>` (or via `docker exec` if using Option A)
2. Add an entry to `litellm/config.yaml`:
   ```yaml
   - model_name: <model>
     litellm_params:
       model: ollama/<model>
       api_base: os.environ/OLLAMA_BASE_URL
   ```
3. In Admin → App Settings, use **Test Connection** to auto-detect and register the new model.
4. Restart LiteLLM: `docker compose restart litellm`

---

## Local OpenAI-compatible endpoint

For local servers that expose OpenAI-style `/v1/chat/completions` endpoints, add to the `litellm` service `environment` block in `docker-compose.yml`:

```yaml
LOCAL_OPENAI_BASE_URL: "http://host.docker.internal:8081/v1"
LOCAL_OPENAI_API_KEY: "local-dev-key"
```

The default `litellm/config.yaml` includes the `huggingface/Qwen3VL-8B-Instruct-F16` model pointing to this endpoint.

---

## Public Test API

This app can optionally expose a narrow public endpoint for external scanners, SaaS tools, or simple prompt testing.

### How to enable it

Add to the `app` service `environment` block in `docker-compose.yml`:

```yaml
app:
  environment:
    PUBLIC_API_ENABLED: "true"
    PUBLIC_API_MAX_PROMPT_TOKENS: "4000"
    PUBLIC_API_MAX_OUTPUT_TOKENS: "600"
    PUBLIC_API_ALLOW_SYSTEM_PROMPT: "false"
```

Then rebuild:

```bash
docker compose up -d --build app
```

### How to create an API key

1. Open the chat UI and click the user menu (top right).
2. Open **Settings → App API Keys**.
3. Create a key (e.g. `saas-test`).
4. Copy the `hg_live_...` key immediately — it is shown only once.

### Example request

```bash
curl http://localhost:9100/v1/responses \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_APP_API_KEY" \
  -d '{
    "model": "meta-llama/llama-3.1-8b-instruct:free",
    "input": "Hello from the public test API"
  }'
```

---

## Admin Dashboard

Located at `/admin` (admin password required).

| Tab | Description |
| --- | ----------- |
| **Overview** | Message volume chart, model distribution, PS action breakdown, top users |
| **Prompt Security** | PS mode stats, per-mode toggle cards |
| **Users** | User list with per-user stats, inline edit, detail view with charts |
| **PS Tenants** | Create / edit / delete PS tenants |
| **Activity Log** | Combined view of all chat messages and config change audit events |
| **Settings** | Setting up the Web Application |

---

## License

MIT
