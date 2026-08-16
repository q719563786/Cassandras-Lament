import threading
import unittest

from yuanjian_app.operations import CognitionOperation, OperationBusy


class BlockingController:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def process_once(self):
        self.started.set()
        if not self.release.wait(2):
            raise TimeoutError("test did not release controller")
        return {"judgments": 2}


class CognitionOperationTests(unittest.TestCase):
    def test_second_run_is_rejected_while_first_is_active(self):
        controller = BlockingController()
        operation = CognitionOperation(controller)
        results = []
        worker = threading.Thread(
            target=lambda: results.append(operation.run("manual")), daemon=True
        )
        worker.start()
        self.assertTrue(controller.started.wait(1))

        try:
            with self.assertRaises(OperationBusy):
                operation.run("scheduled")
        finally:
            controller.release.set()
            worker.join(1)

        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]["source"], "manual")
        self.assertEqual(results[0]["judgments"], 2)
        self.assertGreaterEqual(results[0]["elapsed_ms"], 0)

    def test_lock_is_released_after_controller_failure(self):
        class FailingOnceController:
            def __init__(self):
                self.calls = 0

            def process_once(self):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("failed")
                return {"status": "recovered"}

        operation = CognitionOperation(FailingOnceController())

        with self.assertRaises(RuntimeError):
            operation.run("manual")
        self.assertEqual(operation.run("manual")["status"], "recovered")

    def test_running_and_started_at_reflect_lock_state(self):
        """Regression: /api/cognition/status must report whether the
        operation is currently busy so the Action Home button can poll
        until the background scheduler finishes instead of showing
        '认知任务正在运行' as a hard failure.
        """
        controller = BlockingController()
        operation = CognitionOperation(controller)

        # Idle before any run
        self.assertFalse(operation.running)
        self.assertIsNone(operation.started_at_monotonic)

        worker = threading.Thread(
            target=lambda: operation.run("scheduled"), daemon=True
        )
        worker.start()
        self.assertTrue(controller.started.wait(1))

        # Busy while the controller is blocked
        self.assertTrue(operation.running)
        self.assertIsNotNone(operation.started_at_monotonic)

        try:
            with self.assertRaises(OperationBusy):
                operation.run("manual")
            # Still busy after a rejected attempt
            self.assertTrue(operation.running)
        finally:
            controller.release.set()
            worker.join(1)

        # Idle after the worker exits
        self.assertFalse(operation.running)
        self.assertIsNone(operation.started_at_monotonic)


if __name__ == "__main__":
    unittest.main()
