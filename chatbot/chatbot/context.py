"""Read the explicitly selected local context directory."""

from pathlib import Path

SUFFIXES = {"", ".txt", ".md", ".markdown", ".yaml", ".yml", ".json"}


def read_context(directory: Path, max_bytes: int) -> str:
    directory = directory.resolve(strict=True)
    if not directory.is_dir():
        raise ValueError("--context 必须指向目录")
    parts = []
    total = 0
    for path in sorted(directory.rglob("*")):
        if any(part.startswith(".") for part in path.relative_to(directory).parts):
            continue
        if path.is_symlink() or not path.is_file() or path.suffix.lower() not in SUFFIXES:
            continue
        relative = path.relative_to(directory).as_posix()
        size = path.stat().st_size
        total += size
        if total > max_bytes:
            raise ValueError(f"上下文目录超过 {max_bytes} 字节")
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"上下文文件不是 UTF-8 文本: {relative}") from exc
        parts.append(f"### {relative}\n{content}")
    return "\n\n".join(parts)
