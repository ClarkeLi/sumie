"""Virtual layers: the layer arithmetic, the preview order and how the executor passes them on."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'client'))
sys.path.insert(0, str(ROOT))
import anime_human as human
import layers
from anime_study import execute_study, op_requests
from dg_artist import Budget, Op, build_parser, load_plan
from test_anime_human import Bridge, Clock


def bar(points, color, size, vlayer=None, layer=0):
    op = Op('line', points, color, size, layer)
    op.vlayer = vlayer
    op.dur, op.pause = 0.2, 0.1
    return op


class LayerArithmetic(unittest.TestCase):
    def test_nine_layers_with_three_bands_are_strictly_ordered(self):
        keys = [layers.resolve(v, 3) for v in range(9)]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len(set(keys)), 9)
        self.assertEqual(keys[0], (0, 0))
        self.assertEqual(keys[8], (2, 2))

    def test_two_bands_still_give_at_least_five_layers(self):
        self.assertGreaterEqual(len({layers.resolve(v, 2) for v in range(6)}), 5)

    def test_roles_are_ordered_bottom_to_top_for_every_layer_count(self):
        for total in (5, 6, 7, 8, 9, 12):
            roles = layers.assign(total)
            values = [roles[name] for name in layers.ROLES]
            self.assertEqual(values, sorted(values), total)
            self.assertLess(roles['flat'], roles['ink'])
            self.assertLess(roles['shade'], roles['ink'])
            self.assertLessEqual(max(values), total - 1)
        self.assertIsNone(layers.assign(4))

    def test_fills_lie_under_the_lines_of_their_real_layer(self):
        pour, line = Op('bucket', [[0.5, 0.5]]), bar([[0, 0], [1, 1]], '#000000', 4)
        self.assertLess(layers.render_key(pour), layers.render_key(line))

    def test_usable_needs_enough_layers_and_the_native_patch(self):
        self.assertTrue(layers.usable({'virtual': 9, 'nativePatch': True}))
        self.assertFalse(layers.usable({'virtual': 3, 'nativePatch': True}))
        self.assertFalse(layers.usable({'virtual': 9, 'nativePatch': False}))
        self.assertFalse(layers.usable(None))

    def test_capabilities_of_an_old_plugin_are_none(self):
        class Old:
            def layers(self):
                raise RuntimeError('404')
        self.assertIsNone(layers.capabilities(Old()))


class Probe(unittest.TestCase):
    def test_no_game_and_old_plugin_are_told_apart(self):
        from dg_bridge import BridgeError

        class Gone:
            def layers(self):
                raise BridgeError('Cannot reach the bridge')

        class Old:
            def layers(self):
                raise BridgeError('not found', 404)

        class New:
            def layers(self):
                return {'virtual': 9, 'bands': 3, 'nativePatch': True}

        self.assertEqual(layers.probe(Gone()), (None, False))
        self.assertEqual(layers.probe(Old()), (None, True))
        self.assertEqual(layers.probe(New())[1], True)
        self.assertEqual(layers.probe(New())[0]['virtual'], 9)


class PreviewOrder(unittest.TestCase):
    def render(self, ops):
        picture, _ = human.simulate(ops, 16 / 9, 17.7778, width=640)
        return picture

    def centre(self, picture):
        h, w = picture.shape[:2]
        return tuple(int(v) for v in picture[h // 2, w // 2])

    def crossing(self, first, second):
        return [bar([[0.2, 0.5], [0.8, 0.5]], first[0], 35, first[1]),
                bar([[0.5, 0.2], [0.5, 0.8]], second[0], 35, second[1])]

    def test_a_higher_virtual_layer_wins_whatever_the_drawing_order(self):
        red, blue = '#ff0000', '#0000ff'
        low_first = self.render(self.crossing((red, 1), (blue, 6)))
        high_first = self.render(self.crossing((blue, 6), (red, 1)))
        self.assertEqual(self.centre(low_first), (0, 0, 255))
        self.assertEqual(self.centre(high_first), (0, 0, 255))

    def test_same_layer_keeps_the_drawing_order(self):
        picture = self.render(self.crossing(('#ff0000', 1), ('#0000ff', 1)))
        self.assertEqual(self.centre(picture), (0, 0, 255))
        picture = self.render(self.crossing(('#0000ff', 1), ('#ff0000', 1)))
        self.assertEqual(self.centre(picture), (255, 0, 0))

    def test_real_layers_still_work_without_virtual_ones(self):
        ops = self.crossing(('#ff0000', None), ('#0000ff', None))
        ops[0].layer, ops[1].layer = 1, 0
        self.assertEqual(self.centre(self.render(ops)), (255, 0, 0))


class Passing(unittest.TestCase):
    def test_requests_carry_the_virtual_layer(self):
        op = bar([[0.1, 0.1], [0.4, 0.4]], '#123456', 6, vlayer=3)
        self.assertEqual({r['vlayer'] for r in op_requests(op)}, {3})
        plain = bar([[0.1, 0.1], [0.4, 0.4]], '#123456', 6)
        self.assertTrue(all('vlayer' not in r for r in op_requests(plain)))

    def test_plan_json_keeps_the_virtual_layer(self):
        ops = [bar([[0.1, 0.1], [0.4, 0.4]], '#123456', 6, vlayer=7), bar([[0.1, 0.1], [0.4, 0.4]], '#123456', 6)]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'plan.json'
            path.write_text(json.dumps([o.to_json() for o in ops]))
            again = load_plan(path)
        self.assertEqual([o.vlayer for o in again], [7, None])


class LayerBridge(Bridge):
    def __init__(self, clock):
        super().__init__(clock)
        self.layer_calls, self.pen_vlayers = [], []

    def layer(self, value):
        self.layer_calls.append(value)

    def layers(self):
        return {'virtual': 9, 'bands': 3, 'nativePatch': True}

    def pen_stroke(self, samples, force=False, vlayer=None, **_):
        self.pen_vlayers.append(vlayer)
        return super().pen_stroke(samples, force=force)


class Execution(unittest.TestCase):
    def setUp(self):
        self.args = build_parser().parse_args(['unused', '--style', 'anime_human', '--dry-run'])
        self.args.study_deadline = 1e9
        self.args.pace = 'slow'

    def run_ops(self, pen):
        self.args.pen = pen
        ops = [bar([[0.1, 0.2], [0.2, 0.3], [0.3, 0.2]], '#112233', 8, vlayer=v, layer=v // 3)
               for v in (6, 1, 3)]
        for o in ops:
            o.pen = [[0, 0.1, 0.2, 0.3], [100, 0.2, 0.3, 0.3], [200, 0.3, 0.2, 0.3]]
        clock = Clock()
        bridge = LayerBridge(clock)
        with patch('anime_study.time.monotonic', clock.monotonic), \
                patch('anime_study.time.sleep', clock.sleep):
            execute_study(bridge, ops, Budget(100, 90, 900, False, 'test', 3), self.args)
        return bridge

    def test_direct_strokes_name_their_layer_and_the_executor_does_not_switch(self):
        bridge = self.run_ops('direct')
        self.assertEqual([s['vlayer'] for s in bridge.strokes], [6, 1, 3])
        self.assertEqual(bridge.layer_calls, [0])           # only the reset at the start

    def test_pen_strokes_name_their_layer_too(self):
        bridge = self.run_ops('native')
        self.assertEqual(bridge.pen_vlayers, [6, 1, 3])
        self.assertEqual(bridge.layer_calls, [0])

    def test_the_pen_is_not_used_when_the_plugin_cannot_place_it(self):
        self.args.colour, self.args.native_layers = 'brush', False
        with patch.object(LayerBridge, 'pen_status', lambda self: {'usePenPressure': True}, create=True):
            bridge = self.run_ops('auto')
        self.assertEqual(bridge.pen_vlayers, [])
        self.assertEqual([s['vlayer'] for s in bridge.strokes], [6, 1, 3])


if __name__ == '__main__':
    unittest.main()
