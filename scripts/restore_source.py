"""Restore confirmed source archive into a NEW directory without runtime state.

Usage: python scripts/restore_source.py /path/to/new-directory
Baseline inspected 2026-10-18: selected data/rules/state tests: 32 passed.
No live feed, deployment or profitability validation implied.
"""
from pathlib import Path, PurePosixPath
import sys
import zipfile

ALLOWED_ROOTS = {"backend", "frontend", "tests"}
ALLOWED_FILES = {"README.md", "TEMPLATE.md", "memory/SPEC.md", "smoke_phase_f.sh", "smoke_phase_g.sh"}

def restore(archive: Path, destination: Path) -> int:
    if destination.exists():
        raise ValueError("Destination must not exist; never overwrite an existing checkout")
    pending = []
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            parts = PurePosixPath(info.filename).parts
            if not parts or parts[0] != "Theta-pluse-main":
                raise ValueError("Unexpected archive root")
            rel = PurePosixPath(*parts[1:])
            if not rel.parts or ".." in rel.parts or rel.is_absolute():
                raise ValueError("Unsafe archive path")
            if any(p.startswith(".") for p in rel.parts):
                continue
            if rel.parts[0] not in ALLOWED_ROOTS and str(rel) not in ALLOWED_FILES:
                continue
            if rel.suffix in {".db", ".sqlite", ".pyc"} or "__pycache__" in rel.parts:
                continue
            pending.append((rel, z.read(info)))
    destination.mkdir(parents=True, exist_ok=False)
    for rel, content in pending:
        target = destination.joinpath(*rel.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return len(pending)

if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python scripts/restore_source.py NEW_DIRECTORY")
    archive = Path(__file__).resolve().parents[1] / "Theta-pluse-main.zip"
    print(f"Restored {restore(archive, Path(sys.argv[1]))} files")
