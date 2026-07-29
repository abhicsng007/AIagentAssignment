from pathlib import Path
from typing import Dict, List, Any
from utils import (
    read_file,
    get_file_tree,
    get_repo_metadata,
    grep_repo,
    LLMClient,
)
from prompts import EXPLORATION_SUMMARY_PROMPT, RELEVANT_FILES_PROMPT
from output import print_file_read, print_step, print_header
from config import Config


class Explorer:
    def __init__(self, repo_path: Path):
        self.repo_path = repo_path
        self.files_content: Dict[str, str] = {}
        self.summary: str = ""
        self.file_tree: str = ""
        self.metadata: Dict[str, Any] = {}

    def explore(self) -> None:
        print_step("Phase 1: Scanning repository structure...")
        self.file_tree = get_file_tree(self.repo_path)
        self.metadata = get_repo_metadata(self.repo_path)

    def summarize(self, llm: LLMClient) -> str:
        print_step("Generating repository summary with LLM...")
        meta_text = ""
        for k, v in self.metadata.items():
            meta_text += f"\n--- {k} ---\n{v}\n"

        prompt = EXPLORATION_SUMMARY_PROMPT.format(
            repo_tree=self.file_tree[:Config.MAX_TREE_CHARS],
            metadata=meta_text,
        )
        try:
            self.summary = llm.chat([{"role": "user", "content": prompt}])
        except Exception as e:
            print(f"  ⚠️ Summary LLM call failed: {e}")
            # Lightweight local fallback so the pipeline can continue
            pkg = self.metadata.get("package_json", "")[:400]
            self.summary = (
                "Heuristic summary (LLM unavailable):\n"
                f"- Repo root: {self.repo_path.name}\n"
                f"- package.json snippet: {pkg or 'n/a'}\n"
                "- Likely a small Node/Express notes API if server.js / note.* files exist.\n"
            )
        print(self.summary)
        return self.summary

    def _heuristic_relevant_files(self, user_request: str) -> Dict[str, Any]:
        """Offline fallback when the LLM is rate-limited or unavailable."""
        print_step("Using heuristic file selection (LLM unavailable)…")
        candidates: List[str] = []
        for f in sorted(self.repo_path.rglob("*")):
            if not f.is_file():
                continue
            if any(p in f.parts for p in ("node_modules", ".git", "dist", "build", ".venv", "__pycache__")):
                continue
            if f.suffix.lower() in {".js", ".ts", ".jsx", ".tsx", ".py", ".json", ".ejs", ".html"}:
                # Skip lockfiles / huge manifests as "must read"
                if f.name in ("package-lock.json", "yarn.lock"):
                    continue
                candidates.append(str(f.relative_to(self.repo_path)).replace("\\", "/"))

        priority_names = (
            "server.js", "app.js", "index.js", "main.js",
            "note.model.js", "note.controller.js", "note.routes.js",
            "database.config.js", "package.json", "memory.store.js",
        )
        must, should = [], []
        for c in candidates:
            base = c.split("/")[-1]
            if base in priority_names or any(
                k in c.lower() for k in ("model", "controller", "route", "server", "app/")
            ):
                must.append(c)
            elif c.endswith((".js", ".py")):
                should.append(c)

        # Cap lists
        must = must[:12]
        should = [s for s in should if s not in must][:8]

        words = [w for w in user_request.replace(",", " ").split() if len(w) > 3]
        search_terms = list(dict.fromkeys(
            ["note", "notes", "search", "tag", "tags", "categor"] + words[:5]
        ))[:6]

        return {
            "must_read": must,
            "should_read": should,
            "search_terms": search_terms,
            "reasoning": "Heuristic fallback after LLM rate limit / failure",
        }

    def select_relevant_files(self, user_request: str, llm: LLMClient) -> Dict[str, Any]:
        print_step("Phase 2: Asking LLM which files are relevant...")
        prompt = RELEVANT_FILES_PROMPT.format(
            repo_tree=self.file_tree[:Config.MAX_TREE_CHARS],
            repo_summary=self.summary,
            user_request=user_request,
        )
        try:
            result = llm.chat_structured([{"role": "user", "content": prompt}])
            if not isinstance(result, dict):
                raise ValueError(f"Expected dict, got {type(result)}")
            return result
        except Exception as e:
            print(f"  ⚠️ Relevant-file LLM call failed: {e}")
            return self._heuristic_relevant_files(user_request)

    def deep_read(self, file_list: List[str]) -> None:
        print_step("Phase 3: Reading selected files...")
        for rel_path in file_list:
            file_path = self.repo_path / rel_path
            if file_path.exists() and file_path.is_file():
                print_file_read(rel_path)
                self.files_content[rel_path] = read_file(file_path)
            else:
                print(f"  ⚠️ File not found: {rel_path}")

    def grep_and_augment(self, terms: List[str]) -> None:
        print_step("Phase 4: Grepping for keywords...")
        for term in terms:
            matches = grep_repo(self.repo_path, term)
            for m in matches[:10]:
                rel = m["file"]
                if rel not in self.files_content:
                    self.files_content[rel] = read_file(self.repo_path / rel)

    def get_context(self) -> str:
        context = "Repository structure and selected file contents:\n"
        for path, content in self.files_content.items():
            context += f"\n--- {path} ---\n{content}\n"
        return context

    def build_context(self, user_request: str, llm: LLMClient) -> str:
        """Full two-phase exploration pipeline."""
        self.explore()
        self.summarize(llm)

        selection = self.select_relevant_files(user_request, llm)
        must_read = selection.get("must_read", [])
        should_read = selection.get("should_read", [])
        search_terms = selection.get("search_terms", [])

        print_header("📂 Selected Files")
        print(f"  Must read: {must_read}")
        print(f"  Should read: {should_read}")
        print(f"  Search terms: {search_terms}")

        self.deep_read(must_read + should_read)
        self.grep_and_augment(search_terms)

        context = self.get_context()
        if len(context) > Config.MAX_FILE_CONTEXT:
            sorted_files = sorted(self.files_content.items(), key=lambda x: len(x[1]), reverse=True)
            trimmed = {}
            budget = Config.MAX_FILE_CONTEXT
            for path, content in sorted_files:
                if len(content) > 3000:
                    content = content[:3000] + "\n... (truncated)"
                if len(content) + len(path) + 10 > budget:
                    break
                trimmed[path] = content
                budget -= len(content) + len(path) + 10
            context = "Repository structure and selected file contents:\n"
            for path, content in trimmed.items():
                context += f"\n--- {path} ---\n{content}\n"
        return context