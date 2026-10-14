#!/usr/bin/env python3
# template: product-owner @ 0.4.0 — shared; change it through Codery
"""SessionStart check: the detection half of the technology gate.

Reports every dependency named in package.json (dependencies, devDependencies,
optionalDependencies, peerDependencies, and the keys of pnpm.overrides),
pyproject.toml (project.dependencies, project.optional-dependencies,
dependency-groups, tool.poetry dependencies) and requirements*.txt that is not
recorded. A name counts as recorded only when it appears as an exact backticked
token in docs/stack.md or in a file under docs/adr/. Backticks keep prose from
shadowing a name ("next", "sharp" are ordinary words). Python names are compared
after PEP 503 normalisation (case, "-", "_" and "." alike).

It is a REPORT, not a gate: it never blocks a session, always exits 0, and says
nothing when there is no manifest to read. Output is plain prose on stdout (the
SessionStart channel the session sees), mirrored to stderr. Never JSON:
JSON-shaped stdout would be parsed as hook control output.

Known gaps, stated rather than implied: lockfile transitives are not checked
(each is covered by its direct parent in the record); a major version bump of an
already-recorded package is not detected (the technology gate covers bumps in
conversation); a name written without backticks is not seen as recorded, so
this can over-report, never hide a gap. pyproject.toml needs python3 3.11 or
newer (tomllib); on an older python3 the report says it was skipped.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

NPM_SECTIONS = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")
BACKTICKED = re.compile(r"`([^`\n]+)`")
PACKAGE_NAME = re.compile(r"^(@[a-z0-9][a-z0-9._~-]*/)?[A-Za-z0-9][A-Za-z0-9._~-]{0,213}$")
PY_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def project_root() -> Path | None:
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return Path(env)
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None


def norm_py(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def py_requirement_name(spec: object) -> str | None:
    if not isinstance(spec, str):
        return None
    m = PY_NAME.match(spec)
    return m.group(1) if m else None


def npm_names(root: Path) -> list[str]:
    try:
        manifest = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(manifest, dict):
        return []
    names: set[str] = set()
    for section in NPM_SECTIONS:
        block = manifest.get(section)
        if isinstance(block, dict):
            names.update(str(k) for k in block)
    pnpm = manifest.get("pnpm")
    overrides = pnpm.get("overrides") if isinstance(pnpm, dict) else None
    if isinstance(overrides, dict):
        names.update(str(k) for k in overrides)
    return sorted(names)


def pyproject_names(root: Path, notes: list[str]) -> list[str]:
    path = root / "pyproject.toml"
    if not path.is_file():
        return []
    try:
        import tomllib
    except ImportError:
        notes.append("pyproject.toml was not read: this python3 is older than 3.11 (no tomllib).")
        return []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    found: set[str] = set()

    def add_list(items: object) -> None:
        if isinstance(items, list):
            for spec in items:
                name = py_requirement_name(spec)
                if name:
                    found.add(name)

    project = data.get("project")
    if isinstance(project, dict):
        add_list(project.get("dependencies"))
        optional = project.get("optional-dependencies")
        if isinstance(optional, dict):
            for items in optional.values():
                add_list(items)
    groups = data.get("dependency-groups")
    if isinstance(groups, dict):
        for items in groups.values():
            add_list(items)
    poetry = data.get("tool", {}).get("poetry") if isinstance(data.get("tool"), dict) else None
    if isinstance(poetry, dict):
        for key in ("dependencies", "dev-dependencies"):
            block = poetry.get(key)
            if isinstance(block, dict):
                found.update(str(k) for k in block if str(k).lower() != "python")
        poetry_groups = poetry.get("group")
        if isinstance(poetry_groups, dict):
            for group in poetry_groups.values():
                block = group.get("dependencies") if isinstance(group, dict) else None
                if isinstance(block, dict):
                    found.update(str(k) for k in block if str(k).lower() != "python")
    return sorted(found)


def requirements_names(root: Path) -> list[str]:
    found: set[str] = set()
    for f in sorted(root.glob("requirements*.txt")):
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith("-") or "://" in line:
                continue
            name = py_requirement_name(line)
            if name:
                found.add(name)
    return sorted(found)


def recorded_tokens(root: Path) -> set[str]:
    files = [root / "docs" / "stack.md", *sorted((root / "docs" / "adr").glob("*.md"))]
    tokens: set[str] = set()
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        tokens.update(m.strip() for m in BACKTICKED.findall(text))
    return tokens


def main() -> None:
    root = project_root()
    if root is None:
        return
    notes: list[str] = []
    recorded = recorded_tokens(root)
    recorded_py = {norm_py(t) for t in recorded}

    missing: list[tuple[str, str]] = []
    for name in npm_names(root):
        if name not in recorded:
            missing.append((name, "package.json"))
    py_found = [(n, "pyproject.toml") for n in pyproject_names(root, notes)]
    py_found += [(n, "requirements*.txt") for n in requirements_names(root)]
    seen: set[str] = set()
    for name, source in py_found:
        key = norm_py(name)
        if key in recorded_py or key in seen:
            continue
        seen.add(key)
        missing.append((name, source))

    # A manifest is repo content, not trusted text: only names shaped like package
    # names are printed; anything else is counted, never echoed.
    valid = [(n, src) for n, src in missing if PACKAGE_NAME.fullmatch(n)]
    skipped = len(missing) - len(valid)
    missing = valid
    if not missing and not skipped and not notes:
        return
    lines: list[str] = []
    if missing:
        lines += [
            f"Dependency-record check: {len(missing)} dependency name(s) with no decision record:",
            *[f"  - {n} ({src})" for n, src in missing],
            "Each needs an ADR under docs/adr/ or an entry in docs/stack.md (technology gate in CLAUDE.md).",
            "A name counts only when it appears in backticks. This is a report, not a gate.",
        ]
    if skipped:
        lines.append(f"Dependency-record check: {skipped} {'entry' if skipped == 1 else 'entries'} skipped: not package names.")
    lines += [f"Dependency-record check note: {n}" for n in notes]
    report = "\n".join(lines) + "\n"
    sys.stdout.write(report)
    sys.stderr.write(report)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
