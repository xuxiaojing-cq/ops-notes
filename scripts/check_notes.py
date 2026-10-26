import subprocess
import sys
from pathlib import Path


def check_file(path):
    """返回文件的问题列表；空列表表示通过。"""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeError:
        return ["invalid UTF-8"]

    errors = []

    if not text.strip():
        errors.append("empty Markdown file")

    if "\0" in text:
        errors.append("contains a NUL character")

    return errors


def main():
    output = subprocess.check_output(
        ["git", "ls-files", "-z", "--", "*.md"]
    )
    files = [
        Path(name.decode("utf-8"))
        for name in output.split(b"\0")
        if name
    ]

    errors = []

    if Path("README.md") not in files:
        errors.append("README.md is missing from tracked files")

    for path in files:
        for error in check_file(path):
            errors.append(f"{path}: {error}")
        print(f"Checked: {path}")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print(f"PASS: checked {len(files)} Markdown files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
