import ast
import importlib
import importlib.util
from pathlib import Path
import sys
from threading import Event
import types
import unittest
from unittest.mock import Mock, patch

from flask import Flask


root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("_worker_monitor_test", root / "app" / "worker_monitor.py")
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


class WorkerMonitorTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)

    def test_blocked_step_logs_its_name_and_main_thread_stack(self):
        reported = Event()
        with self.app.app_context(), patch.object(monitor, "SLOW_STEP_SECONDS", 0.02):
            with patch.object(self.app.logger, "error", side_effect=lambda *args: reported.set()) as log:
                with monitor.worker_step("webhooks"):
                    self.assertTrue(reported.wait(timeout=2))
        self.assertEqual(log.call_args.args[1], "webhooks")
        self.assertIn("test_blocked_step_logs", log.call_args.args[3])
        self.assertIn("wait", log.call_args.args[3])

    def test_completed_step_does_not_report_a_stall(self):
        reported = Event()
        with self.app.app_context(), patch.object(monitor, "SLOW_STEP_SECONDS", 0.02):
            with patch.object(self.app.logger, "error", side_effect=lambda *args: reported.set()):
                with monitor.worker_step("push"):
                    pass
                self.assertFalse(reported.wait(timeout=0.08))


class WorkerLoopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.package_name = "_worker_loop_test_app"
        package = types.ModuleType(cls.package_name)
        package.__path__ = [str(root / "app")]
        cls.modules = {cls.package_name: package}
        for node in ast.walk(ast.parse((root / "app" / "erp_sync.py").read_text())):
            if not isinstance(node, ast.ImportFrom) or not (node.level or node.module == "app.models"):
                continue
            name = cls.package_name + "." + node.module if node.level else node.module
            if node.module == "worker_monitor":
                cls.modules[name] = monitor
                continue
            module = cls.modules.setdefault(name, types.ModuleType(name))
            for alias in node.names:
                setattr(module, alias.name, Mock())
        cls.module_patch = patch.dict(sys.modules, cls.modules)
        cls.module_patch.start()
        cls.addClassCleanup(cls.module_patch.stop)
        cls.worker = importlib.import_module(cls.package_name + ".erp_sync")

    def setUp(self):
        self.app = Flask(__name__)
        context = self.app.app_context()
        context.push()
        self.addCleanup(context.pop)
        self.worker.db.reset_mock()

    def test_round_preserves_step_order_limits_counts_and_progress(self):
        names = (
            "process_mobile_deliveries", "requeue_stale_work",
            "enqueue_pending_commission_photo_uploads", "process_webhook_batch",
            "process_outbox_batch", "process_process_info_work",
            "process_external_assignment_mail_batch", "process_push_batch",
            "process_tenant_notification_batch", "process_tenant_sms_notification_batch", "process_completion_notification_batch",
            "process_dms_batch",
        )
        manager = Mock()
        actions = {}
        for index, name in enumerate(names):
            action = Mock(return_value=index)
            actions[name] = action
            manager.attach_mock(action, name)
            target = self.modules[self.package_name + ".mobile_sync"] if index == 0 else self.worker
            action_patch = patch.object(target, name, action)
            action_patch.start()
            self.addCleanup(action_patch.stop)
        progress = Mock()
        result = self.worker.process_due_work(outbox_limit=3, webhook_limit=4, push_limit=5, progress=progress)
        self.assertEqual([call[0] for call in manager.mock_calls], list(names))
        actions["process_webhook_batch"].assert_called_once_with(4)
        actions["process_outbox_batch"].assert_called_once_with(3)
        actions["process_push_batch"].assert_called_once_with(5)
        self.assertEqual(result, {
            "mobile_deliveries": 0, "webhooks": 3, "outbox": 4,
            "process_infos": 5, "external_assignment_emails": 6, "push": 7,
            "tenant_notifications": 8, "tenant_sms_notifications": 9, "completion_notifications": 10, "dms": 11,
        })
        self.assertEqual(progress.call_count, len(names))

    def test_failed_step_logs_phase_and_does_not_report_progress(self):
        mobile = self.modules[self.package_name + ".mobile_sync"]
        progress = Mock()
        with patch.object(mobile, "process_mobile_deliveries", side_effect=RuntimeError("Verbindung unterbrochen")):
            with self.assertLogs(self.app.logger, level="ERROR") as logs:
                with self.assertRaises(RuntimeError):
                    self.worker.process_due_work(progress=progress)
        progress.assert_not_called()
        self.assertIn("worker_step_failed step=mobile_deliveries", "\n".join(logs.output))

    def test_failed_round_logs_releases_session_and_recovers(self):
        with patch.object(self.worker, "process_due_work", side_effect=[RuntimeError("Datenbankfehler"), {}]) as work:
            with patch.object(self.worker, "sleep", side_effect=[None, KeyboardInterrupt()]) as sleep:
                with self.assertLogs(self.app.logger, level="ERROR") as logs:
                    with self.assertRaises(KeyboardInterrupt):
                        self.worker.run_worker(poll_seconds=5)
        self.assertEqual(work.call_count, 2)
        self.assertEqual(self.worker.db.session.remove.call_count, 2)
        self.assertEqual(sleep.call_count, 2)
        self.assertIn("worker_iteration_failed", "\n".join(logs.output))
        self.assertIn("Datenbankfehler", "\n".join(logs.output))

    def test_progress_renews_heartbeat_during_a_long_round(self):
        elapsed = [0]
        snapshots = []

        def work(*, progress):
            elapsed[0] = 61
            progress()
            snapshots.append(self.worker.db.session.commit.call_count)
            return {}

        with patch.object(self.worker, "monotonic", side_effect=lambda: elapsed[0]):
            with patch.object(self.worker, "process_due_work", side_effect=work):
                with patch.object(self.worker, "sleep", side_effect=KeyboardInterrupt()):
                    with self.assertRaises(KeyboardInterrupt):
                        self.worker.run_worker(poll_seconds=5)
        self.assertEqual(snapshots, [2])


if __name__ == "__main__":
    unittest.main()
