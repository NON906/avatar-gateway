"""Hermes cronのlocal出力から、視線発話だけを待ち行列へ集めます。"""

import json
import re
import threading
from pathlib import Path


_SPEECH_PATTERN = re.compile(r"\[speech\](.*?)\[/speech\]", re.IGNORECASE | re.DOTALL)
_JOB_ID_PATTERN = re.compile(r"^[0-9a-fA-F]{8,64}$")
_OUTPUT_NAME_PATTERN = re.compile(r"^[0-9A-Za-z._-]{1,80}\.md$")
_SILENT_MARKER = "[gaze:silent]"
_MAX_UTTERANCE_CHARS = 300
_MAX_QUEUE = 20
_MAX_SEEN = 400
_MAX_OUTPUT_BYTES = 256 * 1024


def extract_gaze_lines(text: str) -> tuple[str, ...]:
    """視線発話のタグ内だけを返し、無言指定やタグのない本文は空にします。"""
    lines: list[str] = []
    for raw in _SPEECH_PATTERN.findall(text):
        collapsed = " ".join(raw.split())
        if not collapsed or _SILENT_MARKER in collapsed.lower():
            continue
        lines.append(_limit_utterance(collapsed))
    return tuple(lines)


def _limit_utterance(text: str) -> str:
    """読み上げが長くなりすぎないよう、文の区切りを優先して短くします。"""
    if len(text) <= _MAX_UTTERANCE_CHARS:
        return text
    clipped = text[:_MAX_UTTERANCE_CHARS]
    for mark in ("。", "！", "？", "!", "?"):
        index = clipped.rfind(mark)
        if index >= 20:
            return clipped[:index + 1]
    return clipped


class GazeSpeechStore:
    """名前が一致するcronジョブの新しい出力だけを、再起動後も話す順で保持します。"""

    def __init__(self, cron_dir: Path, state_path: Path, job_name: str) -> None:
        self._cron_dir = cron_dir
        self._state_path = state_path
        self._job_name = job_name
        self._lock = threading.Lock()
        self._loaded = False
        self._seeded = False
        self._job_found = False
        self._seen: set[str] = set()
        self._queue: list[str] = []

    def scan(self) -> int:
        """未処理の出力を確認し、新しく積んだ件数を返します。"""
        with self._lock:
            self._load_locked()
            job_ids = self._matching_job_ids()
            self._job_found = bool(job_ids)
            added = 0
            for job_id in job_ids:
                directory = self._output_dir(job_id)
                if directory is None or not directory.is_dir():
                    continue
                for path in sorted(directory.glob("*.md")):
                    if not _OUTPUT_NAME_PATTERN.fullmatch(path.name):
                        continue
                    key = f"{job_id}/{path.name}"
                    if key in self._seen:
                        continue
                    if not self._seeded:
                        self._seen.add(key)
                        continue
                    try:
                        if path.stat().st_size > _MAX_OUTPUT_BYTES:
                            self._seen.add(key)
                            continue
                        text = path.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
                    for line in extract_gaze_lines(text):
                        if line in self._queue:
                            continue
                        if len(self._queue) >= _MAX_QUEUE:
                            self._queue.pop(0)
                        self._queue.append(line)
                        added += 1
                    self._seen.add(key)
            self._seeded = True
            if len(self._seen) > _MAX_SEEN:
                self._seen = set(list(self._seen)[-_MAX_SEEN:])
            self._save_locked()
            return added

    def status(self) -> dict[str, object]:
        """待ち件数と、対象ジョブを見つけたかだけを返します。"""
        with self._lock:
            self._load_locked()
            return {
                "pending": len(self._queue),
                "job_found": self._job_found,
                "job_name": self._job_name,
            }

    def claim(self) -> str | None:
        """次の一文を待ち行列から外します。"""
        with self._lock:
            self._load_locked()
            if not self._queue:
                return None
            text = self._queue.pop(0)
            self._save_locked()
            return text

    def put_back(self, text: str) -> None:
        """音声化に失敗した一文を、同じ順序の先頭へ戻します。"""
        normalized = " ".join(text.split())
        if not normalized:
            return
        with self._lock:
            self._load_locked()
            if normalized not in self._queue:
                self._queue.insert(0, normalized)
            self._save_locked()

    def _matching_job_ids(self) -> tuple[str, ...]:
        """jobs.jsonから、視線発話に指定した名前のIDだけを集めます。"""
        try:
            payload = json.loads((self._cron_dir / "jobs.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            return ()
        jobs = payload.get("jobs") if isinstance(payload, dict) else payload
        if not isinstance(jobs, list):
            return ()
        ids: list[str] = []
        for job in jobs:
            if not isinstance(job, dict) or job.get("name") != self._job_name:
                continue
            job_id = job.get("id")
            if isinstance(job_id, str) and self._output_dir(job_id) is not None:
                ids.append(job_id)
        return tuple(ids)

    def _output_dir(self, job_id: str) -> Path | None:
        """ジョブIDをcron出力ディレクトリの直下だけに解決します。"""
        if not _JOB_ID_PATTERN.fullmatch(job_id):
            return None
        root = (self._cron_dir / "output").resolve()
        path = (root / job_id).resolve()
        if path == root or root not in path.parents:
            return None
        return path

    def _load_locked(self) -> None:
        """プロセス内では最初の一回だけ状態ファイルを読みます。"""
        if self._loaded:
            return
        self._loaded = True
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            return
        if not isinstance(payload, dict):
            return
        self._seeded = payload.get("seeded") is True
        seen = payload.get("seen")
        if isinstance(seen, list):
            self._seen = {item for item in seen if isinstance(item, str)}
        queue = payload.get("queue")
        if isinstance(queue, list):
            self._queue = [item.strip() for item in queue if isinstance(item, str) and item.strip()]

    def _save_locked(self) -> None:
        """待ち行列の消失を避けるため、一時ファイル経由で状態を置き換えます。"""
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._state_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                {
                    "seeded": self._seeded,
                    "seen": sorted(self._seen),
                    "queue": self._queue,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        temporary.replace(self._state_path)
