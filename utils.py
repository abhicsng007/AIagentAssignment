import os
import json
import re
import shutil
import subprocess
import requests
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional
from config import Config
import time
import socket
import platform
import signal


SKIP_DIRS = {"node_modules", ".git", "dist", "build", ".venv", "__pycache__", ".next", "coverage"}


class LLMClient:
    """OpenRouter chat client with rate-limit aware retries."""

    # Transient / rate-limit statuses worth retrying
    _RETRY_STATUSES = {429, 500, 502, 503, 504}

    def __init__(self):
        self.api_key = Config.OPENROUTER_API_KEY
        self.model = Config.OPENROUTER_MODEL
        self.base_url = Config.OPENROUTER_BASE_URL
        self._last_call_at = 0.0
        if not self.api_key:
            raise ValueError("OPENROUTER_API_KEY not set")

    def _throttle(self) -> None:
        """Small gap between calls to reduce burst 429s."""
        gap = getattr(Config, "LLM_CALL_GAP", 1.5)
        if gap <= 0:
            return
        elapsed = time.time() - self._last_call_at
        if self._last_call_at and elapsed < gap:
            time.sleep(gap - elapsed)

    def _retry_delay(self, response: Optional[requests.Response], attempt: int) -> float:
        """Prefer Retry-After header; else exponential backoff with jitter."""
        if response is not None:
            ra = response.headers.get("Retry-After") or response.headers.get("retry-after")
            if ra:
                try:
                    return min(float(ra), Config.LLM_RETRY_MAX_DELAY)
                except ValueError:
                    pass
            # OpenRouter sometimes exposes reset hints
            reset = response.headers.get("X-RateLimit-Reset")
            if reset:
                try:
                    # unix timestamp or seconds
                    val = float(reset)
                    if val > 1e9:  # timestamp
                        return min(max(val - time.time(), 1.0), Config.LLM_RETRY_MAX_DELAY)
                    return min(max(val, 1.0), Config.LLM_RETRY_MAX_DELAY)
                except ValueError:
                    pass

        base = Config.LLM_RETRY_BASE_DELAY
        # 4, 8, 16, 32… capped
        delay = min(base * (2 ** attempt), Config.LLM_RETRY_MAX_DELAY)
        # small jitter so parallel clients don't sync-retry
        delay += min(1.5, 0.25 * (attempt + 1))
        return delay

    def _format_http_error(self, response: requests.Response) -> str:
        body = ""
        try:
            body = response.text[:500]
        except Exception:
            pass
        remaining = response.headers.get("X-RateLimit-Remaining") or response.headers.get(
            "x-ratelimit-remaining"
        )
        extra = f" remaining={remaining}" if remaining is not None else ""
        return f"HTTP {response.status_code}{extra}: {body}"

    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = None,
        max_tokens: int = None,
    ) -> str:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/your-username/ai-coding-agent",
            "X-Title": "AI Coding Agent",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else Config.TEMPERATURE,
            "max_tokens": max_tokens or Config.MAX_TOKENS,
        }

        max_attempts = max(1, Config.LLM_MAX_RETRIES)
        last_err: Optional[Exception] = None

        for attempt in range(max_attempts):
            self._throttle()
            try:
                response = requests.post(
                    self.base_url,
                    headers=headers,
                    json=payload,
                    timeout=120,
                )
                self._last_call_at = time.time()

                if response.status_code in self._RETRY_STATUSES:
                    delay = self._retry_delay(response, attempt)
                    msg = self._format_http_error(response)
                    if attempt + 1 >= max_attempts:
                        raise requests.HTTPError(
                            f"{msg} (gave up after {max_attempts} attempts)",
                            response=response,
                        )
                    print(
                        f"  ⚠️ OpenRouter {response.status_code} (attempt "
                        f"{attempt + 1}/{max_attempts}). Waiting {delay:.1f}s then retrying…"
                    )
                    if response.status_code == 429:
                        print(
                            "     Tip: free/rate-limited keys hit 429 often. "
                            "Wait, switch OPENROUTER_MODEL, or raise LLM_RETRY_* in .env."
                        )
                    time.sleep(delay)
                    continue

                if not response.ok:
                    # Non-retryable client error (401, 402, 400, …)
                    raise requests.HTTPError(
                        self._format_http_error(response),
                        response=response,
                    )

                data = response.json()
                # OpenRouter error payload with 200 is rare but handle
                if "error" in data and "choices" not in data:
                    err = data["error"]
                    raise RuntimeError(f"OpenRouter error: {err}")

                content = data["choices"][0]["message"]["content"]
                if content is None:
                    # Some models put text in reasoning fields; surface clearly
                    raise RuntimeError(f"Empty model content. Raw: {str(data)[:400]}")
                return content

            except (requests.Timeout, requests.ConnectionError) as e:
                last_err = e
                if attempt + 1 >= max_attempts:
                    break
                delay = self._retry_delay(None, attempt)
                print(
                    f"  ⚠️ Network error ({e}). Retry {attempt + 1}/{max_attempts} "
                    f"in {delay:.1f}s…"
                )
                time.sleep(delay)
            except requests.HTTPError:
                raise
            except Exception as e:
                last_err = e
                raise

        raise RuntimeError(f"LLM request failed after {max_attempts} attempts: {last_err}")

    def chat_structured(
        self,
        messages: List[Dict[str, str]],
        temperature: float = None,
    ) -> Any:
        """Ask LLM for JSON and parse it safely (with repair)."""
        raw = self.chat(messages, temperature=temperature)
        return parse_llm_json(raw)


