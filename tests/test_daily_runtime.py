import tempfile
import unittest
from pathlib import Path

from gcores_crawler.daily_runtime import update_run_state, update_step_state


class DailyRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "runtime.json"

    def test_new_run_drops_previous_steps_even_after_interruption(self):
        first = update_run_state(self.path, status="running", new_run=True)
        update_step_state(self.path, "transcribe", status="completed", extra={"completed": 7})
        next_run = update_run_state(self.path, status="running", new_run=True)
        self.assertNotEqual(next_run["run_id"], first["run_id"])
        self.assertEqual(next_run["steps"], [])
        step = update_step_state(self.path, "transcribe", status="skipped", current=0, total=0)["steps"][0]
        self.assertNotIn("details", step)
        self.assertNotIn("started_at", step)

    def test_progress_update_keeps_current_run_and_completed_is_100_percent(self):
        first = update_run_state(self.path, status="running")
        update_step_state(self.path, "index", status="running", current=3, total=5)
        progress = update_run_state(self.path, status="running", current_step_key="index")
        self.assertEqual(progress["run_id"], first["run_id"])
        self.assertEqual(len(progress["steps"]), 1)
        completed = update_step_state(self.path, "index", status="completed")["steps"][0]
        self.assertEqual(completed["progress"]["percent"], 100)
        self.assertEqual(completed["progress"]["current"], 3)
        skipped = update_step_state(self.path, "index", status="skipped", current=0, total=0)["steps"][0]
        self.assertNotIn("percent", skipped["progress"])


if __name__ == "__main__":
    unittest.main()
