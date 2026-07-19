import unittest
from datetime import datetime, timedelta, timezone

from tachikoma_notifier.events import (
    EventSeverity,
    EventSource,
    EventType,
    SCHEMA_VERSION,
    TachikomaEvent,
    make_dedupe_key,
    make_event_id,
)
from tachikoma_notifier.session_registry import (
    SessionRegistry,
    SessionRegistryCapacityError,
    make_session_key,
)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class SessionRegistryTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc)
        self.registry = SessionRegistry()

    def event(
        self,
        *,
        event_type=EventType.TASK_STARTED,
        session_id="s1",
        device_id="pc-1",
        project_id="proj-a",
        occurred_at=None,
        source=EventSource.CLAUDE_CODE,
    ):
        occurred = occurred_at or iso(self.start)
        message = "state"
        return TachikomaEvent(
            schema_version=SCHEMA_VERSION,
            event_id=make_event_id(),
            source=source,
            event_type=event_type,
            severity=EventSeverity.INFO,
            occurred_at=occurred,
            session_id=session_id,
            project_id=project_id,
            device_id=device_id,
            title="t",
            message=message,
            requires_action=False,
            dedupe_key=make_dedupe_key(source, session_id, event_type, message),
        )

    def test_upsert_registers_a_new_session(self):
        info = self.registry.upsert_from_event(self.event())
        self.assertEqual(info.status, "task_started")
        self.assertEqual(info.priority, 0)
        self.assertEqual(self.registry.count(), 1)

    def test_upsert_updates_existing_session_status(self):
        self.registry.upsert_from_event(self.event(event_type=EventType.TASK_STARTED))
        later = self.event(event_type=EventType.TASK_COMPLETED, occurred_at=iso(self.start + timedelta(seconds=5)))
        info = self.registry.upsert_from_event(later)
        self.assertEqual(info.status, "task_completed")
        self.assertEqual(self.registry.count(), 1)

    def test_identity_uses_source_device_session_project_and_workspace(self):
        self.registry.upsert_from_event(self.event(device_id="pc-1"), workspace="ws-a")
        self.registry.upsert_from_event(self.event(device_id="pc-2"), workspace="ws-a")
        self.assertEqual(self.registry.count(), 2)

    def test_different_workspace_is_a_different_session(self):
        self.registry.upsert_from_event(self.event(), workspace="ws-a")
        self.registry.upsert_from_event(self.event(), workspace="ws-b")
        self.assertEqual(self.registry.count(), 2)

    def test_out_of_order_event_does_not_regress_status(self):
        self.registry.upsert_from_event(
            self.event(event_type=EventType.TASK_COMPLETED, occurred_at=iso(self.start + timedelta(seconds=10)))
        )
        stale = self.event(event_type=EventType.TASK_FAILED, occurred_at=iso(self.start))
        info = self.registry.upsert_from_event(stale)
        self.assertEqual(info.status, "task_completed")

    def test_priority_is_preserved_across_updates_unless_overridden(self):
        self.registry.upsert_from_event(self.event(), priority=5)
        info = self.registry.upsert_from_event(
            self.event(event_type=EventType.TASK_COMPLETED, occurred_at=iso(self.start + timedelta(seconds=1)))
        )
        self.assertEqual(info.priority, 5)
        info = self.registry.upsert_from_event(
            self.event(event_type=EventType.TASK_FAILED, occurred_at=iso(self.start + timedelta(seconds=2))),
            priority=1,
        )
        self.assertEqual(info.priority, 1)

    def test_list_by_priority_orders_by_priority_then_recency(self):
        self.registry.upsert_from_event(self.event(session_id="low", occurred_at=iso(self.start)), priority=1)
        self.registry.upsert_from_event(
            self.event(session_id="high-old", occurred_at=iso(self.start + timedelta(seconds=1))), priority=9
        )
        self.registry.upsert_from_event(
            self.event(session_id="high-new", occurred_at=iso(self.start + timedelta(seconds=2))), priority=9
        )
        ordered = self.registry.list_by_priority()
        self.assertEqual([info.session_id for info in ordered], ["high-new", "high-old", "low"])

    def test_get_returns_none_for_unknown_key(self):
        key = make_session_key(EventSource.CLAUDE_CODE, "pc-1", "nope", "proj-a", None)
        self.assertIsNone(self.registry.get(key))

    def test_get_returns_registered_session_by_key(self):
        info = self.registry.upsert_from_event(self.event())
        self.assertEqual(self.registry.get(info.key), info)

    def test_remove_stale_prunes_old_sessions_only(self):
        self.registry.upsert_from_event(self.event(session_id="old", occurred_at=iso(self.start)))
        self.registry.upsert_from_event(
            self.event(session_id="new", occurred_at=iso(self.start + timedelta(seconds=100)))
        )
        removed = self.registry.remove_stale(iso(self.start + timedelta(seconds=50)))
        self.assertEqual(removed, 1)
        self.assertEqual(self.registry.count(), 1)
        self.assertEqual(self.registry.list_all()[0].session_id, "new")

    def test_capacity_is_enforced_for_new_sessions(self):
        registry = SessionRegistry(max_sessions=1)
        registry.upsert_from_event(self.event(session_id="a"))
        with self.assertRaises(SessionRegistryCapacityError):
            registry.upsert_from_event(self.event(session_id="b"))

    def test_capacity_error_does_not_block_updating_an_existing_session(self):
        registry = SessionRegistry(max_sessions=1)
        registry.upsert_from_event(self.event(session_id="a"))
        info = registry.upsert_from_event(
            self.event(
                session_id="a",
                event_type=EventType.TASK_COMPLETED,
                occurred_at=iso(self.start + timedelta(seconds=1)),
            )
        )
        self.assertEqual(info.status, "task_completed")

    def test_rejects_non_positive_max_sessions(self):
        with self.assertRaises(ValueError):
            SessionRegistry(max_sessions=0)


if __name__ == "__main__":
    unittest.main()
