"""The new event adapter through the normal installed-Unit/Window path.

Arithmetic originals are interface fixtures, not security or resource evidence.
"""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_joint_014 as fixture
from seampact.evaluation import validate_criteria
from seampact.scenes.scenarios import freeze_scene
from seampact.scheduling.joint import Window


class ReviewEventsWindowTests(unittest.TestCase):
    def test_intake_unit_return_and_abstention_use_real_owner_and_deferred_feedback(self):
        environment = fixture.Joint014Integration()
        environment.setUp()
        self.addCleanup(environment.doCleanups)
        window = environment.window(scope="roots")
        criterion = validate_criteria(dict(id="delivered", version="1", dimensions=[dict(
            id="delivered", provider="program", binding="delivered", direction="maximize",
            unit="ratio", range=[0, 1], bounds=[0, 1], definition="actual terminal delivery")],
            objective=dict(weights=dict(delivered=1))))
        scene = copy.deepcopy(environment.scene)
        adapter = Path(__file__).resolve().parents[1] / "src/seampact/scenes/multisource_review_scene.py"
        for route, unit in zip(scene["routes"].values(), environment.units):
            route.update(criteria=criterion, evaluation_criteria=criterion,
                adapter=dict(path=str(adapter), symbol="make_scene", config=dict(schema="review-events/1",
                    abstain_answer="Declined after the configured intake check.", checks=[dict(
                        id="intake_check", stage="intake", calls=[dict(unit_id=unit, arguments=dict(value=3))])])) )
        for task in scene["tasks"]:
            task["request"] = "Review the external material."
            task["material"] = "External material."
        scene = freeze_scene(scene)
        window.manifest.update(scene=scene, criteria=criterion, evaluation_criteria=criterion)
        window.config["functional_policy"] = "all_applicable"
        window = Window(window.jobs, window.rid, window.manifest, window.catalog, window.bound,
                        None, 0, scene["tasks"], {})
        item = window.run(defer_evaluation=True)
        self.assertIsNone(item["execution_error"], item["execution_error"])
        self.assertTrue(all(task.evaluation_pending for task in window.tasks))
        self.assertFalse(item["release"]["left"])
        self.assertEqual(window.working.claims, {})
        for task in item["tasks"]:
            self.assertEqual(len(task["calls"]), 1)
            self.assertEqual(task["final_response"]["answer"], "Declined after the configured intake check.")
            self.assertEqual(task["feedback"], None)
        item = window.evaluate_pending()
        self.assertTrue(item["eligible"], item["exclusion_reason"])
        self.assertEqual([task["feedback"]["metrics"][0]["value"] for task in item["tasks"]], [1, 1])


if __name__ == "__main__":
    unittest.main()
