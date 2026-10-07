"""視線発話が、指定したcronジョブの新しい[speech]だけを積むことを確認します。"""

import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.services.gaze_speech import GazeSpeechStore, extract_gaze_lines


def _write_jobs(cron_dir: Path, jobs: list[dict]) -> None:
    """テスト用のHermes cron台帳を、実装が読む場所へ置きます。"""
    cron_dir.mkdir(parents=True, exist_ok=True)
    (cron_dir / "jobs.json").write_text(json.dumps(jobs), encoding="utf-8")


def _write_output(cron_dir: Path, job_id: str, name: str, text: str) -> None:
    """一件のcron出力を、ジョブIDの直下へ書きます。"""
    directory = cron_dir / "output" / job_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(text, encoding="utf-8")


def _store(tmp_path: Path, job_name: str = "avatar-gateway-gaze") -> GazeSpeechStore:
    """一時ディレクトリ上のcronと状態ファイルを使うストアを作ります。"""
    return GazeSpeechStore(tmp_path / "cron", tmp_path / "gaze_speech.json", job_name)


def test_extract_gaze_lines_keeps_only_speech_text() -> None:
    """説明文や無言指定を読み上げず、speechタグの中だけを文として残します。"""
    assert extract_gaze_lines("前置き\n[speech]  明日は予定があるよ。  [/speech]\n補足") == ("明日は予定があるよ。",)
    assert extract_gaze_lines("[gaze:silent]") == ()
    assert extract_gaze_lines("タグのないメモ") == ()
    assert extract_gaze_lines("[speech][gaze:silent][/speech]") == ()


def test_startup_ignores_outputs_already_on_disk(tmp_path: Path) -> None:
    """起動前からある出力は話さず、その後に増えた文だけを積むことを確認します。"""
    cron_dir = tmp_path / "cron"
    _write_jobs(cron_dir, [{"id": "abc123def456", "name": "avatar-gateway-gaze"}])
    _write_output(cron_dir, "abc123def456", "2026-10-07_09-00-00.md", "[speech]古い用件です。[/speech]")
    store = _store(tmp_path)

    assert store.scan() == 0
    assert store.claim() is None

    _write_output(cron_dir, "abc123def456", "2026-10-07_10-00-00.md", "[speech]新しい用件です。[/speech]")
    assert store.scan() == 1
    assert store.claim() == "新しい用件です。"
    assert store.claim() is None


def test_silent_and_other_jobs_do_not_enter_the_queue(tmp_path: Path) -> None:
    """無言の結果と、別の名前のジョブ出力は待ち行列へ入れません。"""
    cron_dir = tmp_path / "cron"
    _write_jobs(cron_dir, [
        {"id": "abc123def456", "name": "avatar-gateway-gaze"},
        {"id": "fff000aaa111", "name": "daily-news"},
    ])
    store = _store(tmp_path)
    assert store.scan() == 0

    _write_output(cron_dir, "abc123def456", "2026-10-07_11-00-00.md", "[gaze:silent]\n")
    _write_output(cron_dir, "fff000aaa111", "2026-10-07_11-00-00.md", "[speech]別のジョブです。[/speech]")
    assert store.scan() == 0
    assert store.status()["pending"] == 0
    assert store.status()["job_found"] is True


def test_wrapped_jobs_json_and_multiple_speech_tags(tmp_path: Path) -> None:
    """jobsを包んだ台帳でも、同じファイル内の複数文を順番どおり積むことを確認します。"""
    cron_dir = tmp_path / "cron"
    cron_dir.mkdir()
    (cron_dir / "jobs.json").write_text(json.dumps({
        "jobs": [{"id": "abc123def456", "name": "avatar-gateway-gaze"}],
    }), encoding="utf-8")
    store = _store(tmp_path)
    store.scan()
    _write_output(
        cron_dir,
        "abc123def456",
        "2026-10-07_12-00-00_123456.md",
        "[speech]最初です。[/speech]\n[speech]次です。[/speech]",
    )

    assert store.scan() == 2
    assert store.claim() == "最初です。"
    assert store.claim() == "次です。"


