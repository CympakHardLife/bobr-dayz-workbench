"""Import a pinned, verified English wiki selection as external data, never code.

    python tools/knowledge/import_wiki.py --archive wiki.zip --target <new-root>
    python tools/knowledge/import_wiki.py --download --target <new-root>

The target must not exist. Its parent must exist. No upstream command is run.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/core"))
import engine_library_index as library

SPEC_PATH = Path(__file__).with_name("wiki_source.json")
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_EXPANDED_BYTES = 128 * 1024 * 1024
MAX_MEMBER_BYTES = 8 * 1024 * 1024
MAX_MEMBERS = 3000
MAX_RATIO = 1000
REPARSE = 0x400
PINNED_URL = "https://codeload.github.com/StarDZ-Team/DayZ-Modding-Wiki/zip/e8d971e3941c274befdd6596baba2e5119efb3e4"


class ImportError(ValueError):
    """Unsafe path, unexpected content, or unsupported import request."""


def digest(data):
    return hashlib.sha256(data).hexdigest()


def plain_path(value, *, file=False):
    text = os.fspath(value)
    if not text or any(c in text for c in ("\0", "\r", "\n")):
        raise ImportError("Invalid local path")
    path = Path(text).expanduser().absolute()
    if ".." in path.parts or str(path).startswith("\\\\"):
        raise ImportError("Traversal and network paths are not allowed")
    if any(part.casefold() in {".git", "secrets", "keys", ".ssh"} for part in path.parts):
        raise ImportError("Private directories are not import targets")
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if part.is_symlink() or getattr(info, "st_file_attributes", 0) & REPARSE:
            raise ImportError("Symlink or junction is not allowed")
    if file and not path.is_file():
        raise ImportError("Archive is not an existing file")
    return path


def target_path(value):
    target = plain_path(value)
    if target.exists():
        raise ImportError("Target already exists; choose a new directory")
    if not target.parent.is_dir():
        raise ImportError("Target parent must already exist")
    if target == ROOT or ROOT.is_relative_to(target):
        raise ImportError("Target must not contain the Workbench repository")
    if target.is_relative_to(ROOT) and not target.is_relative_to(ROOT / ".local"):
        raise ImportError("Inside Workbench only .local may contain external corpora")
    return target


def load_spec(path=SPEC_PATH):
    data = Path(path).read_bytes()
    if len(data) > 128 * 1024:
        raise ImportError("Source specification is too large")
    spec = json.loads(data.decode("utf-8-sig"))
    record = spec.get("record")
    if not isinstance(record, dict) or not isinstance(record.get("files"), list):
        raise ImportError("Invalid source specification")
    seen = set()
    for item in record["files"]:
        relative = safe_member_name(item["path"])
        if relative != item["path"] or relative in seen:
            raise ImportError("Invalid or duplicate source specification path")
        seen.add(relative)
        if not re.fullmatch(r"[0-9a-f]{64}", item.get("sha256", "")):
            raise ImportError("Invalid pinned SHA256")
        if type(item.get("bytes")) is not int or not 0 <= item["bytes"] <= MAX_MEMBER_BYTES:
            raise ImportError("Invalid pinned file size")
    if not record["files"] or len(record["files"]) > MAX_MEMBERS:
        raise ImportError("Invalid source selection size")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", record.get("dataset", "")):
        raise ImportError("Invalid dataset name")
    return spec


def safe_member_name(name):
    if not isinstance(name, str) or not name or "\\" in name or ":" in name or "\0" in name:
        raise ImportError("Invalid ZIP member name")
    if name.startswith("/") or "//" in name or any(ord(c) < 32 for c in name):
        raise ImportError("Absolute or control-character ZIP path")
    parts = name.rstrip("/").split("/")
    if any(part in ("", ".", "..") or part.endswith((".", " ")) for part in parts):
        raise ImportError("ZIP path traversal or ambiguous component")
    reserved = {"CON", "PRN", "AUX", "NUL", *["COM" + str(i) for i in range(1, 10)], *["LPT" + str(i) for i in range(1, 10)]}
    if any(part.split(".")[0].upper() in reserved for part in parts):
        raise ImportError("Reserved filesystem path in ZIP")
    return "/".join(parts)


def inspect_archive(archive, spec):
    selected = {item["path"]: item for item in spec["record"]["files"]}
    if archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ImportError("Archive exceeds size limit")
    z = zipfile.ZipFile(archive)
    try:
        members = z.infolist()
        if len(members) > MAX_MEMBERS:
            raise ImportError("Too many archive members")
        seen = set()
        top = None
        found = {}
        total = 0
        for info in members:
            normalized = safe_member_name(info.filename)
            folded = normalized.casefold()
            if folded in seen:
                raise ImportError("Duplicate or case-colliding ZIP member")
            seen.add(folded)
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                raise ImportError("Special or symlink ZIP member")
            if info.flag_bits & 1:
                raise ImportError("Encrypted ZIP members are not supported")
            if info.file_size < 0 or info.file_size > MAX_MEMBER_BYTES:
                raise ImportError("ZIP member exceeds size limit")
            total += info.file_size
            if total > MAX_EXPANDED_BYTES or info.file_size > max(1, info.compress_size) * MAX_RATIO:
                raise ImportError("Archive expansion or compression ratio exceeds limit")
            parts = PurePosixPath(normalized).parts
            if top is None:
                top = parts[0]
            if parts[0] != top:
                raise ImportError("Archive must have one root directory")
            if info.is_dir():
                continue
            if len(parts) < 2:
                raise ImportError("Expected files below archive root")
            relative = PurePosixPath(*parts[1:]).as_posix()
            if relative in selected:
                if info.file_size != selected[relative]["bytes"]:
                    raise ImportError("Selected file size differs from pinned source: " + relative)
                found[relative] = info
        if set(found) != set(selected):
            raise ImportError("Archive is missing pinned selected files")
        return z, found
    except Exception:
        z.close()
        raise


def clean_owned_stage(stage, parent):
    # Only our freshly created temporary tree can be removed; never a user target.
    if stage is not None and stage.exists():
        if stage.parent != parent or not stage.name.startswith(".bobr-wiki-") or stage.is_symlink():
            raise ImportError("Refusing cleanup of an unowned path")
        shutil.rmtree(stage)


def import_archive(archive, target, *, spec=None):
    """Import selected text. The optional spec is for isolated first-party fixtures.

    CLI never accepts a replacement spec or arbitrary remote URL.
    """
    spec = load_spec() if spec is None else copy.deepcopy(spec)
    target = target_path(target)
    archive = plain_path(archive, file=True)
    z, selected = inspect_archive(archive, spec)
    stage = None
    try:
        stage = Path(tempfile.mkdtemp(prefix=".bobr-wiki-", dir=target.parent))
        record = copy.deepcopy(spec["record"])
        dataset = record["dataset"]
        for item in record["files"]:
            relative = item["path"]
            with z.open(selected[relative]) as stream:
                data = stream.read(MAX_MEMBER_BYTES + 1)
            if len(data) != item["bytes"] or digest(data) != item["sha256"]:
                raise ImportError("Selected file SHA256 differs from pinned source: " + relative)
            destination = stage / "sources" / dataset / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        record["archive_sha256_observed"] = digest(archive.read_bytes())
        record["selected_files_sha_verified"] = True
        record["full_archive_git_tree_verified"] = False
        records = stage / "records"
        records.mkdir()
        (records / (dataset + ".json")).write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        built = library.build_index(stage)
        verified = library.verify_index(stage)
        if not verified["ok"]:
            raise ImportError("Imported index failed verification")
        # Windows rename does not overwrite an existing target. Check again on
        # other platforms; this tool never intentionally replaces a directory.
        target_path(target)
        if os.name == "nt":
            stage.rename(target)
        else:
            # Reserve a new empty destination before replacing only our own
            # reservation. No existing user directory is accepted or removed.
            target.mkdir()
            reserved = target.stat()
            current = target.stat()
            if (reserved.st_dev, reserved.st_ino) != (current.st_dev, current.st_ino) or any(target.iterdir()):
                raise ImportError("Destination reservation changed")
            try:
                stage.rename(target)
            except Exception:
                if target.is_dir() and not any(target.iterdir()) and target.stat().st_ino == reserved.st_ino:
                    target.rmdir()
                raise
        stage = None
        return {"status": "imported", "root": str(target), "dataset": dataset,
                "commit": record["commit"], "selected_files": len(record["files"]),
                "index": built, "verification": verified, "upstream_code_executed": False,
                "authority": "Community documentation; verify owning current game API",
                "license": record["license_api"], "license_path": str(target / "sources" / dataset / "LICENCE")}
    finally:
        z.close()
        clean_owned_stage(stage, target.parent)


class PinnedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme != "https" or parsed.hostname != "codeload.github.com" or parsed.port not in (None, 443) or parsed.username or parsed.password:
            raise ImportError("Unexpected download redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_archive(destination):
    opener = urllib.request.build_opener(PinnedRedirect())
    request = urllib.request.Request(PINNED_URL, headers={"User-Agent": "BobrDayZWorkbench-wiki-import"})
    total = 0
    with opener.open(request, timeout=30) as response, Path(destination).open("xb") as output:
        declared = response.headers.get("Content-Length")
        if declared and int(declared) > MAX_ARCHIVE_BYTES:
            raise ImportError("Download exceeds archive limit")
        while part := response.read(1024 * 1024):
            total += len(part)
            if total > MAX_ARCHIVE_BYTES:
                raise ImportError("Download exceeds archive limit")
            output.write(part)
    return total


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--archive", type=Path)
    mode.add_argument("--download", action="store_true", help="Explicitly fetch only the pinned upstream ZIP")
    parser.add_argument("--target", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        target = target_path(args.target)
        if args.download:
            with tempfile.TemporaryDirectory(prefix=".bobr-wiki-download-", dir=target.parent) as directory:
                archive = Path(directory) / "wiki.zip"
                download_archive(archive)
                result = import_archive(archive, target)
        else:
            result = import_archive(args.archive, target)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile, urllib.error.URLError) as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