def strip_code_fences(text: str) -> str:
    """Remove ```json ... ``` wrappers if present."""
    if not text:
        return ""
    raw = text.strip()
    if "```json" in raw:
        raw = raw.split("```json", 1)[1]
        raw = raw.split("```", 1)[0].strip()
    elif raw.startswith("```"):
        raw = raw.split("```", 1)[1]
        raw = raw.split("```", 1)[0].strip()
    return raw.strip()


def extract_json_region(text: str) -> str:
    """Pick the most likely JSON array/object substring from model output."""
    raw = strip_code_fences(text)
    # Prefer array (plans) over object
    for opener, closer in (("[", "]"), ("{", "}")):
        start = raw.find(opener)
        if start == -1:
            continue
        # If truncated, take to end; repair will close brackets
        end = raw.rfind(closer)
        if end != -1 and end > start:
            return raw[start : end + 1]
        return raw[start:]
    return raw


def repair_json_text(s: str) -> str:
    """
    Best-effort repair for common LLM JSON failures:
    - trailing commas
    - truncated / unterminated strings
    - missing closing ] }
    """
    if not s:
        return s
    s = s.strip()
    # Remove trailing commas before } or ]
    s = re.sub(r",\s*([}\]])", r"\1", s)

    # If we're inside an unterminated string, close it
    # Track string state with simple scanner
    in_str = False
    escape = False
    stack = []  # [ or {
    for ch in s:
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "[{":
            stack.append(ch)
        elif ch == "]" and stack and stack[-1] == "[":
            stack.pop()
        elif ch == "}" and stack and stack[-1] == "{":
            stack.pop()

    if in_str:
        s += '"'
    # Close any open structures
    while stack:
        opener = stack.pop()
        s += "]" if opener == "[" else "}"

    # One more trailing-comma pass after closing
    s = re.sub(r",\s*([}\]])", r"\1", s)
    return s


def parse_llm_json(text: str) -> Any:
    """
    Parse JSON from an LLM response robustly.
    Raises json.JSONDecodeError / ValueError if unrecoverable.
    """
    region = extract_json_region(text)
    errors = []

    # 1) Direct parse
    try:
        return json.loads(region)
    except json.JSONDecodeError as e:
        errors.append(str(e))

    # 2) Prefer complete objects from a truncated array (before aggressive repair
    #    which can invent closed strings with incomplete code bodies)
    if region.lstrip().startswith("["):
        salvaged = _salvage_json_array(region)
        if salvaged:
            return salvaged

    # 3) Repair truncated / messy JSON then parse
    for candidate in (repair_json_text(region), repair_json_text(strip_code_fences(text))):
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as e:
            errors.append(str(e))
            continue

    raise json.JSONDecodeError(
        errors[-1] if errors else "Could not parse JSON from model output",
        region[:200],
        0,
    )


def _salvage_json_array(text: str) -> Optional[List[Any]]:
    """
    If the model truncated mid-array, keep complete top-level objects only.
    Useful when one huge "content" string is cut off.
    """
    start = text.find("[")
    if start == -1:
        return None
    body = text[start + 1 :]
    items = []
    depth = 0
    in_str = False
    escape = False
    obj_start = None
    for i, ch in enumerate(body):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and obj_start is not None:
                chunk = body[obj_start : i + 1]
                try:
                    items.append(json.loads(chunk))
                except json.JSONDecodeError:
                    # incomplete object — stop
                    break
                obj_start = None
        elif ch == "]" and depth == 0:
            break
    return items if items else None


