import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from hcs_ai.local_codex.worker_instance import AlreadyRunningError, WorkerInstanceLock


class WorkerInstanceLockTests(unittest.TestCase):
    def test_second_mail_worker_is_rejected_while_first_is_running(self):
        with TemporaryDirectory() as directory:
            pid_path = Path(directory) / "mail_worker.pid"

            with WorkerInstanceLock(pid_path) as instance:
                self.assertEqual(pid_path.read_text(encoding="utf-8"), str(os.getpid()))
                self.assertNotEqual(instance.lock_path, pid_path)
                self.assertTrue(instance.lock_path.exists())
                with self.assertRaises(AlreadyRunningError):
                    with WorkerInstanceLock(pid_path):
                        pass

    def test_lock_can_be_acquired_again_after_worker_stops(self):
        with TemporaryDirectory() as directory:
            pid_path = Path(directory) / "mail_worker.pid"

            with WorkerInstanceLock(pid_path):
                pass
            with WorkerInstanceLock(pid_path):
                self.assertEqual(pid_path.read_text(encoding="utf-8"), str(os.getpid()))



if __name__ == "__main__":
    unittest.main()
