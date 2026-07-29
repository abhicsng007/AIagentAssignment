EXPLORATION_SUMMARY_PROMPT = """
You are an expert software engineer. You have been given a codebase to understand.

Here is the repository file tree and metadata:

{repo_tree}

{metadata}

Based on this, provide a concise summary of:
- The technology stack (backend framework, database, templating engine, etc.)
- The main functionality of the application
- Key directories and their roles

Keep it brief but informative.
"""

RELEVANT_FILES_PROMPT = """
You are an expert software engineer. Given a codebase overview and a user request, identify the files that are most relevant to implement the request.

Repository File Tree:
{repo_tree}

Repository Summary:
{repo_summary}

User Request: {user_request}

Your task: output a JSON object with two arrays:
1. "must_read" — files whose full contents are essential to understand before planning (e.g., models, routes, schemas, key views)
2. "should_read" — files that are likely useful but lower priority (e.g., CSS, secondary views, config)

Also include a "search_terms" array of 3-5 keywords to grep for in the repo (e.g., "Note", "todo", "search", "tag").

Output ONLY valid JSON in this exact shape:
{{
  "must_read": ["relative/path/1", "relative/path/2"],
  "should_read": ["relative/path/3"],
  "search_terms": ["keyword1", "keyword2"],
  "reasoning": "Brief explanation of why these files matter"
}}
"""

# Phase 1: skeleton only (no file bodies) — small JSON, rarely truncates
PLAN_SKELETON_PROMPT = """
You are an AI coding assistant. Propose an implementation plan for the user request.

User Request: {user_request}

Repository Summary:
{repo_summary}

Codebase Context (selected relevant files):
{repo_context}

Return ONLY a JSON array of action OBJECTS. Do NOT include full file contents.
Each object must have:
{{
  "file_path": "relative/path/from/repo/root",
  "action": "create" | "update" | "delete",
  "description": "brief summary of the change"
}}

Rules:
- Prefer the fewest files needed (typically 3–6).
- Preserve existing behaviour unless the request requires change.
- For this notes app: do NOT process.exit() when MongoDB is down; keep in-memory
  fallback (USE_MEMORY_DB / memory.store.js) if present; keep /health working.
- If adding tags/search/categories, touch model + controller + routes as needed.

Output ONLY the JSON array, no markdown, no commentary.
"""

# Phase 2: one file at a time — avoids giant truncated JSON
FILE_CONTENT_PROMPT = """
You are implementing a single file change for a coding agent.

User Request: {user_request}

Repository Summary:
{repo_summary}

Action:
- file_path: {file_path}
- action: {action}
- description: {description}

Current file content (empty if new file):
-----BEGIN CURRENT FILE-----
{current_content}
-----END CURRENT FILE-----

Related context (other files, may be truncated):
{repo_context}

Return ONLY a JSON object (no markdown) with this exact shape:
{{
  "file_path": "{file_path}",
  "action": "{action}",
  "description": "{description}",
  "content": "<FULL new file source code as a JSON string>"
}}

Rules for content:
- Provide the COMPLETE file body (not a diff).
- Escape all JSON special characters correctly inside "content".
- Preserve existing functionality; only apply the described change.
- Keep MongoDB in-memory fallback patterns if they already exist.
- Output valid JSON only — the "content" string must be fully closed.
"""

# Legacy single-shot (kept as last-resort fallback)
PLAN_PROMPT = """
You are an AI coding assistant. You will receive a user request and a codebase context.
Your task is to propose a concrete implementation plan to fulfill the request.

User Request: {user_request}

Repository Summary:
{repo_summary}

Codebase Context (selected relevant files):
{repo_context}

Produce a JSON array of actions. EACH action:
{{
    "file_path": "relative/path/from/repo/root",
    "action": "create" | "update" | "delete",
    "description": "brief summary",
    "content": "full new file content for create/update, or null for delete"
}}

CRITICAL:
- Output ONLY valid JSON (array). No markdown fences, no commentary.
- Keep the plan small (max 5 files). Prefer short files.
- "content" must be complete — never truncate mid-string.
- When updating, provide the entire new file content.
- Preserve existing functionality.
- Do NOT process.exit() when MongoDB is unavailable; preserve in-memory fallback.
- Keep /health and root GET working.

Output ONLY the JSON array.
"""

IMPLEMENT_SUMMARY_PROMPT = """
You have just modified the codebase according to the following plan:

{plan_text}

Please summarise the changes made in a few bullet points, focusing on how they address the user request:
{user_request}

Be concise and clear.
"""

VALIDATION_FEEDBACK_PROMPT = """
The following implementation plan was executed but validation failed:

User Request: {user_request}

Original Plan:
{plan_text}

Validation Errors:
{errors}

Produce a corrected plan as a JSON ARRAY of action objects WITHOUT full content first is NOT allowed —
each action must include:
- "file_path": string
- "action": "create" | "update" | "delete"
- "description": string
- "content": full file content (for create/update) — COMPLETE, not truncated

Prefer fixing only the broken files (usually 1–3 files).
Output ONLY valid JSON array. No markdown.
"""

JSON_REPAIR_PROMPT = """
The following text was supposed to be valid JSON but failed to parse.

Parse error: {error}

Broken text (may be truncated):
{broken}

Return a FIXED version that is valid JSON only (same structure intended).
If content was truncated, you may shorten string values but keep the JSON structure valid.
Output ONLY the JSON, no markdown.
"""
