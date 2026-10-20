"""Offline file-link check for the maintained documentation entry points.

Historical research is deliberately not treated as current setup instructions.
External URLs and fragment anchors are not fetched/validated by this tool.
"""
import argparse
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
GUIDES = (
    "README.md", "CONTRIBUTING.md", "docs/README.md", "docs/architecture.md",
    "docs/development.md", "docs/repository-maintenance.md",
    "docs/releases/README.md", "docs/archive/README.md", "tools/README.md",
    "scripts/README.md", "docs/screenshots/README.md",
)
LINK = re.compile(r"!?\[[^\]\n]*\]\(\s*(<[^>\n]+>|[^\s)]+)(?:\s+[^)\n]+)?\)")
HTML_SOURCE = re.compile(r"\b(?:src|href)=[\"']([^\"']+)[\"']")


def targets(markdown):
    markdown = re.sub(r"(?ms)^(```|~~~)[^\n]*\n.*?^\1[ \t]*$", "", markdown)
    markdown = re.sub(r"`[^`\n]*`", "", markdown)
    return [m.group(1).strip("<>") for m in LINK.finditer(markdown)] + HTML_SOURCE.findall(markdown)


def check_links(root=ROOT, guides=GUIDES):
    root = Path(root).resolve()
    problems = []
    checked = 0
    for name in guides:
        document = (root / name).resolve()
        if not document.is_relative_to(root) or not document.is_file():
            problems.append(f"Missing or out-of-root document: {name}")
            continue
        for target in targets(document.read_text(encoding="utf-8")):
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            destination = (document.parent / unquote(parsed.path)).resolve()
            checked += 1
            if not destination.is_relative_to(root):
                problems.append(f"{name}: link escapes repository: {target}")
            elif not destination.exists():
                problems.append(f"{name}: missing local target: {target}")
    return checked, problems


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", help="Repository-relative Markdown files")
    args = parser.parse_args()
    checked, problems = check_links(guides=args.paths or GUIDES)
    if problems:
        parser.exit(1, "\n".join(problems) + "\n")
    print(f"Documentation links: {checked} local targets checked; 0 problems (offline)")


if __name__ == "__main__":
    main()