def normalize_plan(plan: Any) -> List[Dict[str, Any]]:
    """Coerce common LLM plan shapes into List[dict]."""
    if isinstance(plan, dict):
        if "actions" in plan and isinstance(plan["actions"], list):
            plan = plan["actions"]
        elif "plan" in plan and isinstance(plan["plan"], list):
            plan = plan["plan"]
        else:
            plan = [plan]
    if not isinstance(plan, list):
        raise ValueError(f"Plan must be a JSON array, got {type(plan)}")
    out = []
    for action in plan:
        if not isinstance(action, dict):
            raise ValueError(f"Invalid plan action (not an object): {action}")
        if "file_path" not in action or "action" not in action:
            raise ValueError(f"Invalid plan action missing required fields: {action}")
        act = str(action["action"]).lower().strip()
        action["action"] = act
        if "description" not in action:
            action["description"] = f"{act} {action['file_path']}"
        # Normalize path separators
        action["file_path"] = str(action["file_path"]).replace("\\", "/").lstrip("./")
        out.append(action)
    return out


def read_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception as e:
        print(f"Error reading {path}: {e}")
        return ""


def write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _is_windows() -> bool:
    return platform.system() == "Windows"


def _npm_bin() -> str:
    """Resolve npm executable (npm.cmd on Windows)."""
    name = "npm.cmd" if _is_windows() else "npm"
    found = shutil.which(name) or shutil.which("npm")
    return found or name


def _node_bin() -> str:
    found = shutil.which("node")
    return found or "node"


def run_command(
    cmd: List[str],
    cwd: Path = None,
    timeout: int = 30,
    env: Optional[dict] = None,
) -> Tuple[int, str, str]:
    """
    Run a command safely cross-platform.

    Uses shell=False with a resolved executable list so Windows does not
    mangle npm/node invocations (shell=True + list was a common failure mode).
    """
    if not cmd:
        return -1, "", "Empty command"

    # Resolve well-known tools
    exe = cmd[0]
    if exe in ("npm", "npm.cmd"):
        cmd = [_npm_bin()] + list(cmd[1:])
    elif exe == "node":
        cmd = [_node_bin()] + list(cmd[1:])
    elif exe == "npx":
        npx = shutil.which("npx.cmd" if _is_windows() else "npx") or "npx"
        cmd = [npx] + list(cmd[1:])

    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            env=env,
        )
        return result.returncode, result.stdout or "", result.stderr or ""
    except FileNotFoundError as e:
        return -1, "", f"Command not found: {cmd[0]} ({e})"
    except subprocess.TimeoutExpired:
        return -1, "", f"Command timed out after {timeout}s: {' '.join(cmd)}"
    except Exception as e:
        return -1, "", str(e)


def _should_skip_path(path: Path) -> bool:
    return any(part in SKIP_DIRS for part in path.parts)


def grep_repo(root: Path, pattern: str, extensions: List[str] = None) -> List[Dict[str, Any]]:
    """Search the repo for a pattern (skips node_modules / .git / etc.)."""
    if extensions is None:
        extensions = [".js", ".ts", ".ejs", ".json", ".html", ".css", ".py", ".sql", ".md"]
    matches = []
    for ext in extensions:
        for f in root.rglob(f"*{ext}"):
            if _should_skip_path(f):
                continue
            try:
                content = f.read_text(encoding="utf-8", errors="ignore")
                for i, line in enumerate(content.splitlines(), 1):
                    if pattern.lower() in line.lower():
                        matches.append({
                            "file": str(f.relative_to(root)),
                            "line": i,
                            "text": line.strip(),
                        })
            except (PermissionError, OSError):
                # Common on Windows locked node_modules leftovers
                continue
            except Exception:
                continue
    return matches


def get_file_tree(root: Path) -> str:
    """Generate a concise file tree (first N lines), skipping heavy dirs."""
    lines = []
    for f in sorted(root.rglob("*")):
        if _should_skip_path(f):
            continue
        rel = f.relative_to(root)
        depth = len(rel.parts) - 1
        prefix = "  " * depth + ("└── " if depth > 0 else "")
        lines.append(f"{prefix}{rel.name}")
    return "\n".join(lines[:200])


