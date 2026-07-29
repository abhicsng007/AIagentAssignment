#!/usr/bin/env python3
import sys
from pathlib import Path
from config import Config
from utils import LLMClient
from explorer import Explorer
from planner import Planner
from implementer import Implementer
from summarizer import Summarizer
from output import print_header, print_step, print_file_read
from colorama import Fore, Style


class CodingAgent:
    def __init__(self, repo_path: Path):
        self.repo_path = repo_path
        self.llm = LLMClient()
        self.explorer = Explorer(repo_path)
        self.planner = Planner(self.explorer, self.llm)
        self.implementer = Implementer(repo_path)
        self.summarizer = Summarizer(self.llm)

    def run(self, user_request: str):
        print_header(f"🚀 AI Coding Agent – Request: {user_request}")

        # Phase 1: Smart Exploration
        print_header("🔍 Exploration Phase")
        context = self.explorer.build_context(user_request, self.llm)

        # Phase 2: Planning
        print_header("🧠 Planning Phase")
        plan = self.planner.generate_plan(user_request)

        # Phase 3: Implementation + Validation (with retries)
        print_header("⚙️ Implementation Phase")
        ok, error_msg, retryable = True, "", False
        for attempt in range(1, Config.MAX_RETRIES + 1):
            changes = self.implementer.apply_plan(plan)
            for change in changes:
                print(f"  ✓ {change}")

            ok, error_msg, retryable = self.implementer.validate(plan)
            if ok:
                if error_msg and error_msg not in ("Validation passed.", "Validation disabled."):
                    print(f"{Fore.YELLOW}ℹ️ Validation notes:{Style.RESET_ALL}\n{error_msg}")
                break

            print(f"{Fore.YELLOW}⚠️ Validation failed (attempt {attempt}/{Config.MAX_RETRIES}):{Style.RESET_ALL}")
            print(error_msg)

            # Infrastructure failures (MongoDB down, EPERM on ipaddr.js, missing npm) —
            # do NOT burn retries asking the LLM to "fix" the environment.
            if not retryable:
                print(
                    f"{Fore.YELLOW}⚠️ Non-retryable (environment/infra). "
                    f"Continuing with applied changes.{Style.RESET_ALL}"
                )
                break

            if attempt < Config.MAX_RETRIES:
                print_step("Asking LLM to fix the plan (code error)...")
                from prompts import VALIDATION_FEEDBACK_PROMPT
                from utils import parse_llm_json, normalize_plan

                plan_text = "\n".join(
                    f"{i+1}. [{a['action'].upper()}] {a['file_path']}"
                    for i, a in enumerate(plan)
                )
                prompt = VALIDATION_FEEDBACK_PROMPT.format(
                    user_request=user_request,
                    plan_text=plan_text,
                    errors=error_msg,
                )
                try:
                    raw = self.llm.chat(
                        [{"role": "user", "content": prompt}],
                        temperature=0.2,
                        max_tokens=Config.PLAN_MAX_TOKENS,
                    )
                    plan = normalize_plan(parse_llm_json(raw))
                    for action in plan:
                        if action["action"] in ("create", "update") and not action.get("content"):
                            raise ValueError(f"Fix plan missing content for {action['file_path']}")
                except Exception as e:
                    print(f"{Fore.RED}❌ Could not parse fix plan: {e}{Style.RESET_ALL}")
                    # Fall back to regenerating via two-phase planner for the fix request
                    try:
                        fix_req = (
                            f"{user_request}\n\nFix these validation errors:\n{error_msg}"
                        )
                        plan = self.planner.generate_plan(fix_req)
                    except Exception as e2:
                        print(f"{Fore.RED}❌ Replan failed: {e2}. Stopping retries.{Style.RESET_ALL}")
                        break
            else:
                print(f"{Fore.RED}❌ Max retries reached. Proceeding with best effort.{Style.RESET_ALL}")

        # Phase 4: Git commit
        if Config.ENABLE_GIT:
            self.implementer.commit(f"AI agent: {user_request[:50]}")

        # Phase 5: Summarization
        print_header("📄 Summarising Changes")
        final_summary = self.summarizer.summarize(user_request, plan)
        print(final_summary)

        # Write log
        with open(self.repo_path / "AGENT_CHANGES.md", "w") as f:
            f.write(f"# AI Agent Changes\n\n## User Request\n{user_request}\n\n")
            f.write("## Implementation Plan\n")
            for action in plan:
                f.write(f"- {action['action']} `{action['file_path']}`: {action.get('description', '')}\n")
            f.write("\n## Summary\n" + final_summary)
            if not ok:
                f.write(f"\n\n## Validation Warnings\n{error_msg}")
        print(f"\n{Fore.GREEN}✅ Done! Detailed log written to AGENT_CHANGES.md{Style.RESET_ALL}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python agent.py <path-to-repo> \"<user-request>\"")
        sys.exit(1)
    repo_path = Path(sys.argv[1]).resolve()
    if not repo_path.exists():
        print(f"Error: Repository path {repo_path} does not exist.")
        sys.exit(1)
    user_request = sys.argv[2]
    agent = CodingAgent(repo_path)
    agent.run(user_request)