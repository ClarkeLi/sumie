"""Planning progress reporter: steps, timing, notes, no-op without a reporter."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'client'))
import planlog


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class PlanLogTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / 'plan_progress.json'
        self.clock = FakeClock()

    def tearDown(self):
        planlog.finish(True)
        self.dir.cleanup()

    def read(self):
        return json.loads(self.path.read_text(encoding='utf-8'))

    def start(self):
        return planlog.start(self.path, [('a', 'First'), ('b', 'Second'), ('c', 'Third')],
                             clock=self.clock, heartbeat=0, echo=False)

    def test_no_reporter_is_a_noop(self):
        planlog.finish(True)
        planlog.stage('x')
        planlog.note('y', 0.5)
        planlog.skip('z')

    def test_steps_start_pending_and_progress_in_order(self):
        self.start()
        self.assertEqual([s['status'] for s in self.read()['steps']], ['pending'] * 3)
        planlog.stage('a')
        self.clock.now = 2.0
        planlog.stage('b', detail='model on mps')
        data = self.read()
        self.assertEqual([s['status'] for s in data['steps']], ['done', 'running', 'pending'])
        self.assertEqual(data['steps'][0]['seconds'], 2.0)
        self.assertEqual(data['steps'][1]['detail'], 'model on mps')
        self.assertAlmostEqual(data['fraction'], 1 / 3, places=3)

    def test_running_step_reports_live_elapsed_time(self):
        self.start()
        planlog.stage('a')
        self.clock.now = 5.5
        planlog.note('inference')
        step = self.read()['steps'][0]
        self.assertEqual(step['seconds'], 5.5)
        self.assertEqual(step['detail'], 'inference')

    def test_fraction_inside_a_step_moves_the_total(self):
        self.start()
        planlog.stage('a')
        planlog.stage('b')
        planlog.note('3/4', 0.5)
        self.assertAlmostEqual(self.read()['fraction'], (1 + 0.5) / 3, places=3)

    def test_finish_marks_leftovers_skipped_and_state_done(self):
        self.start()
        planlog.stage('a')
        planlog.finish(True)
        data = self.read()
        self.assertEqual(data['state'], 'done')
        self.assertEqual([s['status'] for s in data['steps']], ['done', 'skipped', 'skipped'])
        self.assertEqual(data['fraction'], 1.0)

    def test_failure_is_recorded_on_the_running_step(self):
        self.start()
        planlog.stage('b')
        planlog.finish(False, 'boom')
        data = self.read()
        self.assertEqual(data['state'], 'failed')
        self.assertEqual(data['error'], 'boom')
        self.assertEqual(data['steps'][1]['status'], 'failed')

    def test_unknown_stage_is_appended_and_skip_only_touches_pending(self):
        self.start()
        planlog.stage('extra', 'Extra step')
        planlog.skip('b', 'off')
        planlog.skip('extra', 'ignored')
        data = self.read()
        self.assertEqual(data['steps'][-1]['title'], 'Extra step')
        self.assertEqual(data['steps'][1]['status'], 'skipped')
        self.assertEqual(data['steps'][-1]['status'], 'running')

    def test_unicode_titles_survive_the_json_file(self):
        planlog.start(self.path, [('a', '读取图片')], clock=self.clock, heartbeat=0, echo=False)
        self.assertEqual(self.read()['steps'][0]['title'], '读取图片')


if __name__ == '__main__':
    unittest.main()
