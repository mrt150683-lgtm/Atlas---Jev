"""Phase 1: clean directory scanner.

Walks a root directory, prunes junk via gitignore-style patterns, keeps only
whitelisted source extensions, and returns FileRecord metadata for each file.
"""

from __future__ import annotations

import os
import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path

import pathspec

from .config import CMSIGNORE_FILENAME, DEFAULT_IGNORES, LANGUAGE_BY_EXTENSION
from .scope import dir_in_scope, file_in_scope, load_scope


@dataclass
class FileRecord:
    rel_path: str  # posix-style, relative to scan root
    abs_path: str
    size_bytes: int
    line_count: int
    mtime: float
    language: str
    content_hash: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def load_ignore_spec(root: Path) -> pathspec.PathSpec:
    """Ignore rules, in increasing precedence: built-in defaults, then the
    project's own ``.gitignore`` (what IT declares as non-source — no guessing
    by us), then ``.cmsignore`` (user overrides, which can re-include with
    ``!pattern``)."""
    lines = list(DEFAULT_IGNORES)
    gitignore = root / ".gitignore"
    if gitignore.is_file():
        lines += gitignore.read_text(encoding="utf-8", errors="ignore").splitlines()
    cmsignore = root / CMSIGNORE_FILENAME
    if cmsignore.is_file():
        lines += cmsignore.read_text(encoding="utf-8", errors="ignore").splitlines()
    if hasattr(pathspec, "GitIgnoreSpec"):
        return pathspec.GitIgnoreSpec.from_lines(lines)
    return pathspec.PathSpec.from_lines("gitwildmatch", lines)


def _ignore_spec(lines):
    if hasattr(pathspec, "GitIgnoreSpec"):
        return pathspec.GitIgnoreSpec.from_lines(lines)
    return pathspec.PathSpec.from_lines("gitwildmatch", lines)


class IgnoreMatcher:
    """Inherited Git rules, followed by the root's explicit Atlas overrides.

    Paths are relative to the scan root. Each nested .gitignore is evaluated
    relative to its own directory, so anchored patterns and negations retain
    their Git meanings. Excluded parents are pruned just as Git prunes them.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.defaults = _ignore_spec(DEFAULT_IGNORES)
        self.overrides = self._read(self.root / CMSIGNORE_FILENAME)
        self._git_specs = {}

    @staticmethod
    def _read(path):
        if not path.is_file():
            return _ignore_spec([])
        return _ignore_spec(path.read_text(encoding="utf-8", errors="ignore").splitlines())

    @staticmethod
    def _decision(spec, path):
        # include=None means no rule matched; False is an explicit negation.
        if hasattr(spec, "check_file"):
            return spec.check_file(path).include
        decision = None
        for pattern in spec.patterns:
            if pattern.include is not None and pattern.match_file(path) is not None:
                decision = pattern.include
        return decision

    def match_file(self, rel: str) -> bool:
        rel = rel.replace("\\", "/")
        ignored = bool(self._decision(self.defaults, rel))
        parts = rel.rstrip("/").split("/")
        # The target directory's own ignore file affects its children, not
        # the directory entry being considered for traversal.
        for depth in range(len(parts)):
            prefix = "/".join(parts[:depth])
            if prefix not in self._git_specs:
                self._git_specs[prefix] = self._read(self.root / prefix / ".gitignore")
            local = rel[len(prefix) + 1:] if prefix else rel
            decision = self._decision(self._git_specs[prefix], local)
            if decision is not None:
                ignored = decision
        override = self._decision(self.overrides, rel)
        return bool(ignored if override is None else override)


def _source_facts(path: Path) -> tuple[int, str]:
    """Count lines and fingerprint the same bytes, without loading large files."""
    digest = hashlib.sha256()
    lines = 0
    last = b""
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(65536), b""):
                digest.update(block)
                lines += block.count(b"\n")
                last = block[-1:]
    except OSError:
        return 0, ""
    return lines + bool(last and last != b"\n"), digest.hexdigest()


def _count_lines(path: Path) -> int:
    try:
        with open(path, "rb") as f:
            content = f.read()
    except OSError:
        return 0
    if not content:
        return 0
    return content.count(b"\n") + (0 if content.endswith(b"\n") else 1)


# @memory:feature:CleanDirectoryScanner
# @memory:connects:TreeExport, KnowledgeGraphConstruction
# @memory:summary:Single source of truth for what belongs to the codebase — walks the tree, prunes junk dirs in place, whitelists source extensions, records metadata.
def scan(root: Path | str) -> list[FileRecord]:
    root = Path(root).resolve()
    spec = IgnoreMatcher(root)
    scope = load_scope(root)  # None => whole codebase; else only selected dirs/files
    records: list[FileRecord] = []

    for dirpath, dirnames, filenames in os.walk(root):
        dir_rel = Path(dirpath).relative_to(root).as_posix()
        prefix = "" if dir_rel == "." else dir_rel + "/"
        # prune ignored / out-of-scope directories in place so os.walk skips them
        dirnames[:] = sorted(
            d for d in dirnames
            if not spec.match_file(f"{prefix}{d}/")
            and dir_in_scope(f"{prefix}{d}/", scope)
        )
        for name in sorted(filenames):
            rel = f"{prefix}{name}"
            if spec.match_file(rel):
                continue
            if not file_in_scope(rel, scope):
                continue
            ext = Path(name).suffix.lower()
            language = LANGUAGE_BY_EXTENSION.get(ext)
            if language is None:
                continue
            p = Path(dirpath) / name
            try:
                stat = p.stat()
            except OSError:
                continue
            line_count, content_hash = _source_facts(p)
            records.append(
                FileRecord(
                    rel_path=rel,
                    abs_path=str(p),
                    size_bytes=stat.st_size,
                    line_count=line_count,
                    mtime=stat.st_mtime,
                    language=language,
                    content_hash=content_hash,
                )
            )
    return records
