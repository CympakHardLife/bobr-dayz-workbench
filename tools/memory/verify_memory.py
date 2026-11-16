"""Check portable memory references and checkpoint bytes; no server access."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
from checkpoint_hook import FILES, checked_text
from context_router import checked_path, load_routes

def verify(root):
    root = root.resolve(strict=True)
    sizes = {}
    for relative, limit in {"AGENTS.md": 10240, ".ai-rules.md": 2304, "memory/CURRENT.md": 4096}.items():
        raw = checked_text(root / relative)
        assert len(raw) <= limit, (relative, "startup_budget_exceeded")
        sizes[relative] = len(raw)
    topics = load_routes(root)
    refs = {r for topic in topics.values() for r in [topic["summary"], *topic["sources"]]}
    for relative in refs:
        assert checked_path(root, relative).is_file(), relative
    for p in (root / "tools/memory").glob("*.py"):
        ast.parse(p.read_text("utf-8-sig"), filename=str(p))
    state = json.loads((root / "memory/checkpoints/state.json").read_text("utf-8"))
    snapshot = checked_path(root, "memory/checkpoints/" + state["snapshot"] + "/manifest.json").parent
    manifest = json.loads((snapshot / "manifest.json").read_text("utf-8"))
    assert set(manifest["sha256"]) == set(FILES), "snapshot_allowlist_mismatch"
    for relative, expected in manifest["sha256"].items():
        data = (snapshot / relative).read_bytes()
        assert hashlib.sha256(data).hexdigest() == expected, relative
        assert data == (root / relative).read_bytes(), (relative, "checkpoint_is_stale")
    return {"result": "passed", "topics": len(topics), "registered_sources": len(refs),
            "snapshot_files": len(FILES), "startup_sizes": sizes,
            "lifecycle_hooks_active": False, "notice": "Manual tools verified; automatic runtime integration not tested."}

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    print(json.dumps(verify(parser.parse_args().root), ensure_ascii=False, indent=2))
