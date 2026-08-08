from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("run_bugscouter.py")
SPEC = importlib.util.spec_from_file_location("run_bugscouter", SCRIPT)
run_bugscouter = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(run_bugscouter)


def _event(repository="owner/repo", head_repository="owner/repo"):
    return {
        "repository": {"full_name": repository},
        "pull_request": {
            "number": 7,
            "title": "Test PR",
            "html_url": "https://github.com/owner/repo/pull/7",
            "head": {"sha": "a" * 40, "repo": {"full_name": head_repository}},
        },
    }


def test_event_values_bind_same_repository_and_exact_sha():
    values = run_bugscouter._event_values(_event())
    assert values["repository"] == "owner/repo"
    assert values["pr_number"] == 7
    assert values["head_sha"] == "a" * 40


def test_event_values_reject_fork_pull_request():
    with pytest.raises(ValueError, match="same-repository"):
        run_bugscouter._event_values(_event(head_repository="other/repo"))


def test_azure_token_uses_requested_resource(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return type("Process", (), {"stdout": "token\n"})()

    monkeypatch.setattr(run_bugscouter.subprocess, "run", run)
    assert run_bugscouter._azure_token("https://ai.azure.com") == "token"
    assert calls[0][0][0:5] == [
        "az",
        "account",
        "get-access-token",
        "--resource",
        "https://ai.azure.com",
    ]
    assert calls[0][1]["check"] is True


def test_publish_sets_failure_and_updates_existing_comment(monkeypatch):
    calls = []
    marker = f"<!-- bug-scouter:{'a' * 40} -->"

    def request(method, path, token, payload=None):
        calls.append((method, path, token, payload))
        if path.endswith("comments?per_page=100"):
            return [{"id": 99, "body": marker}]
        return {"html_url": "https://github.com/owner/repo/pull/7#issuecomment-99"}

    monkeypatch.setattr(run_bugscouter, "_github_request", request)
    url = run_bugscouter.publish(
        _event(),
        {
            "state": "completed",
            "result": {
                "status": "ok",
                "real_bug_count": 2,
                "summary_markdown": "## Two bugs",
            },
        },
        github_token="token",
        target_url="https://github.com/owner/repo/actions/runs/1",
    )

    assert calls[0][3]["state"] == "failure"
    assert calls[-1][0:2] == (
        "PATCH",
        "repos/owner/repo/issues/comments/99",
    )
    assert url.endswith("issuecomment-99")


def test_publish_creates_success_comment(monkeypatch):
    calls = []

    def request(method, path, token, payload=None):
        calls.append((method, path, token, payload))
        if path.endswith("comments?per_page=100"):
            return []
        return {"html_url": "https://github.com/owner/repo/pull/7#issuecomment-1"}

    monkeypatch.setattr(run_bugscouter, "_github_request", request)
    run_bugscouter.publish(
        _event(),
        {
            "state": "completed",
            "result": {
                "status": "ok",
                "real_bug_count": 0,
                "summary_markdown": "## No bugs",
            },
        },
        github_token="token",
        target_url="https://github.com/owner/repo/actions/runs/1",
    )

    assert calls[0][3]["state"] == "success"
    assert calls[-2][3] is None
    assert calls[-1][0:2] == ("POST", "repos/owner/repo/issues/7/comments")


def test_main_publishes_infrastructure_failure(tmp_path, monkeypatch):
    event_path = tmp_path / "event.json"
    event_path.write_text(__import__("json").dumps(_event()), encoding="utf-8")
    output_path = tmp_path / "result.json"
    published = []
    monkeypatch.setattr(
        run_bugscouter,
        "_parse_args",
        lambda: type("Args", (), {
            "event": str(event_path),
            "output": str(output_path),
            "publish": True,
            "target_url": "https://example/run",
        })(),
    )
    monkeypatch.setattr(
        run_bugscouter,
        "invoke",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("Azure unavailable")),
    )
    monkeypatch.setattr(
        run_bugscouter,
        "publish",
        lambda _event, response, **_kwargs: published.append(response),
    )
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    assert run_bugscouter.main() == 2
    assert published[0]["state"] == "failed"
    assert "Azure unavailable" in output_path.read_text(encoding="utf-8")
