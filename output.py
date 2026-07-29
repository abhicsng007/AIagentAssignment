import difflib
import os
import shutil
from colorama import Fore, Back, Style, init

# Windows: colorama converts standard 16-color ANSI (Fore/Back.*) to Win32 APIs.
# Truecolor \x1b[48;2;… does NOT convert and often shows as plain text — so we
# use colorama Back.LIGHTGREEN_EX / LIGHTRED_EX by default.
init(autoreset=False, convert=True, strip=False)

_RESET = Style.RESET_ALL

# ── Diff colors (colorama — reliably green/red on Windows terminals) ────────
_ADD_BG = Back.LIGHTGREEN_EX
_ADD_FG = Fore.BLACK
_DEL_BG = Back.LIGHTRED_EX
_DEL_FG = Fore.WHITE
_CTX_FG = Fore.WHITE
_LN_FG = Fore.LIGHTBLACK_EX
_MARKER_ADD = Fore.GREEN
_MARKER_DEL = Fore.RED
_META_FG = Fore.MAGENTA
_HUNK_FG = Fore.YELLOW
_BOX = Fore.CYAN
_HEADER_FG = Fore.LIGHTBLACK_EX

# Optional: softer truecolor washes (Windows Terminal / VS Code). Opt-in only.
if os.getenv("DIFF_TRUECOLOR", "").lower() in ("1", "true", "yes"):
    def _bg(r, g, b):
        return f"\x1b[48;2;{r};{g};{b}m"

    def _fg(r, g, b):
        return f"\x1b[38;2;{r};{g};{b}m"

    _ADD_BG, _ADD_FG = _bg(180, 235, 195), _fg(10, 70, 30)
    _DEL_BG, _DEL_FG = _bg(255, 190, 195), _fg(120, 20, 20)
    _CTX_FG = _fg(200, 200, 200)
    _LN_FG = _fg(130, 130, 140)
    _HEADER_FG = _fg(150, 155, 165)


def print_header(msg: str):
    print(f"\n{Fore.CYAN}{Style.BRIGHT}{'=' * 60}{_RESET}")
    print(f"{Fore.CYAN}{Style.BRIGHT}{msg}{_RESET}")
    print(f"{Fore.CYAN}{Style.BRIGHT}{'=' * 60}{_RESET}")


def print_step(msg: str):
    print(f"{Fore.GREEN}{Style.BRIGHT}▶ {msg}{_RESET}")


def print_file_read(path: str):
    print(f"  {Fore.BLUE}📄 Reading {path}{_RESET}")


def print_plan_action(action: dict):
    act = action["action"]
    path = action["file_path"]
    desc = action.get("description", "No description provided")

    if act == "create":
        color = Fore.GREEN
        symbol = "➕"
    elif act == "update":
        color = Fore.YELLOW
        symbol = "✏️"
    elif act == "delete":
        color = Fore.RED
        symbol = "➖"
    else:
        color = Fore.WHITE
        symbol = "❓"

    print(f"  {color}{symbol} {act.upper()} {path}{_RESET}")
    print(f"     {Fore.WHITE}→ {desc}{_RESET}")


def _term_width(default: int = 100) -> int:
    try:
        return max(60, shutil.get_terminal_size(fallback=(default, 24)).columns)
    except Exception:
        return default


def _clip(text: str, width: int) -> str:
    text = text.replace("\t", "    ").replace("\r", "")
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return "…"
    return text[: width - 1] + "…"


def _paint_code(kind: str, marker: str, code: str, code_w: int) -> str:
    """
    Paint ONLY the code cell. Marker + source padded to exactly code_w chars
    so the background never spills into line-number columns.
    """
    body = _clip(marker + code, code_w).ljust(code_w)

    if kind == "add":
        # Bright green background + black text (clearly visible)
        return f"{_ADD_BG}{_ADD_FG}{Style.BRIGHT}{body}{_RESET}"
    if kind == "del":
        # Bright red/pink background + white text
        return f"{_DEL_BG}{_DEL_FG}{Style.BRIGHT}{body}{_RESET}"
    return f"{_CTX_FG}{body}{_RESET}"


