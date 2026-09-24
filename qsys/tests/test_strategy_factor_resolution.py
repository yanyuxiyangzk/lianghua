import unittest
import library


class ResolutionTests(unittest.TestCase):
    def test_legacy_reference_gets_code_and_kind_without_mutation(self):
        original = {'name':'a', 'kind':'evolved', 'weight':.3, 'direction':-1}
        result = library.resolve_strategy_factors([original], {'a':{'kind':'loopengine','code':'tree','factor_type':'量价'}})[0]
        self.assertEqual(result['code'], 'tree')
        self.assertEqual(result['kind'], 'loopengine')
        self.assertEqual(result['weight'], .3)
        self.assertEqual(result['direction'], -1)
        self.assertNotIn('code', original)

    def test_explicit_code_snapshot_is_not_replaced_or_rerouted(self):
        original = {'name':'a','kind':'evolved','code':'snapshot'}
        result = library.resolve_strategy_factors([original], {'a':{'kind':'loopengine','code':'new'}})[0]
        self.assertEqual(result, original)

    def test_missing_registry_does_not_invent_definition(self):
        original = {'name':'missing', 'kind':'evolved'}
        self.assertEqual(library.resolve_strategy_factors([original],{}), [original])
