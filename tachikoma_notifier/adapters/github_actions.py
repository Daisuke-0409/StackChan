"""Convert GitHub Actions / webhook payloads into the common TachikomaEvent format.

GitHub delivers its webhook event name via the `X-GitHub-Event` HTTP
header, not inside the JSON body. Since this adapter's `can_handle` /
`normalize` contract only receives a payload mapping (matching every other
adapter in this package), the expected calling convention is that whatever
receives the raw webhook merges that header's value into the body under
the key `github_event` before calling this adapter -- e.g. a small receiver
script builds `{**json_body, "github_event": headers["X-GitHub-Event"]}`.
That receiver is out of scope here; this module only converts the merged
payload.

Supported `github_event` values and the standard GitHub webhook fields they
carry:

  - "workflow_run" (action == "completed"): `workflow_run.conclusion` in
    {"success", "failure", "timed_out"} -> build success / build or CI failure
  - "check_run" (action == "completed"): `check_run.conclusion == "failure"`
    -> test failure
  - "pull_request" (action == "review_requested") -> PR review waiting
  - "deployment_status": `deployment_status.state` in
    {"success", "failure", "error"} -> deploy completed / deploy failed

Anything else, or a payload missing the fields above, is safely ignored
(returns None), matching every other adapter's behavior for unknown input.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from tachikoma_notifier.events import (
    EventSeverity,
    EventSource,
    EventType,
    MAX_TITLE_LENGTH,
    SCHEMA_VERSION,
    TachikomaEvent,
    make_dedupe_key,
    make_event_id,
    normalize_message,
    utc_now_iso,
)

MAX_PROJECT_ID_LENGTH = 256
_SUPPORTED_EVENTS = frozenset({"workflow_run", "check_run", "pull_request", "deployment_status"})


def _safe_name(value: Any, default: str) -> str:
    if not isinstance(value, str) or not value.strip():
        return default
    return normalize_message(value)[:MAX_TITLE_LENGTH]


def _project_id_from(payload: Mapping[str, Any]) -> Optional[str]:
    repository = payload.get("repository")
    if not isinstance(repository, Mapping):
        return None
    full_name = repository.get("full_name")
    if not isinstance(full_name, str) or not full_name.strip():
        return None
    return full_name.strip()[:MAX_PROJECT_ID_LENGTH]


class GitHubActionsAdapter:
    source = EventSource.GITHUB_ACTIONS

    def can_handle(self, payload: Mapping[str, Any]) -> bool:
        return payload.get("github_event") in _SUPPORTED_EVENTS

    def normalize(self, payload: Mapping[str, Any]) -> Optional[TachikomaEvent]:
        github_event = payload.get("github_event")
        if github_event not in _SUPPORTED_EVENTS:
            return None
        project_id = _project_id_from(payload)

        if github_event == "workflow_run":
            return self._from_workflow_run(payload, project_id)
        if github_event == "check_run":
            return self._from_check_run(payload, project_id)
        if github_event == "pull_request":
            return self._from_pull_request(payload, project_id)
        if github_event == "deployment_status":
            return self._from_deployment_status(payload, project_id)
        return None

    def _from_workflow_run(self, payload: Mapping[str, Any], project_id: Optional[str]) -> Optional[TachikomaEvent]:
        if payload.get("action") != "completed":
            return None
        workflow_run = payload.get("workflow_run")
        if not isinstance(workflow_run, Mapping):
            return None
        conclusion = workflow_run.get("conclusion")
        name = _safe_name(workflow_run.get("name"), "ワークフロー")
        if conclusion == "success":
            return self._event(
                event_type=EventType.TASK_COMPLETED,
                severity=EventSeverity.INFO,
                title="ビルド成功",
                message=f"{name}が成功したよ",
                requires_action=False,
                project_id=project_id,
                raw_event_name="workflow_run",
            )
        if conclusion in ("failure", "timed_out"):
            return self._event(
                event_type=EventType.TASK_FAILED,
                severity=EventSeverity.ERROR,
                title="ビルド失敗",
                message=f"{name}が失敗したみたい",
                requires_action=False,
                project_id=project_id,
                raw_event_name="workflow_run",
            )
        return None

    def _from_check_run(self, payload: Mapping[str, Any], project_id: Optional[str]) -> Optional[TachikomaEvent]:
        if payload.get("action") != "completed":
            return None
        check_run = payload.get("check_run")
        if not isinstance(check_run, Mapping):
            return None
        if check_run.get("conclusion") != "failure":
            return None
        name = _safe_name(check_run.get("name"), "テスト")
        return self._event(
            event_type=EventType.TOOL_FAILED,
            severity=EventSeverity.ERROR,
            title="テスト失敗",
            message=f"{name}が失敗したみたい",
            requires_action=False,
            project_id=project_id,
            raw_event_name="check_run",
        )

    def _from_pull_request(self, payload: Mapping[str, Any], project_id: Optional[str]) -> Optional[TachikomaEvent]:
        if payload.get("action") != "review_requested":
            return None
        pull_request = payload.get("pull_request")
        title = "PR"
        if isinstance(pull_request, Mapping):
            title = _safe_name(pull_request.get("title"), "PR")
        return self._event(
            event_type=EventType.WAITING,
            severity=EventSeverity.INFO,
            title="レビュー待ち",
            message=f"{title}のレビューが必要だよ",
            requires_action=True,
            project_id=project_id,
            raw_event_name="pull_request",
        )

    def _from_deployment_status(self, payload: Mapping[str, Any], project_id: Optional[str]) -> Optional[TachikomaEvent]:
        deployment_status = payload.get("deployment_status")
        if not isinstance(deployment_status, Mapping):
            return None
        state = deployment_status.get("state")
        environment = _safe_name(deployment_status.get("environment"), "本番")
        if state == "success":
            return self._event(
                event_type=EventType.TASK_COMPLETED,
                severity=EventSeverity.INFO,
                title="デプロイ完了",
                message=f"{environment}へのデプロイが完了したよ",
                requires_action=False,
                project_id=project_id,
                raw_event_name="deployment_status",
            )
        if state in ("failure", "error"):
            return self._event(
                event_type=EventType.TASK_FAILED,
                severity=EventSeverity.ERROR,
                title="デプロイ失敗",
                message=f"{environment}へのデプロイが失敗したみたい",
                requires_action=False,
                project_id=project_id,
                raw_event_name="deployment_status",
            )
        return None

    def _event(
        self,
        *,
        event_type: EventType,
        severity: EventSeverity,
        title: str,
        message: str,
        requires_action: bool,
        project_id: Optional[str],
        raw_event_name: str,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> TachikomaEvent:
        safe_message = normalize_message(message)
        # GitHub Actions events have no session_id; pass project_id (the repo)
        # into that dedupe-key slot instead so the same message from two
        # different repos doesn't collapse into one deduplicated event.
        return TachikomaEvent(
            schema_version=SCHEMA_VERSION,
            event_id=make_event_id(),
            source=self.source,
            event_type=event_type,
            severity=severity,
            occurred_at=utc_now_iso(),
            project_id=project_id,
            title=title,
            message=safe_message,
            raw_event_name=raw_event_name,
            requires_action=requires_action,
            dedupe_key=make_dedupe_key(self.source, project_id, event_type, safe_message),
            metadata=dict(metadata or {}),
        )


__all__ = ["GitHubActionsAdapter"]
