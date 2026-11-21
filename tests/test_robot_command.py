import tempfile
import unittest
from pathlib import Path

from hcs_robot_command import RobotStore


class RobotCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RobotStore(Path(self.tmp.name) / "robots.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_dock_and_robot_persist(self):
        dock = self.store.add_dock("Greenhouse Dock", "Greenhouse", 2, ["charging", "data"])
        robot = self.store.add_robot("GH-01", "inspection", "facility", "Greenhouse",
                                     dock, ["camera"])
        self.assertEqual(self.store._get("robots", robot)["preferred_dock_id"], dock)
        self.assertEqual(len(self.store.list("docks")), 1)

    def test_auto_assignment_prefers_facility(self):
        self.store.add_robot("Roamer", capabilities=["camera"])
        local = self.store.add_robot("GH-01", scope="facility", area="Greenhouse",
                                     capabilities=["camera"])
        task = self.store.create_task("Inspect irrigation", location="Greenhouse",
                                      required_capabilities=["camera"])
        self.assertEqual(self.store.auto_assign(task), local)
        self.assertEqual(self.store._get("tasks", task)["state"], "assigned")

    def test_approval_and_transition(self):
        robot = self.store.add_robot("Inspector", capabilities=["camera"])
        task = self.store.create_task("Inspect", robot_id=robot, approval_required=True)
        with self.assertRaises(ValueError):
            self.store.transition(task, "working")
        self.store.approve(task)
        self.store.transition(task, "working")
        self.store.transition(task, "completed", "Simulated inspection completed")
        self.assertEqual(self.store._get("tasks", task)["state"], "completed")
        self.assertGreaterEqual(len(self.store.events(task)), 4)

    def test_operating_area_enforced(self):
        robot = self.store.add_robot("Barn", scope="facility", area="Barn")
        task = self.store.create_task("Inspect greenhouse", location="Greenhouse")
        with self.assertRaises(ValueError):
            self.store.assign(task, robot)

    def test_physical_execution_blocked(self):
        robot = self.store.add_robot("Physical-01", simulated=False)
        task = self.store.create_task("Move", robot_id=robot)
        with self.assertRaises(ValueError):
            self.store.transition(task, "working")


if __name__ == "__main__":
    unittest.main()