def test_unsafe_job_id_is_not_read(tmp_path: Path) -> None:
    """ジョブIDにディレクトリ脱出が含まれていても、cron出力の外は読みません。"""
    cron_dir = tmp_path / "cron"
    _write_jobs(cron_dir, [{"id": "../secret", "name": "avatar-gateway-gaze"}])
    outside = tmp_path / "secret"
    outside.mkdir()
    (outside / "note.md").write_text("[speech]読んではいけない文です。[/speech]", encoding="utf-8")
    store = _store(tmp_path)

    assert store.scan() == 0
    assert store.claim() is None
    assert store.status()["job_found"] is False


def test_queue_survives_restart_and_returns_failed_synthesis(tmp_path: Path) -> None:
    """再起動後も未読の文を保ち、合成失敗時は先頭へ戻すことを確認します。"""
    cron_dir = tmp_path / "cron"
    _write_jobs(cron_dir, [{"id": "abc123def456", "name": "avatar-gateway-gaze"}])
    store = _store(tmp_path)
    store.scan()
    _write_output(cron_dir, "abc123def456", "2026-10-07_13-00-00.md", "[speech]まだ話していません。[/speech]")
    store.scan()

    restored = _store(tmp_path)
    text = restored.claim()
    assert text == "まだ話していません。"
    restored.put_back(text or "")
    assert restored.claim() == "まだ話していません。"


@pytest.mark.asyncio
async def test_claim_endpoint_synthesizes_without_a_conversation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """視線発話APIが会話Runを作らず、積んだ文の音声URLだけを返すことを確認します。"""
    from dataclasses import replace

    from app import main as main_module

    cron_dir = tmp_path / "cron"
    _write_jobs(cron_dir, [{"id": "abc123def456", "name": "avatar-gateway-gaze"}])
    store = GazeSpeechStore(cron_dir, tmp_path / "state.json", "avatar-gateway-gaze")
    store.scan()
    _write_output(cron_dir, "abc123def456", "2026-10-07_14-00-00.md", "[speech]視線の用件です。[/speech]")
    monkeypatch.setattr(main_module, "gaze_store", store)
    monkeypatch.setattr(main_module, "settings", replace(main_module.settings, tts_enabled=True))

    async def fake_synthesize(text: str, _config, _client=None):
        """外部TTSの代わりに、渡された文と仮の音声IDを返します。"""
        assert text == "視線の用件です。"
        return "a" * 32, tmp_path / "voice.wav"

    monkeypatch.setattr(main_module, "synthesize", fake_synthesize)

    payload = await main_module.claim_gaze_speech()

    assert payload["spoken"] is True
    assert payload["text"] == "視線の用件です。"
    assert payload["audio_url"] == "/api/audio/" + ("a" * 32) + ".wav"
    assert store.claim() is None


@pytest.mark.asyncio
async def test_claim_endpoint_rejects_disabled_tts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """音声合成が無効なときは文を消費せず、話せない理由を返します。"""
    from dataclasses import replace

    from app import main as main_module

    cron_dir = tmp_path / "cron"
    _write_jobs(cron_dir, [{"id": "abc123def456", "name": "avatar-gateway-gaze"}])
    store = GazeSpeechStore(cron_dir, tmp_path / "state.json", "avatar-gateway-gaze")
    store.scan()
    _write_output(cron_dir, "abc123def456", "2026-10-07_15-00-00.md", "[speech]残しておく文です。[/speech]")
    monkeypatch.setattr(main_module, "gaze_store", store)
    monkeypatch.setattr(main_module, "settings", replace(main_module.settings, tts_enabled=False))

    with pytest.raises(HTTPException) as caught:
        await main_module.claim_gaze_speech()

    assert caught.value.status_code == 409
    assert store.claim() == "残しておく文です。"
