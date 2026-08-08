"""Invoke Bug Scouter for an exact PR artifact and publish the result."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

AI_SCOPE = "https://ai.azure.com/.default"


def _request_json(
    method: str,
    url: str,
    *,
    token: str,
    payload: dict | None = None,
) -> tuple[dict, dict[str, str]]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "bug-scouter-github-action",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = json.loads(response.read().decode("utf-8"))
            headers = {key.lower(): value for key, value in response.headers.items()}
            return body, headers
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {detail}") from exc


def _with_query(url: str, **values: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parsed.query))
    query.update({key: value for key, value in values.items() if value})
    return urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query)))


def _azure_token(resource: str) -> str:
    process = subprocess.run(
        ["az", "account", "get-access-token", "--resource", resource, "--query", "accessToken", "-o", "tsv"],
        check=True,
        capture_output=True,
        text=True,
    )
    token = process.stdout.strip()
    if not token:
        raise RuntimeError(f"Azure CLI returned no token for {resource}")
    return token


def _upload_blob(args, blob_name: str, wheel_path: Path) -> None:
    subprocess.run(
        [
            "az",
            "storage",
            "blob",
            "upload",
            "--auth-mode",
            "login",
            "--account-name",
            args.storage_account,
            "--container-name",
            args.container,
            "--name",
            blob_name,
            "--file",
            str(wheel_path),
            "--overwrite",
            "true",
            "--only-show-errors",
            "-o",
            "none",
        ],
        check=True,
    )


def _event_values(event: dict) -> dict:
    pull_request = event.get("pull_request")
    repository = event.get("repository")
    if not isinstance(pull_request, dict) or not isinstance(repository, dict):
        raise ValueError("event must contain pull_request and repository objects")
    head = pull_request.get("head") or {}
    repo = head.get("repo") or {}
    full_name = repository.get("full_name")
    if repo.get("full_name") != full_name:
        raise ValueError("Bug Scouter only accepts same-repository pull requests")
    head_sha = str(head.get("sha") or "").lower()
    if len(head_sha) != 40 or any(char not in "0123456789abcdef" for char in head_sha):
        raise ValueError("pull request head SHA is invalid")
    return {
        "repository": full_name,
        "pr_number": int(pull_request["number"]),
        "head_sha": head_sha,
        "pr_title": str(pull_request.get("title") or ""),
        "html_url": str(pull_request.get("html_url") or ""),
    }


def invoke(args, event: dict) -> dict:
    values = _event_values(event)
    wheel_path = Path(args.wheel).resolve()
    document_path = Path(args.document).resolve()
    if not wheel_path.is_file():
        raise RuntimeError(f"wheel does not exist: {wheel_path}")
    if not document_path.is_file():
        raise RuntimeError(f"document does not exist: {document_path}")

    wheel = wheel_path.read_bytes()
    wheel_hash = hashlib.sha256(wheel).hexdigest()
    owner, repo = values["repository"].split("/", 1)
    blob_name = (
        f"{owner.lower()}/{repo.lower()}/{values['pr_number']}/"
        f"{values['head_sha']}/{wheel_path.name}"
    )
    _upload_blob(args, blob_name, wheel_path)

    payload = {
        "schema_version": 1,
        **values,
        "wheel_filename": wheel_path.name,
        "wheel_blob_name": blob_name,
        "wheel_sha256": wheel_hash,
        "bug_bash_document": document_path.read_text(encoding="utf-8"),
    }
    token = _azure_token("https://ai.azure.com")
    started, headers = _request_json(
        "POST",
        _with_query(args.endpoint, **{"api-version": "v1"}),
        token=token,
        payload=payload,
    )
    invocation_id = started.get("invocation_id")
    if not isinstance(invocation_id, str) or not invocation_id:
        raise RuntimeError(f"Bug Scouter did not return an invocation ID: {started}")

    poll_url = _with_query(
        f"{args.endpoint.rstrip('/')}/{urllib.parse.quote(invocation_id)}",
        **{
            "api-version": "v1",
            "agent_session_id": headers.get("x-agent-session-id", ""),
        },
    )
    deadline = time.monotonic() + args.timeout_minutes * 60
    while time.monotonic() < deadline:
        status, _ = _request_json("GET", poll_url, token=token)
        if status.get("state") != "running":
            return status
        print(f"Bug Scouter: {status.get('stage') or 'running'}", flush=True)
        time.sleep(args.poll_seconds)
    raise TimeoutError(f"Bug Scouter did not finish within {args.timeout_minutes} minutes")


def _github_request(method: str, path: str, token: str, payload: dict | None = None) -> dict:
    result, _ = _request_json(
        method,
        f"https://api.github.com/{path.lstrip('/')}",
        token=token,
        payload=payload,
    )
    return result


def publish(event: dict, response: dict, *, github_token: str, target_url: str) -> str:
    values = _event_values(event)
    result = response.get("result") if isinstance(response, dict) else None
    if response.get("state") != "completed" or not isinstance(result, dict):
        state, description = "error", "Bug Scouter infrastructure failure"
        summary = "## Bug Scouter - infrastructure failure"
    elif result.get("status") != "ok":
        state, description = "error", "Bug Scouter pipeline failed"
        summary = "## Bug Scouter - pipeline failed"
    else:
        bug_count = int(result.get("real_bug_count", 0))
        state = "success" if bug_count == 0 else "failure"
        description = "No real bugs found" if bug_count == 0 else f"{bug_count} real bug(s) found"
        summary = str(result.get("summary_markdown") or description)

    repository = values["repository"]
    _github_request(
        "POST",
        f"repos/{repository}/statuses/{values['head_sha']}",
        github_token,
        {
            "state": state,
            "context": "Bug Scouter",
            "description": description[:140],
            "target_url": target_url,
        },
    )
    marker = f"<!-- bug-scouter:{values['head_sha']} -->"
    body = f"{marker}\n{summary}\n\n[Workflow run]({target_url})"
    comments = _github_request(
        "GET",
        f"repos/{repository}/issues/{values['pr_number']}/comments?per_page=100",
        github_token,
    )
    existing = next(
        (comment for comment in comments if marker in str(comment.get("body") or "")),
        None,
    )
    if existing:
        comment = _github_request(
            "PATCH",
            f"repos/{repository}/issues/comments/{existing['id']}",
            github_token,
            {"body": body},
        )
    else:
        comment = _github_request(
            "POST",
            f"repos/{repository}/issues/{values['pr_number']}/comments",
            github_token,
            {"body": body},
        )
    return str(comment.get("html_url") or "")


def _parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--storage-account", required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--wheel", required=True)
    parser.add_argument("--document", required=True)
    parser.add_argument("--output", default="bugscouter-result.json")
    parser.add_argument("--timeout-minutes", type=int, default=90)
    parser.add_argument("--poll-seconds", type=int, default=20)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--target-url", default="")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    event = json.loads(Path(args.event).read_text(encoding="utf-8"))
    try:
        response = invoke(args, event)
    except Exception as exc:
        response = {
            "state": "failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
    Path(args.output).write_text(json.dumps(response, indent=2), encoding="utf-8")
    if args.publish:
        token = os.environ.get("GITHUB_TOKEN", "")
        if not token:
            raise RuntimeError("GITHUB_TOKEN is required to publish results")
        print(publish(event, response, github_token=token, target_url=args.target_url))
    result = response.get("result") if isinstance(response, dict) else None
    if response.get("state") != "completed" or not isinstance(result, dict):
        return 2
    if result.get("status") != "ok":
        return 2
    return 1 if int(result.get("real_bug_count", 0)) > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