def get_repo_metadata(root: Path) -> Dict[str, Any]:
    """Extract package.json / requirements.txt / README snippets."""
    meta = {}
    pkg = root / "package.json"
    if pkg.exists():
        meta["package_json"] = read_file(pkg)[:2000]
    req = root / "requirements.txt"
    if req.exists():
        meta["requirements_txt"] = read_file(req)[:1000]
    for name in ("README.md", "Readme.md", "readme.md"):
        readme = root / name
        if readme.exists():
            meta["readme"] = read_file(readme)[:1500]
            break
    return meta


def git_status(root: Path) -> str:
    rc, out, _ = run_command(["git", "status", "--short"], cwd=root)
    return out if rc == 0 else ""


def git_add_commit(root: Path, message: str) -> bool:
    run_command(["git", "add", "-A"], cwd=root)
    rc, _, err = run_command(["git", "commit", "-m", message], cwd=root)
    if rc != 0:
        print(f"  ⚠️ git commit skipped/failed: {err.strip() or 'no changes or not a git repo'}")
    return rc == 0


def syntax_check_js(path: Path) -> Tuple[bool, str]:
    """Run node --check on a JS file if node is available."""
    rc, out, err = run_command(["node", "--check", str(path)])
    return rc == 0, (err or out).strip()


def syntax_check_python(path: Path) -> Tuple[bool, str]:
    """Run python -m py_compile on a Python file."""
    rc, out, err = run_command(["python", "-m", "py_compile", str(path)])
    return rc == 0, (err or out).strip()


def is_port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_port(host: str, port: int, max_wait: float = 15.0, proc=None) -> Tuple[bool, str]:
    """
    Wait until port is open. If proc is given and exits early, return failure
    with captured output instead of waiting the full timeout.
    """
    deadline = time.time() + max_wait
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            output = _drain_process(proc)
            return False, f"Process exited early (code {proc.returncode}). Output:\n{output[:2000]}"
        if is_port_open(host, port):
            return True, ""
        time.sleep(0.4)
    output = _drain_process(proc) if proc is not None else ""
    extra = f"\nProcess output:\n{output[:2000]}" if output else ""
    return False, f"Timed out waiting for {host}:{port}.{extra}"


def _drain_process(proc) -> str:
    if proc is None:
        return ""
    try:
        # Non-blocking-ish: communicate with short timeout if still running
        if proc.poll() is not None:
            out, _ = proc.communicate(timeout=2)
        else:
            # Don't wait forever while still running
            return ""
        if isinstance(out, bytes):
            return out.decode("utf-8", errors="ignore")
        return out or ""
    except Exception:
        return ""


def _infer_port(root: Path, default: int = 3000) -> int:
    """Find listen(PORT) in source files only (never node_modules)."""
    candidates = [3000, 5000, 8080, 8000, 4000]
    patterns = [
        re.compile(r"\.listen\(\s*(\d{2,5})"),
        re.compile(r"PORT\s*[=:]\s*(\d{2,5})"),
        re.compile(r"port\s*[=:]\s*(\d{2,5})"),
    ]
    search_roots = [root]
    for sub in ("app", "src", "config", "server"):
        p = root / sub
        if p.is_dir():
            search_roots.append(p)

    seen = set()
    for base in search_roots:
        for f in base.rglob("*.js"):
            if _should_skip_path(f):
                continue
            key = str(f)
            if key in seen:
                continue
            seen.add(key)
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except (PermissionError, OSError):
                continue
            for pat in patterns:
                m = pat.search(text)
                if m:
                    return int(m.group(1))
    return default


def _detect_start_command(root: Path) -> Optional[List[str]]:
    """
    Prefer `node <entry>` over `npm start` to avoid Windows npm.cmd shell issues
    and unnecessary script layers.
    """
    pkg_path = root / "package.json"
    if pkg_path.exists():
        try:
            data = json.loads(read_file(pkg_path))
        except Exception:
            data = {}
        scripts = data.get("scripts") or {}
        main = data.get("main") or "server.js"

        # Prefer a real JS entry file
        for candidate in (main, "server.js", "app.js", "index.js"):
            if candidate and (root / candidate).exists():
                return ["node", candidate]

        if "start" in scripts:
            return [_npm_bin(), "start"]
        if "dev" in scripts:
            return [_npm_bin(), "run", "dev"]

    for candidate in ("server.js", "app.js", "index.js"):
        if (root / candidate).exists():
            return ["node", candidate]

    for candidate in ("app.py", "main.py", "run.py", "manage.py"):
        if (root / candidate).exists():
            return ["python", candidate]

    return None


