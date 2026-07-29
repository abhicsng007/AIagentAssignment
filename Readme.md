# AI Coding Agent

An autonomous **Python 3.11+** agent that understands an existing codebase and implements a product requirement with minimal user guidance.

Built for the interview assignment. The agent is **not hard-coded** to a single feature: it takes any natural-language request and generalises to new tasks on the same (or another) repository.

**Target application (assignment):** [callicoder/node-easy-notes-app](https://github.com/callicoder/node-easy-notes-app)  
The agent **modifies** that Node.js/Express app in place. The notes app is **not** rewritten in Python.

---

## Assignment alignment

| Expectation | How this agent meets it |
|---|---|
| Explore the repository | File tree, metadata, LLM summary, keyword search |
| Identify relevant files automatically | LLM selection (`must_read` / `should_read`) + grep fallback |
| Create a brief execution plan | Two-phase planner (skeleton → per-file content) |
| Modify the codebase | Create / update / delete with full-file writes |
| Summarise the changes | LLM summary + `AGENT_CHANGES.md` log |
| Preserve existing functionality | Plan prompts + syntax/validation checks |

**Demo user request (assignment):**

```text
Improve the application so users can better organise and search their notes.
```

The agent decides a reasonable implementation (e.g. tags, categories, query filters) without further specification.

---

## Architecture

The system is a **linear multi-stage pipeline** (not a multi-agent framework). Each stage is a small Python module with a single responsibility.

```
┌─────────────┐    ┌──────────┐    ┌──────────────┐    ┌────────────┐
│  Explorer   │ →  │ Planner  │ →  │ Implementer  │ →  │ Summarizer │
│  (context)  │    │  (plan)  │    │  (writes)    │    │  (report)  │
└─────────────┘    └──────────┘    └──────────────┘    └────────────┘
        │                │                 │
        └──── LLM via OpenRouter (utils.LLMClient) ────┘
```

| Module | Role |
|---|---|
| `agent.py` | CLI entrypoint; orchestrates phases and validation retries |
| `explorer.py` | Repository scan, relevance selection, context assembly |
| `planner.py` | Builds a structured JSON plan (skeleton + file bodies) |
| `implementer.py` | Applies file actions; syntax/server validation |
| `summarizer.py` | Human-readable change summary |
| `utils.py` | LLM client, file I/O, git, grep, server helpers, JSON repair |
| `prompts.py` | All system/user prompt templates |
| `config.py` | Environment-driven settings |
| `output.py` | Coloured terminal output (steps, diffs with line numbers) |

**LLM access:** [OpenRouter](https://openrouter.ai/) chat completions API. Model is configurable via `OPENROUTER_MODEL` (Claude, GPT, Qwen, etc.).

---

## Agent workflow

```
python agent.py <path-to-repo> "<user-request>"
```

### Phase 1 — Exploration

1. Build a concise **file tree** (skips `node_modules`, `.git`, build dirs).
2. Collect **metadata** (`package.json`, `README`, `requirements.txt` snippets).
3. Ask the LLM for a short **repository summary** (stack + purpose).
4. Ask the LLM which files are **relevant** to the user request (`must_read`, `should_read`, `search_terms`).
5. **Deep-read** selected files and **grep** for keywords to pull in related files.
6. Assemble a bounded context string (token budgets in `config.py`).

If the LLM is rate-limited (HTTP 429), exploration falls back to **heuristics** so the pipeline can continue.

### Phase 2 — Planning

Two-phase planning avoids truncated mega-JSON (full file bodies in one response):

1. **Skeleton** — JSON list of `{file_path, action, description}` only.
2. **Per-file content** — for each `create`/`update`, a separate LLM call returns the **complete** new file body.

Fallback: single-shot full plan with robust JSON parse/repair if two-phase fails.

### Phase 3 — Implementation

For each plan action:

- **create** — write new file (green preview with line numbers)
- **update** — show coloured diff (old/new line numbers; light green `+` / light red `-`), then overwrite
- **delete** — remove file (shown as deletions)

Optional **git commit** when `ENABLE_GIT=true`.

### Phase 4 — Validation (optional)

When `ENABLE_VALIDATION=true`:

1. **Syntax** — `node --check` / `python -m py_compile` on changed files (**retryable** via LLM if broken).
2. **Dependencies** — `npm install` if needed; detect broken installs (e.g. Windows EPERM on packages).
3. **Live server** (if `ENABLE_SERVER_VALIDATION=true`) — start app, wait for port, HTTP smoke test.
4. **Tests** — run real test suites only; **skip** placeholder `npm test` scripts.

Infrastructure failures (MongoDB down, missing deps) are **soft-passed** and do **not** burn LLM fix retries. Real syntax/test failures **are** retried (up to `MAX_RETRIES`).

### Phase 5 — Summarisation

LLM produces a short bullet summary. A full log is written to:

```text
<target-repo>/AGENT_CHANGES.md
```

---

## How the repository is explored

Exploration is **tool-assisted**, not “paste the whole monorepo into the prompt”.

| Step | Mechanism | Purpose |
|---|---|---|
| Structure | Recursive file tree | Orientation without reading every file |
| Metadata | `package.json` / README / requirements | Stack and entrypoints |
| Summary | LLM over tree + metadata | High-level understanding |
| Relevance | LLM JSON: `must_read`, `should_read`, `search_terms` | Focus on the user request |
| Deep read | Load selected file contents | Planning context |
| Grep | Case-insensitive search over source extensions | Catch related symbols/usages |
| Budget | Truncate large files / cap total context | Stay within model limits |

**Ignored paths:** `node_modules`, `.git`, `dist`, `build`, `.venv`, `__pycache__`, etc.

**Supported source-ish extensions for search:** `.js`, `.ts`, `.ejs`, `.json`, `.html`, `.css`, `.py`, `.sql`, `.md`, …

The notes app may include a **MongoDB → in-memory fallback** so demos and validation work without a local database. Planner prompts instruct the agent to preserve that pattern when editing.

---

## Assumptions & trade-offs

| Choice | Assumption / trade-off |
|---|---|
| **OpenRouter** | One API key unlocks many models; requires network and a valid key. Rate limits (429) are retried with backoff. |
| **Full-file writes** | Simpler and more reliable than patches for small apps; wasteful on large files. |
| **Two-phase plan** | More LLM calls, but avoids truncated JSON when bodies are large. |
| **No interactive clarifying questions** | Ambiguous requests are resolved by the model (e.g. tags + search). |
| **Context budgets** | Large repos are truncated; edge files may be missed. |
| **Validation** | Syntax is hard; live server depends on the environment (Node, MongoDB, ports). Soft-pass on infra issues. |
| **Placeholder tests** | Easy Notes ships `npm test` that always fails; the agent skips that script. |
| **Git commit** | Best-effort; skipped if the target is not a git repo or has nothing to commit. |
| **Windows** | Server start uses `node`/`npm.cmd` carefully; process kill uses `taskkill` for process trees. |
| **Generalisation** | Any request string works; interview follow-ups do not require code changes to the agent. |

---

## Project layout

```text
gitAgent/
├── agent.py              # CLI orchestrator
├── explorer.py           # Repo exploration
├── planner.py            # Plan generation
├── implementer.py        # Apply + validate
├── summarizer.py         # Change summary
├── utils.py              # LLM, FS, process helpers
├── prompts.py            # Prompt templates
├── config.py             # Settings
├── output.py             # Terminal UI / diffs
├── requirements.txt
├── .env.example
├── Readme.md
└── node-easy-notes-app/  # Target repo (clone separately if needed)
```

---

## Setup

### Prerequisites

- **Python 3.11+**
- **Node.js** (to run / validate the notes app)
- **OpenRouter API key** — [https://openrouter.ai/](https://openrouter.ai/)
- Optional: **MongoDB** on `localhost:27017` for persistent notes storage (in-memory fallback works without it)

### Install

```bash
# 1. Clone this agent repository
git clone <your-github-url>
cd gitAgent   # or your repo folder name

# 2. Python dependencies
pip install -r requirements.txt

# 3. Target application (if not already present)
git clone https://github.com/callicoder/node-easy-notes-app.git
cd node-easy-notes-app
npm install
cd ..

# 4. Environment
copy .env.example .env    # Windows
# cp .env.example .env    # macOS / Linux
```

Edit `.env`:

```env
OPENROUTER_API_KEY=sk-or-v1-your-key-here
OPENROUTER_MODEL=anthropic/claude-3.5-sonnet

# Optional tuning
ENABLE_VALIDATION=true
ENABLE_SERVER_VALIDATION=true
ENABLE_GIT=true
MAX_RETRIES=2
LLM_MAX_RETRIES=6
LLM_CALL_GAP=1.5
```

---

## Usage

### Assignment demo request

```bash
python agent.py ./node-easy-notes-app "Improve the application so users can better organise and search their notes."
```

### Other requests (interview-style generalisation)

```bash
python agent.py ./node-easy-notes-app "Add pagination to the notes list API"
python agent.py ./node-easy-notes-app "Add a soft-delete flag instead of hard deletes"
```

### What you should see

1. Exploration logs (tree summary, selected files)
2. Plan skeleton, then per-file content generation
3. Coloured diffs / create previews with line numbers
4. Validation (syntax / optional HTTP smoke)
5. Written summary and `node-easy-notes-app/AGENT_CHANGES.md`

### Manual check of the notes app

```bash
cd node-easy-notes-app
node server.js
# GET  http://localhost:3000/
# GET  http://localhost:3000/health
# CRUD http://localhost:3000/notes
```

---

## Configuration reference

| Variable | Default | Meaning |
|---|---|---|
| `OPENROUTER_API_KEY` | _(required)_ | OpenRouter API key |
| `OPENROUTER_MODEL` | `anthropic/claude-3.5-sonnet` | Model id |
| `ENABLE_VALIDATION` | `true` | Run validation after writes |
| `ENABLE_SERVER_VALIDATION` | `true` | Try live server + HTTP smoke |
| `ENABLE_GIT` | `true` | Commit changes in the target repo |
| `MAX_RETRIES` | `2` | LLM fix attempts for code errors |
| `LLM_MAX_RETRIES` | `6` | HTTP retries on 429/5xx |
| `LLM_CALL_GAP` | `1.5` | Seconds between LLM calls |
| `PLAN_MAX_TOKENS` | `16000` | Max tokens for plan responses |
| `MAX_PLAN_CONTEXT` | `20000` | Char budget for plan context |

---

## Deliverables checklist

| Deliverable | Status / notes |
|---|---|
| Source code → GitHub URL | Push this repository and share the URL |
| README (architecture, workflow, exploration, trade-offs) | This document |
| 2–3 min screen recording → Google Drive link | Record a run of the assignment request; paste the link in your submission email / form |

**Suggested recording outline**

1. Show the clean notes app (or pre-run tree).
2. Run the agent with the assignment request.
3. Show plan + diffs scrolling.
4. Show `AGENT_CHANGES.md` and a quick API/browser check of organise/search behaviour.

---

## Design notes for the follow-up interview

- **Generalisation:** only the CLI request string changes; exploration + planning are request-driven.
- **Why full-file writes:** reliability over surgical patches for a small Express app in a 2–3 hour assignment.
- **Why two-phase planning:** full multi-file JSON often hits output token limits (`Unterminated string` / invalid JSON).
- **Why soft-pass infra validation:** MongoDB or `node_modules` permission errors are environment issues, not model coding errors.
- **Failure modes:** rate limits (retried), bad JSON (repaired / replanned), syntax errors (retry loop).

---

## License

Assignment submission — use and evaluation by the interviewing company as needed.
