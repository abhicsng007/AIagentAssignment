from utils import LLMClient
from prompts import IMPLEMENT_SUMMARY_PROMPT


class Summarizer:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    def summarize(self, user_request: str, plan: list) -> str:
        plan_lines = []
        for i, action in enumerate(plan, 1):
            desc = action.get("description", f"{action['action']} {action['file_path']}")
            plan_lines.append(f"{i}. [{action['action'].upper()}] {action['file_path']} — {desc}")
        plan_text = "\n".join(plan_lines)

        prompt = IMPLEMENT_SUMMARY_PROMPT.format(
            plan_text=plan_text,
            user_request=user_request
        )
        summary = self.llm.chat([{"role": "user", "content": prompt}])
        return summary