def start_server(root: Path) -> Tuple[Optional[subprocess.Popen], Optional[int], str]:
    """
    Try to start the dev server.

    Returns (process, port, message).
    - process is None if we reused an already-open port, or if start failed.
    - On failure, message explains why (MongoDB missing, EPERM, etc.).
    """
    # Reuse already-running server
    for p in [3000, 5000, 8080, 8000, 4000]:
        if is_port_open("localhost", p):
            print(f"  ℹ️ Server already running on port {p}")
            return None, p, f"Reusing existing server on port {p}"

    cmd = _detect_start_command(root)
    if cmd is None:
        return None, None, "Could not detect how to start the server (no package.json start/main or known entry)."

    port = _infer_port(root, default=3000)
    env = os.environ.copy()
    env["PORT"] = str(port)

    print(f"  ▶ Starting server: {' '.join(cmd)} (expected port {port})")

    try:
        kwargs = {
            "cwd": str(root),
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "env": env,
            "shell": False,
        }
        if _is_windows():
            # Separate process group so we can kill the tree cleanly
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

        proc = subprocess.Popen(cmd, **kwargs)
    except Exception as e:
        return None, None, f"Failed to spawn server process: {e}"

    # Brief settle; catch immediate crashes (missing modules, EPERM on ipaddr.js, etc.)
    time.sleep(1.2)
    if proc.poll() is not None:
        output = _drain_process(proc)
        hint = _classify_startup_failure(output)
        print(f"  ❌ Server exited immediately (code {proc.returncode})")
        if output:
            print(f"  Output: {output[:800]}")
        return None, None, hint or f"Server exited immediately (code {proc.returncode}).\n{output[:1500]}"

    return proc, port, "Server process started"


def _classify_startup_failure(output: str) -> str:
    text = output or ""
    lower = text.lower()

    if "eperm" in lower or "permission denied" in lower:
        return (
            "INFRASTRUCTURE: Permission error while loading node modules "
            "(often node_modules\\ipaddr.js on Windows). "
            "This is not a code bug. Fix: delete node_modules and re-run npm install, "
            "or exclude the folder from antivirus.\n"
            f"Details:\n{text[:1200]}"
        )
    if "cannot find module" in lower or "err_module_not_found" in lower:
        return (
            "INFRASTRUCTURE: Missing Node dependency. Run npm install in the target repo.\n"
            f"Details:\n{text[:1200]}"
        )
    if "mongo" in lower or "econnrefused" in lower and "27017" in text:
        return (
            "INFRASTRUCTURE: MongoDB is not running (app exits before listen()). "
            "Start MongoDB on localhost:27017, or validation will skip live server checks.\n"
            f"Details:\n{text[:1200]}"
        )
    if "eaddrinuse" in lower:
        return (
            "INFRASTRUCTURE: Port already in use.\n"
            f"Details:\n{text[:1200]}"
        )
    return ""


def kill_server(proc) -> None:
    if proc is None:
        return
    try:
        if _is_windows():
            # Kill the whole process tree (node under our Popen)
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                text=True,
                timeout=10,
            )
        else:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def ensure_dependencies(root: Path) -> Tuple[bool, str]:
    """Install missing dependencies before starting the server."""
    if (root / "package.json").exists():
        nm = root / "node_modules"
        needs_install = (not nm.exists()) or (not any(nm.iterdir()))

        # Detect broken installs (EPERM / empty express)
        if not needs_install:
            broken, reason = _node_modules_looks_broken(root)
            if broken:
                print(f"  ⚠️ Broken node_modules detected ({reason}). Reinstalling...")
                needs_install = True
                try:
                    # Best-effort cleanup; Windows antivirus may lock files
                    shutil.rmtree(nm, ignore_errors=True)
                except Exception:
                    pass

        if needs_install:
            print("  ▶ Running npm install...")
            rc, out, err = run_command([_npm_bin(), "install"], cwd=root, timeout=180)
            if rc != 0:
                return False, f"npm install failed:\n{err}\n{out}"
            print("  ✓ npm install completed.")

            # Verify core requires work (catches EPERM on ipaddr.js early)
            ok, msg = verify_node_requires(root)
            if not ok:
                return False, msg

    if (root / "requirements.txt").exists():
        # Only install if this looks like a Python app target (not the agent itself)
        if any((root / c).exists() for c in ("app.py", "main.py", "manage.py", "wsgi.py")):
            print("  ▶ Ensuring Python deps from requirements.txt...")
            rc, out, err = run_command(
                ["python", "-m", "pip", "install", "-r", "requirements.txt"],
                cwd=root,
                timeout=180,
            )
            if rc != 0:
                return False, f"pip install failed:\n{err}\n{out}"

    return True, ""


