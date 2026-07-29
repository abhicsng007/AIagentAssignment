import json
from typing import List, Dict, Any, Optional
from utils import LLMClient, parse_llm_json, normalize_plan, read_file
from prompts import (
    PLAN_SKELETON_PROMPT,
    FILE_CONTENT_PROMPT,
    PLAN_PROMPT,
    JSON_REPAIR_PROMPT,
)
from explorer import Explorer
from output import print_plan_action, print_step, print_header
from config import Config
from colorama import Fore, Style


class Planner:
    def __init__(self, explorer: Explorer, llm: LLMClient):
        self.explorer = explorer
        self.llm = llm

    def _context(self, limit: Optional[int] = None) -> str:
        limit = limit or Config.MAX_PLAN_CONTEXT
        context = self.explorer.get_context()
        if len(context) > limit:
            context = context[:limit] + "\n... (truncated)"
        return context

    def _chat_json(self, prompt: str, temperature: float = 0.2, max_tokens: int = None) -> Any:
        """Call LLM and parse JSON, with one repair retry on failure."""
        max_tokens = max_tokens or Config.PLAN_MAX_TOKENS
        raw = self.llm.chat(
            [{"role": "user", "content": prompt}],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        try:
            return parse_llm_json(raw)
        except (json.JSONDecodeError, ValueError) as e:
            print(f"  {Fore.YELLOW}⚠️ JSON parse failed ({e}). Asking model to repair…{Style.RESET_ALL}")
            repair_prompt = JSON_REPAIR_PROMPT.format(
                error=str(e),
                broken=raw[:12000],
            )
            raw2 = self.llm.chat(
                [{"role": "user", "content": repair_prompt}],
                temperature=0.0,
                max_tokens=max_tokens,
            )
            return parse_llm_json(raw2)

    def _generate_skeleton(self, user_request: str) -> List[Dict[str, Any]]:
        print_step("Step 1/2: Planning which files to change (no content yet)…")
        prompt = PLAN_SKELETON_PROMPT.format(
            user_request=user_request,
            repo_summary=self.explorer.summary,
            repo_context=self._context(),
        )
        data = self._chat_json(prompt, temperature=Config.PLAN_TEMPERATURE, max_tokens=4000)
        skeleton = normalize_plan(data)
        # Skeleton should not require content yet
        for a in skeleton:
            a.pop("content", None)
        if not skeleton:
            raise ValueError("Empty plan skeleton from model")
        print_header("📋 Plan skeleton")
        for action in skeleton:
            print_plan_action(action)
        return skeleton

    def _fill_content(self, user_request: str, skeleton: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        print_step("Step 2/2: Generating full file contents (one file at a time)…")
        # Smaller related context per file to leave room for the new body
        related = self._context(limit=min(12000, Config.MAX_PLAN_CONTEXT))
        plan: List[Dict[str, Any]] = []

        for i, action in enumerate(skeleton, 1):
            act = action["action"]
            path = action["file_path"]
            desc = action.get("description", f"{act} {path}")

            if act == "delete":
                plan.append({
                    "file_path": path,
                    "action": "delete",
                    "description": desc,
                    "content": None,
                })
                print(f"  ✓ [{i}/{len(skeleton)}] delete {path}")
                continue

            current = ""
            file_on_disk = self.explorer.repo_path / path
            if file_on_disk.exists():
                current = read_file(file_on_disk)
            elif path in self.explorer.files_content:
                current = self.explorer.files_content[path]

            # Cap current file to avoid blowing the prompt
            if len(current) > 12000:
                current = current[:12000] + "\n/* … truncated for prompt … */"

            print(f"  ▶ [{i}/{len(skeleton)}] Generating content for {path}…")
            prompt = FILE_CONTENT_PROMPT.format(
                user_request=user_request,
                repo_summary=self.explorer.summary or "",
                file_path=path,
                action=act,
                description=desc,
                current_content=current if current else "(file does not exist yet)",
                repo_context=related,
            )

            try:
                obj = self._chat_json(
                    prompt,
                    temperature=Config.PLAN_TEMPERATURE,
                    max_tokens=Config.FILE_CONTENT_MAX_TOKENS,
                )
            except Exception as e:
                print(f"  {Fore.RED}✗ Failed to generate {path}: {e}{Style.RESET_ALL}")
                raise

            # Accept either full action object or {content: ...}
            if isinstance(obj, list) and obj:
                obj = obj[0]
            if not isinstance(obj, dict):
                raise ValueError(f"Expected object for {path}, got {type(obj)}")

            content = obj.get("content")
            if content is None and act in ("create", "update"):
                # Sometimes model returns { "code": "..." } or raw string
                content = obj.get("code") or obj.get("source")
            if content is None:
                raise ValueError(f"Model returned no content for {path}")
            if not isinstance(content, str):
                content = str(content)

            # Strip accidental fences inside content
            if content.strip().startswith("```"):
                lines = content.strip().splitlines()
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                content = "\n".join(lines)

            plan.append({
                "file_path": path,
                "action": act,
                "description": obj.get("description", desc),
                "content": content,
            })
            print(f"  ✓ [{i}/{len(skeleton)}] {act} {path} ({len(content)} chars)")

        return plan

    def _generate_single_shot(self, user_request: str) -> List[Dict[str, Any]]:
        """Fallback: one big JSON plan (higher token limit + parse repair)."""
        print_step("Fallback: single-shot full plan…")
        prompt = PLAN_PROMPT.format(
            user_request=user_request,
            repo_summary=self.explorer.summary,
            repo_context=self._context(),
        )
        data = self._chat_json(
            prompt,
            temperature=Config.PLAN_TEMPERATURE,
            max_tokens=Config.PLAN_MAX_TOKENS,
        )
        plan = normalize_plan(data)
        for action in plan:
            if action["action"] in ("create", "update") and not action.get("content"):
                raise ValueError(f"Action missing content: {action.get('file_path')}")
        return plan

    def generate_plan(self, user_request: str) -> List[Dict[str, Any]]:
        print_step("Asking LLM for implementation plan…")
        last_err: Optional[Exception] = None

        # Preferred path: skeleton → per-file content (avoids truncated mega-JSON)
        try:
            skeleton = self._generate_skeleton(user_request)
            plan = self._fill_content(user_request, skeleton)
            return self._finalize(plan)
        except Exception as e:
            last_err = e
            print(f"  {Fore.YELLOW}⚠️ Two-phase plan failed: {e}{Style.RESET_ALL}")

        # Fallback single-shot
        try:
            plan = self._generate_single_shot(user_request)
            return self._finalize(plan)
        except Exception as e:
            last_err = e
            print(f"  {Fore.RED}✗ Single-shot plan also failed: {e}{Style.RESET_ALL}")

        raise RuntimeError(
            f"Could not produce a valid implementation plan. Last error: {last_err}"
        ) from last_err

    def _finalize(self, plan: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        plan = normalize_plan(plan)
        # Drop empty / invalid content actions early with a clear message
        cleaned = []
        for action in plan:
            if action["action"] in ("create", "update"):
                content = action.get("content")
                if content is None or (isinstance(content, str) and not content.strip()):
                    raise ValueError(
                        f"Empty content for {action['action']} {action['file_path']}"
                    )
            cleaned.append(action)

        print_header("📋 Generated Plan (ready to apply)")
        for action in cleaned:
            print_plan_action(action)
        return cleaned