def print_diff(old_content: str, new_content: str, file_path: str, context: int = 3):
    """
    Fixed-column diff:

        │ old │ new │ code…                          │
        │   5 │     │<green bg>-deleted…</green>     │
        │     │   8 │<red bg>  +added…  </red>       │
    """
    old_lines = old_content.splitlines()
    new_lines = new_content.splitlines()

    if old_lines == new_lines:
        print(f"  {Fore.YELLOW}(no changes detected){_RESET}")
        return

    rows = []
    sm = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            block_len = i2 - i1
            if block_len <= context * 2:
                for k in range(block_len):
                    rows.append(("ctx", i1 + k + 1, j1 + k + 1, old_lines[i1 + k]))
            else:
                for k in range(context):
                    rows.append(("ctx", i1 + k + 1, j1 + k + 1, old_lines[i1 + k]))
                skipped = block_len - context * 2
                rows.append(("hunk", None, None, f"··· {skipped} unchanged lines ···"))
                for k in range(block_len - context, block_len):
                    rows.append(("ctx", i1 + k + 1, j1 + k + 1, old_lines[i1 + k]))
        elif tag == "delete":
            for i in range(i1, i2):
                rows.append(("del", i + 1, None, old_lines[i]))
        elif tag == "insert":
            for j in range(j1, j2):
                rows.append(("add", None, j + 1, new_lines[j]))
        elif tag == "replace":
            for i in range(i1, i2):
                rows.append(("del", i + 1, None, old_lines[i]))
            for j in range(j1, j2):
                rows.append(("add", None, j + 1, new_lines[j]))

    max_old = max((r[1] or 0 for r in rows), default=1)
    max_new = max((r[2] or 0 for r in rows), default=1)
    ln_w = max(4, len(str(max(max_old, max_new))))

    # Layout: " " + OLD + " │ " + NEW + " │ " + CODE
    term = _term_width()
    available = term - 8
    chrome = 1 + ln_w + 3 + ln_w + 3
    code_w = max(24, min(100, available - chrome))
    inner_w = chrome + code_w

    def ln_cell(n) -> str:
        if n is None:
            return " " * ln_w
        return str(n).rjust(ln_w)

    def gutter(old_ln, new_ln) -> str:
        return f" {ln_cell(old_ln)} │ {ln_cell(new_ln)} │ "

    bar = "─" * inner_w
    print(f"  {_BOX}┌{bar}┐{_RESET}")

    title = _clip(f" diff  a/{file_path} → b/{file_path}", inner_w - 1).ljust(inner_w - 1)
    print(f"  {_BOX}│{_RESET} {_HEADER_FG}{title}{_RESET}{_BOX}│{_RESET}")
    print(f"  {_BOX}├{bar}┤{_RESET}")

    hdr = f" {'old'.rjust(ln_w)} │ {'new'.rjust(ln_w)} │ {'code'.ljust(code_w)}"
    hdr = hdr[:inner_w].ljust(inner_w)
    print(f"  {_BOX}│{_RESET}{_LN_FG}{hdr}{_RESET}{_BOX}│{_RESET}")
    print(f"  {_BOX}├{bar}┤{_RESET}")

    for kind, old_ln, new_ln, text in rows:
        if kind == "hunk":
            label = _clip(f" {text}", inner_w).ljust(inner_w)
            print(f"  {_BOX}│{_RESET}{_HUNK_FG}{label}{_RESET}{_BOX}│{_RESET}")
            continue

        g = gutter(old_ln, new_ln)
        if kind == "del":
            code = _paint_code("del", "-", text, code_w)
        elif kind == "add":
            code = _paint_code("add", "+", text, code_w)
        else:
            code = _paint_code("ctx", " ", text, code_w)

        # Numbers uncolored & fixed-width; only code cell has green/red bg
        print(
            f"  {_BOX}│{_RESET}"
            f"{_LN_FG}{g}{_RESET}"
            f"{code}"
            f"{_BOX}│{_RESET}"
        )

    print(f"  {_BOX}└{bar}┘{_RESET}")

    n_add = sum(1 for r in rows if r[0] == "add")
    n_del = sum(1 for r in rows if r[0] == "del")
    print(
        f"  {_LN_FG}Δ {file_path}: "
        f"{_ADD_BG}{_ADD_FG} +{n_add} {_RESET} "
        f"{_DEL_BG}{_DEL_FG} -{n_del} {_RESET}"
        f"{_LN_FG} lines{_RESET}"
    )


def print_create_preview(content: str, file_path: str, max_lines: int = 80):
    """New file preview — all lines on light-green background."""
    lines = content.splitlines() or [""]
    ln_w = max(4, len(str(len(lines))))
    term = _term_width()
    chrome = 1 + ln_w + 3
    code_w = max(24, min(100, term - 8 - chrome))
    inner_w = chrome + code_w
    bar = "─" * inner_w

    print(f"  {_BOX}┌{bar}┐{_RESET}")
    title = _clip(f" new file  {file_path}", inner_w - 1).ljust(inner_w - 1)
    print(f"  {_BOX}│{_RESET} {_HEADER_FG}{title}{_RESET}{_BOX}│{_RESET}")
    print(f"  {_BOX}├{bar}┤{_RESET}")

    hdr = f" {'new'.rjust(ln_w)} │ {'code'.ljust(code_w)}"
    hdr = hdr[:inner_w].ljust(inner_w)
    print(f"  {_BOX}│{_RESET}{_LN_FG}{hdr}{_RESET}{_BOX}│{_RESET}")
    print(f"  {_BOX}├{bar}┤{_RESET}")

    for i, text in enumerate(lines[:max_lines], 1):
        g = f" {str(i).rjust(ln_w)} │ "
        code = _paint_code("add", "+", text, code_w)
        print(
            f"  {_BOX}│{_RESET}"
            f"{_LN_FG}{g}{_RESET}"
            f"{code}"
            f"{_BOX}│{_RESET}"
        )

    if len(lines) > max_lines:
        more = _clip(f" … ({len(lines) - max_lines} more lines)", inner_w).ljust(inner_w)
        print(f"  {_BOX}│{_RESET}{_LN_FG}{more}{_RESET}{_BOX}│{_RESET}")

    print(f"  {_BOX}└{bar}┘{_RESET}")
    print(
        f"  {_ADD_BG}{_ADD_FG} +{len(lines)} {_RESET}"
        f"{_LN_FG} lines created{_RESET}"
    )