def _node_modules_looks_broken(root: Path) -> Tuple[bool, str]:
    """Quick health check for common Windows EPERM / incomplete install cases."""
    express = root / "node_modules" / "express"
    if not express.exists():
        return True, "express package missing"

    # ipaddr.js is a common EPERM victim under proxy-addr
    ipaddr = root / "node_modules" / "ipaddr.js"
    if ipaddr.exists():
        try:
            # Try reading package entry
            pkg = ipaddr / "package.json"
            if pkg.exists():
                pkg.read_text(encoding="utf-8")
            # Try a real node require
            rc, out, err = run_command(
                ["node", "-e", "require('ipaddr.js'); require('express'); console.log('ok')"],
                cwd=root,
                timeout=15,
            )
            if rc != 0:
                combined = (out + err).lower()
                if "eperm" in combined or "permission denied" in combined:
                    return True, "EPERM reading ipaddr.js / express"
                if "cannot find module" in combined:
                    return True, "cannot require express/ipaddr.js"
        except PermissionError:
            return True, "PermissionError reading ipaddr.js"
        except OSError as e:
            return True, f"OSError: {e}"
    return False, ""


def verify_node_requires(root: Path) -> Tuple[bool, str]:
    """Ensure express (and friends) can be required — surfaces EPERM early."""
    if not (root / "package.json").exists():
        return True, ""
    rc, out, err = run_command(
        ["node", "-e", "require('express'); console.log('deps-ok')"],
        cwd=root,
        timeout=15,
    )
    if rc != 0:
        combined = out + err
        classified = _classify_startup_failure(combined)
        return False, classified or f"Node require check failed:\n{combined[:1200]}"
    return True, ""


def _is_placeholder_npm_test(script: str) -> bool:
    s = (script or "").lower()
    return (
        "no test specified" in s
        or s.strip() in ("", "echo ok")
        or ("echo" in s and "exit 1" in s)
    )


def run_tests(root: Path) -> Tuple[bool, str, bool]:
    """
    Run project tests if a real suite exists.

    Returns (passed, output, ran_real_tests).
    Placeholder npm test scripts (Easy Notes default) are skipped as success.
    """
    pkg = root / "package.json"
    if pkg.exists():
        try:
            data = json.loads(read_file(pkg))
            test_script = (data.get("scripts") or {}).get("test", "")
            if test_script and not _is_placeholder_npm_test(test_script):
                rc, out, err = run_command([_npm_bin(), "test"], cwd=root, timeout=90)
                return rc == 0, out + "\n" + err, True
            # Placeholder or missing — not a real failure
            print("  ℹ️ No real npm test suite (placeholder/missing). Skipping npm test.")
        except Exception as e:
            return True, f"Could not parse package.json tests: {e}", False

    # Python tests only if present
    if (root / "pytest.ini").exists() or (root / "tests").exists() or list(root.glob("test_*.py")):
        for cmd in (["pytest"], ["python", "-m", "pytest"]):
            rc, out, err = run_command(cmd, cwd=root, timeout=90)
            if rc == -1 and "not found" in (err or "").lower():
                continue
            return rc == 0, out + "\n" + err, True

    return True, "No test runner detected.", False


def smoke_http(port: int, path: str = "/") -> Tuple[bool, str]:
    """Hit the running server once to confirm it responds."""
    # Prefer /health when available (EasyNotes dev fallback), else root
    for p in (path, "/health", "/"):
        try:
            r = requests.get(f"http://127.0.0.1:{port}{p}", timeout=5)
            if r.status_code < 500:
                return True, f"HTTP {r.status_code} from :{port}{p}"
        except Exception:
            continue
    try:
        r = requests.get(f"http://127.0.0.1:{port}{path}", timeout=5)
        return True, f"HTTP {r.status_code} from :{port}{path}"
    except Exception as e:
        return False, str(e)
