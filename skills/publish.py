#!/usr/bin/env python3
"""
Generate publishable README for Sutando skills.
Creates a standalone package that anyone can install into Claude Code.

Usage:
  python3 skills/publish.py macos-tools    # generate README for one skill
  python3 skills/publish.py --all          # generate for all skills
"""

import sys
from pathlib import Path

SKILLS_DIR = Path(__file__).parent


def generate_readme(skill_dir: Path) -> str:
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return ""

    content = skill_md.read_text()

    # Count scripts
    scripts_dir = skill_dir / "scripts"
    scripts = list(scripts_dir.glob("*")) if scripts_dir.exists() else []

    readme = f"""# {skill_dir.name}

A Claude Code skill for AI agents.

## Install

```bash
# Clone and install
git clone https://github.com/liususan091219/sutando.git
cd sutando
bash skills/install.sh
```

Or manually:
```bash
ln -s /path/to/sutando/skills/{skill_dir.name} ~/.claude/skills/{skill_dir.name}
```

## What's included

{len(scripts)} scripts:
"""
    for s in scripts:
        readme += f"- `{s.name}` — {s.stem.replace('-', ' ').replace('_', ' ')}\n"

    readme += f"""
## Usage

{content.split('## When to Use')[1].split('##')[0].strip() if '## When to Use' in content else 'See SKILL.md for details.'}

## Requirements

- macOS (uses AppleScript for system integrations)
- Claude Code installed

## License

MIT

---

Built by [Sutando](https://github.com/liususan091219/sutando) — a personal AI agent platform.
"""
    return readme


def main():
    if "--all" in sys.argv:
        skills = [d for d in SKILLS_DIR.iterdir() if d.is_dir() and (d / "SKILL.md").exists()]
    elif len(sys.argv) > 1:
        skill_name = sys.argv[1]
        skill_path = SKILLS_DIR / skill_name
        if not skill_path.exists():
            print(f"Skill not found: {skill_name}")
            sys.exit(1)
        skills = [skill_path]
    else:
        print("Usage: python3 skills/publish.py [skill-name|--all]")
        sys.exit(1)

    for skill in skills:
        readme = generate_readme(skill)
        if readme:
            output = skill / "README.md"
            output.write_text(readme)
            print(f"Generated: {output}")


if __name__ == "__main__":
    main()
