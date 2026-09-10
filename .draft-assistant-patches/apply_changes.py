from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
PATCH_DIR = ROOT / ".draft-assistant-patches"


def parse_patch(path: Path):
    text = path.read_text(encoding="utf-8")
    target = None
    hunks = []
    current = None

    for line in text.splitlines(keepends=True):
        if line.startswith("+++ b/"):
            target = line[len("+++ b/"):].strip()
        elif line.startswith("@@"):
            if current is not None:
                hunks.append(current)
            current = []
        elif current is not None:
            current.append(line)

    if current is not None:
        hunks.append(current)
    if not target:
        raise RuntimeError(f"No target file found in {path.name}")
    return target, hunks


def hunk_strings(lines):
    old_parts = []
    new_parts = []
    for line in lines:
        if line.startswith("\\ No newline"):
            continue
        if not line:
            continue
        marker = line[0]
        body = line[1:]
        if marker in (" ", "-"):
            old_parts.append(body)
        if marker in (" ", "+"):
            new_parts.append(body)
    return "".join(old_parts), "".join(new_parts)


def apply_patch(path: Path):
    target_name, hunks = parse_patch(path)
    target = ROOT / target_name
    original_exists = target.exists()
    content = target.read_text(encoding="utf-8") if original_exists else ""

    for index, hunk in enumerate(hunks, start=1):
        old, new = hunk_strings(hunk)
        if not old:
            if original_exists or content:
                raise RuntimeError(
                    f"{path.name} hunk {index}: expected a new/empty file, but {target_name} already has content"
                )
            content = new
            continue

        count = content.count(old)
        if count != 1:
            raise RuntimeError(
                f"{path.name} hunk {index}: expected exactly one match in {target_name}, found {count}"
            )
        content = content.replace(old, new, 1)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    print(f"Applied {path.name} -> {target_name}")


def main():
    patch_files = sorted(
        p for p in PATCH_DIR.glob("*.patch") if p.name != "00-unused.patch"
    )
    if not patch_files:
        raise RuntimeError("No staged patches found")

    for patch in patch_files:
        apply_patch(patch)


if __name__ == "__main__":
    main()
