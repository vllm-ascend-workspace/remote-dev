#!/usr/bin/env python3
from __future__ import annotations

import shutil
import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
AGENTS_SKILLS = ROOT / ".agents" / "skills"
CLAUDE_SKILLS = ROOT / ".claude" / "skills"


def expected_skill_body(skill_dir: Path) -> str:
    source = skill_dir / "SKILL.md"
    body = source.read_text(encoding="utf-8")
    banner = (
        "<!-- Generated from .agents/skills/"
        + skill_dir.name
        + "/SKILL.md by .remote-dev/tools/sync_claude_skills.py. -->\n\n"
    )
    return banner + body


def source_skill_dirs() -> list[Path]:
    return sorted(path for path in AGENTS_SKILLS.iterdir() if path.is_dir() and (path / "SKILL.md").exists())


def check_mirror() -> list[str]:
    errors: list[str] = []
    expected_names = {path.name for path in source_skill_dirs()}
    observed_names = {path.name for path in CLAUDE_SKILLS.iterdir() if path.is_dir()} if CLAUDE_SKILLS.exists() else set()
    for missing in sorted(expected_names - observed_names):
        errors.append(f"missing Claude skill mirror: {missing}")
    for extra in sorted(observed_names - expected_names):
        errors.append(f"extra Claude skill mirror: {extra}")
    for skill_dir in source_skill_dirs():
        target = CLAUDE_SKILLS / skill_dir.name / "SKILL.md"
        if not target.exists():
            continue
        expected = expected_skill_body(skill_dir)
        observed = target.read_text(encoding="utf-8")
        if observed != expected:
            errors.append(f"stale Claude skill mirror: {skill_dir.name}")
    return errors


def sync_mirror() -> None:
    CLAUDE_SKILLS.mkdir(parents=True, exist_ok=True)
    for skill_dir in source_skill_dirs():
        target_dir = CLAUDE_SKILLS / skill_dir.name
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "SKILL.md"
        target.write_text(expected_skill_body(skill_dir), encoding="utf-8")
    for existing in CLAUDE_SKILLS.iterdir():
        if existing.is_dir() and not (AGENTS_SKILLS / existing.name / "SKILL.md").exists():
            shutil.rmtree(existing)


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync generated Claude Code skill mirrors from .agents/skills.")
    parser.add_argument("--check", action="store_true", help="Only verify that .claude/skills is synchronized.")
    args = parser.parse_args()
    if args.check:
        errors = check_mirror()
        for error in errors:
            print(error)
        return 1 if errors else 0
    sync_mirror()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
