#!/usr/bin/env python3
"""Static checks for the zero-build Local Video Studio frontend.

Node is not assumed, so the whole suite runs on python3 (stdlib only):

  1. JS structural balance - comments/strings/regex-literals stripped, then
     (, {, [ and backtick parity for every file under js/.
  2. Import resolution     - every named import resolves to a named export of
     the target module.
  3. Route wiring          - every router.js route name has a SCREENS entry in
     app.js (and vice versa); every sidebar nav hash matches a router regex.
  4. Security              - no LLM key name in the frontend; no 0.0.0.0;
     storage keys restricted to the two known non-sensitive ones; no external
     http(s) hosts; no telemetry APIs.
  5. Structure             - no package/lock/build artifacts; index.html loads
     the module entry and every referenced local asset exists.

Exit code 0 = all checks pass.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

FRONTEND = Path(__file__).resolve().parent.parent
SELF = Path(__file__).resolve()
FAILURES: list[str] = []


def fail(msg: str) -> None:
    FAILURES.append(msg)


def js_files() -> list[Path]:
    return sorted(FRONTEND.glob("js/**/*.js"))


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def rel(p: Path) -> str:
    return str(p.relative_to(FRONTEND))


# ---------------------------------------------------------------------------
# 1. balance
# ---------------------------------------------------------------------------

def strip_js(src: str) -> str:
    """Drop comments, string literals and regex literals so only structural
    characters (parentheses, braces, brackets, backticks) remain.

    A '/' is treated as the start of a regex literal when the previous
    significant character cannot end a value (i.e. it is not an identifier
    char, a digit, ')', ']', or a closing quote/backtick); otherwise it is
    division. This is the standard heuristic and is exact for this codebase.
    """
    out: list[str] = []
    i, n = 0, len(src)

    def last_sig() -> str:
        for ch in reversed(out):
            if not ch.isspace():
                return ch
        return ""

    while i < n:
        c = src[i]
        if c in "\"'`":
            q = c
            i += 1
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == q:
                    i += 1
                    break
                i += 1
            out.append(q + q)  # placeholder keeps quote pairing stable
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            i += 2
            while i + 1 < n and not (src[i] == "*" and src[i + 1] == "/"):
                i += 1
            i += 2
            continue
        if c == "/" and last_sig() not in "abcdefghijklmnopqrstuvwxyz" \
                               "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_$)\"']`":
            i += 1  # opening slash
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == "/":
                    i += 1
                    break
                if src[i] == "\n":  # not a regex after all; bail out
                    break
                i += 1
            while i < n and src[i].isalpha():  # flags
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def check_balance() -> None:
    for p in js_files():
        s = strip_js(read(p))
        pairs = {
            "()": (s.count("("), s.count(")")),
            "{}": (s.count("{"), s.count("}")),
            "[]": (s.count("["), s.count("]")),
        }
        if any(a != b for a, b in pairs.values()):
            fail(f"balance: {rel(p)} unbalanced {dict(pairs)}")
        if s.count("`") % 2:
            fail(f"balance: {rel(p)} odd backtick count {s.count(chr(96))}")


# ---------------------------------------------------------------------------
# 2. imports
# ---------------------------------------------------------------------------

IMPORT_RE = re.compile(r"import\s*\{([^}]+)\}\s*from\s*['\"]([^'\"]+)['\"]")


def exports_of(src: str) -> set[str]:
    names: set[str] = set()
    for m in re.finditer(r"export\s+(?:async\s+)?function\s+(\w+)", src):
        names.add(m.group(1))
    for m in re.finditer(r"export\s+(?:const|let|class)\s+(\w+)", src):
        names.add(m.group(1))
    for m in re.finditer(r"export\s*\{([^}]+)\}", src):
        for part in m.group(1).split(","):
            part = part.split(" as ")[0].strip()
            if part:
                names.add(part)
    return names


def check_imports() -> None:
    for p in js_files():
        for m in IMPORT_RE.finditer(read(p)):
            names, mod = m.group(1), m.group(2)
            target = (p.parent / mod).resolve()
            if not target.exists():
                fail(f"imports: {rel(p)} -> missing module {mod}")
                continue
            exp = exports_of(read(target))
            for part in names.split(","):
                name = part.split(" as ")[0].strip()
                if name and name not in exp:
                    fail(f"imports: {rel(p)} -> '{name}' not exported by {rel(target)}")


# ---------------------------------------------------------------------------
# 3. routes
# ---------------------------------------------------------------------------

def section(src: str, header: str, end: str = "];") -> str:
    start = src.find(header)
    if start < 0:
        fail(f"routes: {header!r} not found in app.js/router.js")
        return ""
    i = src.index(end, start)
    return src[start:i + len(end)]


def check_routes() -> None:
    router = read(FRONTEND / "js" / "router.js")
    app = read(FRONTEND / "js" / "app.js")

    routes_table = section(router, "const ROUTES = [")
    route_names = re.findall(r'name:\s*"([\w-]+)"', routes_table)
    route_res = [re.sub(r"\\(.)", r"\1", m) for m in
                 re.findall(r"re:\s*/((?:[^/\\]|\\.)*)/", routes_table)]
    if len(route_names) != len(route_res):
        fail(f"routes: ROUTES table parse mismatch ({len(route_names)} names, "
             f"{len(route_res)} regexes)")

    screens_block = section(app, "const SCREENS = {", "};")
    screen_names = {a or b for a, b in re.findall(
        r'^\s{2}(?:"([\w-]+)"|([\w-]+)):', screens_block, re.M)}

    for name in route_names:
        if name not in screen_names:
            fail(f"routes: router name '{name}' has no SCREENS entry in app.js")
    for name in screen_names:
        if name not in route_names:
            fail(f"routes: SCREENS entry '{name}' has no matching router route")

    for h in re.findall(r'hash:\s*"(#[^"]+)"', app):
        if not any(re.fullmatch(rx, h) for rx in route_res):
            fail(f"routes: nav hash {h} matches no router regex")


# ---------------------------------------------------------------------------
# 4. security
# ---------------------------------------------------------------------------

ALLOWED_STORAGE_KEYS = {"lvs-current-project", "lvs-nav-collapsed"}
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "www.w3.org"}  # last: SVG namespace
STORAGE_CALL_RE = re.compile(
    r"(?:localStorage|sessionStorage)\.(?:set|get|remove)Item\(\s*([^,)]+)")
CONST_STR_RE = re.compile(r"const\s+(\w+)\s*=\s*\"([^\"]+)\"")
URL_HOST_RE = re.compile(r"https?://([A-Za-z0-9$_.-]+)")
TELEMETRY_PATTERNS = [re.compile(p) for p in (
    r"sendBeacon", r"\bga\s*\(", r"\bgtag\s*\(", r"\banalytics\s*\.",
    r"hotjar", r"clarity", r"mixpanel", r"amplitude",
)]


def text_files() -> list[Path]:
    files = sorted(p for p in FRONTEND.rglob("*")
                   if p.is_file() and p.suffix in {".js", ".css", ".html",
                                                   ".json", ".md", ".svg",
                                                   ".py"})
    return files


def check_security() -> None:
    for p in text_files():
        src = read(p)
        # This file necessarily names the forbidden patterns (as the
        # literals it detects), so it is exempt from those two assertions.
        if p != SELF:
            if "LOCAL_LLM_API_KEY" in src:
                fail(f"security: {rel(p)} mentions LOCAL_LLM_API_KEY")
            if "0.0.0.0" in src:
                fail(f"security: {rel(p)} references 0.0.0.0")
        if p.suffix != ".js":
            continue
        const_strs = dict(CONST_STR_RE.findall(src))
        for key_arg in STORAGE_CALL_RE.findall(src):
            key_arg = key_arg.strip()
            if key_arg.startswith("\"") and key_arg.endswith("\""):
                key = key_arg[1:-1]
            else:
                key = const_strs.get(key_arg, key_arg)
            if key not in ALLOWED_STORAGE_KEYS:
                fail(f"security: {rel(p)} uses unexpected storage key {key!r}")
        for pat in TELEMETRY_PATTERNS:
            if pat.search(src):
                fail(f"security: {rel(p)} contains telemetry pattern {pat.pattern!r}")
        # URL hosts: only localhost-ish hosts are allowed in code and markup;
        # comment lines are skipped (doc examples of 127.0.0.1 are fine either
        # way, but code must not point elsewhere).
        code_lines = [ln for ln in src.splitlines()
                      if not ln.strip().startswith(("//", "*", "/*"))]
        for m in URL_HOST_RE.finditer("\n".join(code_lines)):
            host = m.group(1)
            if host.startswith("$") or host in ALLOWED_HOSTS:
                continue
            fail(f"security: {rel(p)} references external host {host!r}")


# ---------------------------------------------------------------------------
# 5. structure
# ---------------------------------------------------------------------------

FORBIDDEN = ("package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
             "node_modules", "vite.config.js", "tsconfig.json", ".npmrc",
             "rollup.config.js", "webpack.config.js")


def check_structure() -> None:
    for name in FORBIDDEN:
        if (FRONTEND / name).exists():
            fail(f"structure: forbidden build artifact present: {name}")
    html = read(FRONTEND / "index.html")
    if '<script type="module" src="js/app.js">' not in html:
        fail("structure: index.html does not load js/app.js as a module")
    for m in re.finditer(r'(?:href|src)="([^"]+\.(?:css|svg|js))"', html):
        ref = m.group(1)
        if not (FRONTEND / ref).exists():
            fail(f"structure: index.html references missing file {ref}")
    for p in FRONTEND.glob("css/*.css"):
        for m in re.finditer(r"url\(\s*['\"]?([^'\")]+\.(?:svg|png|woff2?))",
                             read(p)):
            if not (p.parent / m.group(1)).resolve().exists():
                fail(f"structure: {rel(p)} references missing asset {m.group(1)}")


# ---------------------------------------------------------------------------

def main() -> int:
    check_balance()
    check_imports()
    check_routes()
    check_security()
    check_structure()
    if FAILURES:
        print(f"FAIL: {len(FAILURES)} problem(s)")
        for f in FAILURES:
            print("  -", f)
        return 1
    print(f"OK: all static checks passed "
          f"({len(js_files())} JS files, {len(text_files())} files scanned)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
