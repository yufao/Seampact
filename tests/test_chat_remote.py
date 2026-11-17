"""A group locked by another run cannot receive a remote call."""
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.hosting.deployment import Deployments


class ChatRemoteTests(unittest.TestCase):
    def test_remote_plan_cannot_enter_another_runs_locked_group(self):
        deployments = Deployments.__new__(Deployments)
        deployments.lock = threading.RLock()
        deployments.run_locks = {"group": dict(run_id="research-window")}
        with patch.object(deployments, "_route") as route, patch.object(deployments, "_call") as call:
            with self.assertRaisesRegex(ValueError, "research-window"):
                deployments.invoke_placed(dict(group_id="group", run_id="chat-task"), dict(id="unit"), {})
            route.assert_not_called()
            call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
