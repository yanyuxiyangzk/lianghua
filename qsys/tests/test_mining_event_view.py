import unittest
from mining_event_view import RoundView

class RoundViewTests(unittest.TestCase):
    def test_candidate_reset_and_preparation_retention(self):
        start = {'type':'round_start','iteration':1,'batch':15}
        prep = {'type':'step_update','step':2,'status':'done'}
        fourth = {'type':'step_update','step':4,'status':'done','batch_left':15}
        fifth = {'type':'step_update','step':5,'status':'fail'}
        events = [start, prep, fourth, fifth]
        view = RoundView().update(events)
        self.assertEqual(view.candidate, 1)
        next_event = {'type':'step_update','step':4,'status':'done','batch_left':14}
        view.update([fourth, fifth, next_event])
        self.assertEqual(view.candidate, 2)
        self.assertNotIn(5, view.steps)
        self.assertEqual(view.preparation[2], prep)
        view.update([fourth, fifth, next_event])
        self.assertEqual(view.candidate, 2)
        view.update([next_event, {'type':'round_start','iteration':2,'batch':15}])
        self.assertEqual(view.preparation, {})

    def test_missing_round_start_is_unknown_not_waiting(self):
        view = RoundView().update([{'type':'step_update','step':5,'status':'running'}])
        self.assertTrue(view.partial)
        self.assertIsNone(view.candidate)
        self.assertEqual(view.preparation, {})
