import unittest

from tachikoma_notifier.adapters.github_actions import GitHubActionsAdapter
from tachikoma_notifier.events import EventSeverity, EventSource, EventType


class GitHubActionsAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = GitHubActionsAdapter()

    def repo(self, full_name="acme/widgets"):
        return {"repository": {"full_name": full_name}}

    def test_can_handle_supported_events_only(self):
        self.assertTrue(self.adapter.can_handle({"github_event": "workflow_run"}))
        self.assertTrue(self.adapter.can_handle({"github_event": "check_run"}))
        self.assertTrue(self.adapter.can_handle({"github_event": "pull_request"}))
        self.assertTrue(self.adapter.can_handle({"github_event": "deployment_status"}))
        self.assertFalse(self.adapter.can_handle({"github_event": "push"}))
        self.assertFalse(self.adapter.can_handle({}))

    def test_workflow_run_success(self):
        payload = {
            "github_event": "workflow_run",
            "action": "completed",
            "workflow_run": {"conclusion": "success", "name": "CI"},
            **self.repo(),
        }
        event = self.adapter.normalize(payload)
        self.assertIsNotNone(event)
        self.assertEqual(event.source, EventSource.GITHUB_ACTIONS)
        self.assertEqual(event.event_type, EventType.TASK_COMPLETED)
        self.assertEqual(event.severity, EventSeverity.INFO)
        self.assertEqual(event.title, "ビルド成功")
        self.assertEqual(event.project_id, "acme/widgets")
        self.assertFalse(event.requires_action)

    def test_workflow_run_failure(self):
        payload = {
            "github_event": "workflow_run",
            "action": "completed",
            "workflow_run": {"conclusion": "failure", "name": "CI"},
            **self.repo(),
        }
        event = self.adapter.normalize(payload)
        self.assertEqual(event.event_type, EventType.TASK_FAILED)
        self.assertEqual(event.title, "ビルド失敗")

    def test_workflow_run_timed_out_is_also_a_failure(self):
        payload = {
            "github_event": "workflow_run",
            "action": "completed",
            "workflow_run": {"conclusion": "timed_out", "name": "CI"},
        }
        event = self.adapter.normalize(payload)
        self.assertEqual(event.event_type, EventType.TASK_FAILED)

    def test_workflow_run_cancelled_is_ignored(self):
        payload = {
            "github_event": "workflow_run",
            "action": "completed",
            "workflow_run": {"conclusion": "cancelled", "name": "CI"},
        }
        self.assertIsNone(self.adapter.normalize(payload))

    def test_workflow_run_not_yet_completed_is_ignored(self):
        payload = {
            "github_event": "workflow_run",
            "action": "requested",
            "workflow_run": {"conclusion": None, "name": "CI"},
        }
        self.assertIsNone(self.adapter.normalize(payload))

    def test_check_run_failure_maps_to_test_failed(self):
        payload = {
            "github_event": "check_run",
            "action": "completed",
            "check_run": {"conclusion": "failure", "name": "unit-tests"},
            **self.repo(),
        }
        event = self.adapter.normalize(payload)
        self.assertEqual(event.event_type, EventType.TOOL_FAILED)
        self.assertEqual(event.title, "テスト失敗")
        self.assertIn("unit-tests", event.message)

    def test_check_run_success_is_ignored(self):
        payload = {
            "github_event": "check_run",
            "action": "completed",
            "check_run": {"conclusion": "success", "name": "unit-tests"},
        }
        self.assertIsNone(self.adapter.normalize(payload))

    def test_pull_request_review_requested_maps_to_waiting(self):
        payload = {
            "github_event": "pull_request",
            "action": "review_requested",
            "pull_request": {"title": "Fix bug"},
            **self.repo(),
        }
        event = self.adapter.normalize(payload)
        self.assertEqual(event.event_type, EventType.WAITING)
        self.assertTrue(event.requires_action)
        self.assertIn("Fix bug", event.message)

    def test_pull_request_other_actions_are_ignored(self):
        payload = {"github_event": "pull_request", "action": "closed", "pull_request": {"title": "Fix bug"}}
        self.assertIsNone(self.adapter.normalize(payload))

    def test_deployment_status_success_maps_to_task_completed(self):
        payload = {
            "github_event": "deployment_status",
            "deployment_status": {"state": "success", "environment": "production"},
            **self.repo(),
        }
        event = self.adapter.normalize(payload)
        self.assertEqual(event.event_type, EventType.TASK_COMPLETED)
        self.assertEqual(event.title, "デプロイ完了")
        self.assertIn("production", event.message)

    def test_deployment_status_failure_maps_to_task_failed(self):
        payload = {
            "github_event": "deployment_status",
            "deployment_status": {"state": "failure", "environment": "production"},
        }
        event = self.adapter.normalize(payload)
        self.assertEqual(event.event_type, EventType.TASK_FAILED)
        self.assertEqual(event.title, "デプロイ失敗")

    def test_deployment_status_pending_is_ignored(self):
        payload = {"github_event": "deployment_status", "deployment_status": {"state": "pending"}}
        self.assertIsNone(self.adapter.normalize(payload))

    def test_malformed_nested_fields_do_not_raise(self):
        self.assertIsNone(self.adapter.normalize({"github_event": "workflow_run", "action": "completed", "workflow_run": "not-a-dict"}))
        self.assertIsNone(self.adapter.normalize({"github_event": "check_run", "action": "completed", "check_run": None}))
        self.assertIsNone(self.adapter.normalize({"github_event": "deployment_status", "deployment_status": []}))

    def test_dedupe_key_differs_across_repositories(self):
        base = {
            "github_event": "workflow_run",
            "action": "completed",
            "workflow_run": {"conclusion": "failure", "name": "CI"},
        }
        first = self.adapter.normalize({**base, "repository": {"full_name": "acme/widgets"}})
        second = self.adapter.normalize({**base, "repository": {"full_name": "acme/gadgets"}})
        self.assertNotEqual(first.dedupe_key, second.dedupe_key)


if __name__ == "__main__":
    unittest.main()
