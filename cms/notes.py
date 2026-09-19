"""Annotations store — user notes anchored to highlighted spans in files.

The viewer lets a user select text in a file (rendered markdown or
syntax-highlighted source) and attach a note to it. Notes persist under
``.memory/notes.json`` so they survive restarts, keyed by the file path. Each
note records the quoted text (and a little preceding context to disambiguate
repeated quotes) so the highlight can be re-located when the file is reopened —
no fragile line/offset math that breaks the moment the file is edited.
"""

from __future__ import annotations

import json
import uuid
from .storage import locked_store, read_json, atomic_text
from datetime import datetime, timezone
from pathlib import Path

NOTES_FILE = "notes.json"
COLORS = ("amber", "blue", "green", "purple", "rose")
MAX_QUOTE = 2000
MAX_NOTE = 4000


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class NotesStore:
    def __init__(self, memory_dir: Path) -> None:
        self.path = memory_dir / NOTES_FILE
        self.memory_dir = memory_dir

    def _read(self) -> list[dict]:
        return read_json(self.path, {"notes": []}, rows="notes")["notes"]

    def _write(self, notes: list[dict]) -> None:
        atomic_text(self.path, json.dumps({"notes": notes}, indent=1))

    # -- queries ----------------------------------------------------------

    def all(self) -> list[dict]:
        return self._read()

    def for_path(self, path: str) -> list[dict]:
        return [n for n in self._read() if n.get("path") == path]

    def counts(self) -> dict[str, int]:
        """path -> number of notes, for badges in the file tree/inspector."""
        out: dict[str, int] = {}
        for n in self._read():
            out[n.get("path", "")] = out.get(n.get("path", ""), 0) + 1
        return out

    # -- mutations --------------------------------------------------------

    @locked_store()
    def add(self, path: str, quote: str, note: str, *, before: str = "",
            color: str = "amber", mode: str = "source") -> dict:
        if not path or not quote.strip():
            raise ValueError("a note needs a file path and a non-empty highlighted quote")
        entry = {
            "id": f"note-{uuid.uuid4().hex}",
            "path": path,
            "quote": quote[:MAX_QUOTE],
            "before": before[-60:],
            "note": note[:MAX_NOTE],
            "color": color if color in COLORS else "amber",
            "mode": "reader" if mode == "reader" else "source",
            "created_at": _now_iso(),
        }
        notes = self._read()
        notes.append(entry)
        self._write(notes)
        return entry

    @locked_store()
    def update(self, note_id: str, *, note: str | None = None,
               color: str | None = None) -> dict | None:
        notes = self._read()
        for n in notes:
            if n.get("id") == note_id:
                if note is not None:
                    n["note"] = note[:MAX_NOTE]
                if color is not None and color in COLORS:
                    n["color"] = color
                n["updated_at"] = _now_iso()
                self._write(notes)
                return n
        return None

    @locked_store()
    def delete(self, note_id: str) -> bool:
        notes = self._read()
        kept = [n for n in notes if n.get("id") != note_id]
        if len(kept) == len(notes):
            return False
        self._write(kept)
        return True
