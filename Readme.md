# AI Coding Agent

A small Python agent that reads an existing repo, decides what to change, applies the edits, and writes up what it did. It talks to models through [OpenRouter](https://openrouter.ai/), so you can swap Claude / GPT / Qwen / etc. without changing code.

It’s built to work on real apps in place. The usual demo target is [node-easy-notes-app](https://github.com/callicoder/node-easy-notes-app) (Express + Mongo notes API). The agent edits that project as JavaScript — it does not reimplement it in Python.

```bash
python agent.py ./node-easy-notes-app "Improve the application so users can better organise and search their notes."
```

Any other request string works the same way; nothing in the pipeline is hard-wired to tags or search.

---

## Layout

```
agent.py          CLI + orchestration
explorer.py       scan repo, pick files, build context
planner.py        turn request + context into a file plan
implementer.py    write files, run light validation
summarizer.py     short human summary of the diff
utils.py          OpenRouter client, FS, grep, process helpers
prompts.py        prompt templates
config.py         env-based settings
output.py         terminal logging and coloured diffs
```

---

## How it works

End to end the run is a straight pipeline:

**explore → plan → apply → (optional validate / retry) → summarise**

### 1. Exploration

The explorer doesn’t dump the whole tree into the model.

1. Walk the tree (skipping `node_modules`, `.git`, build dirs, and similar noise).
2. Pull short metadata from things like `package.json` and README when they exist.
3. Ask the model for a compact description of the stack and purpose.
4. Ask again which paths matter for *this* request (`must_read`, `should_read`, plus a few grep terms).
5. Read those files and run keyword search to pull in anything related that was missed.
6. Cap total context size so planning stays within reasonable limits.

If OpenRouter is rate-limiting (429s), exploration degrades to a simple heuristic file list instead of dying. Planning still needs a working model call.

### 2. Planning

Emitting every full file body in one giant JSON blob tends to truncate mid-string and blow up `json.loads`. Planning is therefore split:

1. **Skeleton** — list of `{ file_path, action, description }` only (`create` / `update` / `delete`).
2. **Bodies** — one call per file that needs content, returning the complete new source.

There’s also a single-shot fallback with fence stripping, salvage of complete array items, and a repair pass if the first parse fails.

### 3. Applying changes

The implementer walks the plan and writes to disk. Updates print a line-numbered diff (green adds, red deletes). New files get a green preview. Deletes show the removed content the same way.

If git is enabled and the target is a repo, it attempts a single commit at the end.

### 4. Validation

After writes, optional checks:

- Syntax on changed `.js` / `.py` files (`node --check`, `py_compile`).
- Install missing Node deps if `node_modules` is empty or looks broken.
- Optionally start the app and hit `/` or `/health`.
- Run a real test suite if one exists; ignore the stock Easy Notes placeholder that always exits 1.

Syntax / real test failures can re-enter planning a couple of times. Environment problems (Mongo not running, Windows permission issues under `node_modules`) are reported and soft-passed so the model isn’t asked to “fix” the OS.

### 5. Summary

A short bullet summary is printed and also written to `AGENT_CHANGES.md` under the target repo (request, plan, summary, any validation notes).

---

## Design choices

**Full file rewrites, not patches.** For a small Express app this is simpler and less error-prone than teaching the model to emit unified diffs. On large files it’s wasteful; context caps mitigate that a bit.

**OpenRouter instead of a single vendor SDK.** One key, many models. Cost and rate limits depend on the model you pick.

**No clarifying questions mid-run.** Vague product language (“organise and search”) is interpreted by the planner — typically tags/categories plus filtered list endpoints, or similar.

**Retries where they help.** HTTP 429/5xx get exponential backoff. Broken plan JSON gets repair / replan. Broken syntax can loop. Infra failures do not.

**Target-repo specific habits.** For the notes app, prompts tell the model not to `process.exit()` when Mongo is down, and to keep any in-memory fallback / `/health` route if those already exist so demos still boot offline.

---

## Setup

Needs Python 3.11+, Node (for the notes app), and an OpenRouter key. MongoDB on `localhost:27017` is optional if the app can fall back to memory.

```bash
git clone <this-repo>
cd <this-repo>
pip install -r requirements.txt

git clone https://github.com/callicoder/node-easy-notes-app.git
cd node-easy-notes-app && npm install && cd ..

cp .env.example .env   # Windows: copy .env.example .env
```

`.env` (minimum):

```env
OPENROUTER_API_KEY=sk-or-v1-...
OPENROUTER_MODEL=anthropic/claude-3.5-sonnet
```

Useful knobs (all optional):

| Variable | Default | Notes |
|---|---|---|
| `ENABLE_VALIDATION` | `true` | Post-write checks |
| `ENABLE_SERVER_VALIDATION` | `true` | Try live server + HTTP smoke |
| `ENABLE_GIT` | `true` | Commit in the target tree |
| `MAX_RETRIES` | `2` | Code-fix replan attempts |
| `LLM_MAX_RETRIES` | `6` | HTTP retries on 429/5xx |
| `LLM_CALL_GAP` | `1.5` | Pause between model calls |
| `PLAN_MAX_TOKENS` | `16000` | Headroom for large plan outputs |

Don’t commit `.env`. `.gitignore` already excludes it, `node_modules`, `__pycache__`, and `AGENT_CHANGES.md`.

---

## Running

```bash
python agent.py <path-to-repo> "<request>"
```

Examples:

```bash
python agent.py ./node-easy-notes-app "Improve the application so users can better organise and search their notes."

python agent.py ./node-easy-notes-app "Add pagination to GET /notes"
```

Afterwards:

```bash
cd node-easy-notes-app
node server.js
# open http://localhost:3000/  and  /notes
```

---

## Limits

- Large monorepos will be under-sampled; only ranked files make it into context.
- The model can still invent APIs that don’t match local style; validation only catches syntax and whatever tests exist.
- Free / low-tier OpenRouter keys hit 429 often — wait, raise `LLM_CALL_GAP`, or switch model.
- Live validation is only as good as the machine (Node on PATH, free port, healthy `node_modules`).

---

## License

Use as needed for evaluation and interview follow-ups.
