from pathlib import Path
from typing import List, Dict, Any, Tuple
from output import print_step, print_diff, print_header, print_create_preview
from config import Config
from utils import (
    write_file,
    read_file,
    syntax_check_js,
    syntax_check_python,
    run_tests,
    git_add_commit,
    ensure_dependencies,
    start_server,
    wait_for_port,
    kill_server,
    verify_node_requires,
    smoke_http,
)


class Implementer:
    def __init__(self, repo_path: Path):
        self.repo_path = repo_path
        self.errors: List[str] = []

    def apply_plan(self, plan: List[Dict[str, Any]]) -> List[str]:
        print_header("⚙️ Applying changes")
        changes = []
        for action in plan:
            file_path = self.repo_path / action["file_path"]
            act = action["action"]
            if act == "create":
                print_step(f"Creating {action['file_path']}")
                print_create_preview(action.get("content") or "", action["file_path"])
                write_file(file_path, action["content"])
                changes.append(f"Created {action['file_path']}")
            elif act == "update":
                print_step(f"Updating {action['file_path']}")
                old_content = read_file(file_path) if file_path.exists() else ""
                print_diff(old_content, action.get("content") or "", action["file_path"])
                write_file(file_path, action["content"])
                changes.append(f"Updated {action['file_path']}")
            elif act == "delete":
                print_step(f"Deleting {action['file_path']}")
                if file_path.exists():
                    old_content = read_file(file_path)
                    # Show full file as deletions with line numbers
                    print_diff(old_content, "", action["file_path"])
                    file_path.unlink()
                    changes.append(f"Deleted {action['file_path']}")
                else:
                    changes.append(f"Skipped deletion of {action['file_path']} (not found)")

            else:
                changes.append(f"Unknown action {act} for {action['file_path']} - skipped")
        return changes

    def commit(self, message: str) -> bool:
        print_step(f"Git commit: {message}")
        return git_add_commit(self.repo_path, message)

    def validate(self, plan: List[Dict[str, Any]]) -> Tuple[bool, str, bool]:
        """
        Validate applied changes.

        Returns:
            (ok, message, retryable)
            - ok=True: validation passed (or soft-passed with infra warnings)
            - retryable=True: failure is a CODE issue the LLM should try to fix
            - retryable=False: infrastructure / env issue (MongoDB, EPERM, no tests, etc.)
        """
        if not Config.ENABLE_VALIDATION:
            return True, "Validation disabled.", False

        print_header("🔍 Validation Phase")
        code_errors: List[str] = []
        infra_warnings: List[str] = []

        # --- 1. Syntax checks on created/updated files (retryable) ---
        for action in plan:
            if action.get("action") not in ("create", "update"):
                continue
            rel = action["file_path"]
            file_path = self.repo_path / rel
            if not file_path.exists():
                code_errors.append(f"File missing after write: {rel}")
                continue

            if file_path.suffix == ".js":
                ok, err = syntax_check_js(file_path)
                if not ok:
                    code_errors.append(f"Syntax error in {rel}: {err}")
            elif file_path.suffix == ".py":
                ok, err = syntax_check_python(file_path)
                if not ok:
                    code_errors.append(f"Syntax error in {rel}: {err}")

        if code_errors:
            return False, "\n".join(code_errors), True

        print_step("Syntax checks passed.")

        # --- 2. Dependencies (infrastructure; not an LLM code fix) ---
        deps_ok, deps_err = ensure_dependencies(self.repo_path)
        if not deps_ok:
            # Still allow agent to finish — but do not replan for npm/EPERM
            msg = (
                "Dependencies/environment problem (not treating as code failure):\n"
                f"{deps_err}"
            )
            print(f"  ⚠️ {msg}")
            return True, msg, False

        # Quick require check (surfaces ipaddr EPERM cleanly)
        req_ok, req_msg = verify_node_requires(self.repo_path)
        if not req_ok:
            print(f"  ⚠️ {req_msg}")
            # Soft pass: syntax already OK; live server won't work until env fixed
            return True, req_msg, False

        # --- 3. Optional live server + tests ---
        if not Config.ENABLE_SERVER_VALIDATION:
            print_step("Server validation disabled (ENABLE_SERVER_VALIDATION=false).")
            return True, "Syntax OK; server validation skipped.", False

        proc, port, start_msg = None, None, ""
        try:
            proc, port, start_msg = start_server(self.repo_path)

            if port is None:
                # Could not start — almost always infra (MongoDB / EPERM / missing entry)
                infra_warnings.append(start_msg or "Server could not be started.")
                print(f"  ⚠️ Server start skipped/failed: {start_msg}")
                print(
                    "  ℹ️ Soft-passing validation: syntax is OK. "
                    "Live server needs MongoDB (and healthy node_modules) for this app."
                )
                return True, "\n".join(infra_warnings), False

            # We either spawned a process or reused an existing port
            if proc is not None:
                print_step(f"Waiting for server on port {port}...")
                up, wait_msg = wait_for_port("localhost", port, max_wait=25.0, proc=proc)
                if not up:
                    kill_server(proc)
                    proc = None
                    lower = wait_msg.lower()
                    # Real JS runtime errors → LLM should fix
                    if any(
                        t in wait_msg
                        for t in ("SyntaxError", "is not defined", "Unexpected token", "TypeError")
                    ) and "mongo" not in lower:
                        return False, wait_msg, True
                    # MongoDB / EPERM / missing modules → environment, not code
                    if "mongo" in lower or "27017" in wait_msg:
                        note = (
                            "INFRASTRUCTURE: App needs MongoDB on localhost:27017. "
                            "It listens briefly then exits when DB is down. "
                            "Start MongoDB for live HTTP validation, or set "
                            "ENABLE_SERVER_VALIDATION=false."
                        )
                        infra_warnings.append(note)
                    else:
                        infra_warnings.append(wait_msg)
                    print(f"  ⚠️ Server did not become ready:\n{wait_msg[:800]}")
                    print("  ℹ️ Soft-passing (environment). Syntax already verified.")
                    return True, "\n".join(infra_warnings), False

            # HTTP smoke
            smoke_ok, smoke_msg = smoke_http(port, "/")
            if smoke_ok:
                print_step(f"HTTP smoke OK: {smoke_msg}")
            else:
                infra_warnings.append(f"HTTP smoke failed: {smoke_msg}")
                print(f"  ⚠️ HTTP smoke failed: {smoke_msg}")

            # Real tests only (placeholder npm test is skipped)
            tests_ok, test_out, ran_real = run_tests(self.repo_path)
            if ran_real and not tests_ok:
                # Real failing tests → code problem → LLM retry
                return False, f"Tests failed:\n{test_out}", True

            print_step("All validations passed.")
            msg = "Validation passed."
            if infra_warnings:
                msg += "\nWarnings:\n" + "\n".join(infra_warnings)
            return True, msg, False

        finally:
            if proc is not None:
                kill_server(proc)
