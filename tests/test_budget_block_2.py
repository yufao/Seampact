import copy
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from seampact.scheduling import budget_policy as budget


def specification():
    names = ['ram', 'vram', 'latency', 'unfinished', 'quality:risk']
    return dict(schema='budget-policy/2', resource_keys={'ram': ['host_gib'], 'vram': ['gpu_gib']},
        resource_units={'ram': 'GiB', 'vram': 'GiB'},
        scales={name: 1.0 for name in names}, initial_multipliers={name: 0.0 for name in names},
        dual_step=.1, normalizer=3.0, api={'status': 'UNKNOWN'},
        quality_budgets=[dict(metric='risk', undelivered_loss=1.0)],
        points=[dict(id='ram-budget', objective='ram', limits={name: .5 for name in names if name != 'ram'})],
        blocks=[dict(windows=[0, 1], point='ram-budget', cost_weights={name: [.5, .5] for name in ['ram', 'vram', 'latency', 'unfinished']}, quality={'risk': [
            dict(group_id='group', stratum='fixture', weight=1.0, requests=[dict(task_id='a', weight=.25), dict(task_id='b', weight=.75)])]})])


def item(number, task_id, loss, ram, vram):
    feedback = {'metrics': [dict(id='risk', normalized=loss, status='KNOWN', direction='minimize')]}
    return dict(window_id='w' + str(number), task_ids=[task_id], eligible=True, release={'left': {}},
        service={'arrived': 1, 'completed': 1}, parallelism={'charged_task_seconds': 2.0},
        costs={'resource_envelope': {'status': 'KNOWN', 'resource_seconds': {'host_gib': ram, 'gpu_gib': vram}}},
        tasks=[dict(task_id=task_id, status='DONE', deadline_status='DONE', feedback=feedback, quality_credit_index=0)],
        transitions=[dict(transition=dict(reward=0, cost_at_decision=dict(resource_status='KNOWN',
            resource_seconds={'host_gib': 0, 'gpu_gib': 0}, charged_task_seconds=0, missed_arrivals=0)))])


class BlockSettlementTest(unittest.TestCase):
    def test_weighted_groups_and_fixed_conditions(self):
        spec = budget.validate(specification(), 'ppo', 'train', 2)
        items = [item(0, 'a', .2, 4, 2), item(1, 'b', .6, 8, 4)]
        dual = [0., 1., 0., 0., 2.]
        before = copy.deepcopy(dual)
        self.assertTrue(budget.settle_block(items, spec, spec['blocks'][0], 0, dual, True))
        self.assertEqual(dual, before)
        total = items[0]['budget_policy']['normalized']
        self.assertEqual(total['ram'], 6.)
        self.assertEqual(total['vram'], 3.)
        self.assertAlmostEqual(total['quality:risk'], .5)
        rewards = [x['transitions'][0]['transition']['reward'] for x in items]
        self.assertAlmostEqual(sum(rewards), -10.)
        self.assertEqual(items[0]['transitions'][0]['transition']['block_normalizer'], 3.)
        budget.update_dual(items, spec, 0, dual)
        self.assertAlmostEqual(dual[1], 1.25)
        self.assertEqual(items[0]['budget_policy']['api']['status'], 'UNKNOWN')

    def test_unknown_keeps_denominator_and_prevents_updates(self):
        spec = budget.validate(specification(), 'ppo', 'train', 2)
        items = [item(0, 'a', .2, 4, 2), item(1, 'b', .6, 8, 4)]
        items[1]['tasks'][0]['feedback']['metrics'][0]['status'] = 'UNKNOWN'
        dual = [0.] * 5
        self.assertFalse(budget.settle_block(items, spec, spec['blocks'][0], 0, dual, True))
        record = items[0]['budget_policy']
        self.assertIsNone(record['normalized']['quality:risk'])
        self.assertEqual(len(record['quality']['risk']), 2)
        self.assertEqual(dual, [0.] * 5)

    def test_undelivered_retains_weighted_loss(self):
        spec = budget.validate(specification(), 'ppo', 'train', 2)
        items = [item(0, 'a', .2, 4, 2), item(1, 'b', .6, 8, 4)]
        items[1]['tasks'][0].update(status='FAILED', feedback=None)
        items[1]['service']['completed'] = 0
        self.assertTrue(budget.settle_block(items, spec, spec['blocks'][0], 0, [0.] * 5, True))
        self.assertAlmostEqual(items[0]['budget_policy']['normalized']['quality:risk'], .8)

    def test_multiple_devices_convert_declared_units_before_scaling(self):
        spec = specification()
        spec['resource_keys']['vram'] = ['gpu_gib', 'gpu2']
        spec['resource_units']['vram'] = 'MiB'
        spec = budget.validate(spec, 'ppo', 'train', 2)
        items = [item(0, 'a', .2, 4, 1024), item(1, 'b', .6, 8, 2048)]
        for entry in items:
            entry['costs']['resource_envelope']['resource_seconds']['gpu2'] = 1024
            entry['transitions'][0]['transition']['cost_at_decision']['resource_seconds']['gpu2'] = 0
        self.assertTrue(budget.settle_block(items, spec, spec['blocks'][0], 0, [0.] * 5, True))
        self.assertEqual(items[0]['budget_policy']['normalized']['vram'], 2.5)

    def test_prechoice_quality_is_counted_in_constraint_without_fake_action(self):
        spec = budget.validate(specification(), 'ppo', 'train', 2)
        items = [item(0, 'a', .2, 4, 2), item(1, 'b', .6, 8, 4)]
        items[0]['tasks'][0]['quality_credit_index'] = None
        self.assertTrue(budget.settle_block(items, spec, spec['blocks'][0], 0, [0., 0., 0., 0., 1.], True))
        self.assertEqual(items[0]['transitions'][0]['transition']['normalized_cost_delta']['quality:risk'], 0.)
        self.assertAlmostEqual(items[0]['budget_policy']['normalized']['quality:risk'], .5)

    def test_criterion_identity_and_members_are_frozen_across_windows(self):
        spec = budget.validate(specification(), 'ppo', 'train', 2)
        metric = dict(id='risk', direction='minimize', range=[0, 1], bounds=[0, 1], critical=True)
        criterion = dict(dimensions=[metric], prompt='rubric', evidence_fields=['answer'])
        windows = [dict(task_ids=['a']), dict(task_ids=['b'])]
        frozen = budget.validate_cohorts(spec, ['a', 'b'], windows, lambda _: criterion)
        criterion['prompt'] = 'changed rubric'
        self.assertNotEqual(frozen, budget.validate_cohorts(spec, ['a', 'b'], windows, lambda _: criterion))
        windows[1]['task_ids'] = ['c']
        with self.assertRaises(ValueError):
            budget.validate_cohorts(spec, ['a', 'b'], windows, lambda _: criterion)

    def test_budget_points_balance_frozen_group_rotation(self):
        spec = specification()
        spec['points'].append(dict(id='vram-budget', objective='vram',
            limits={n: .5 for n in budget.dimensions(spec) if n != 'vram'}))
        original = spec['blocks'][0]
        spec['blocks'] = [copy.deepcopy(original) for _ in range(4)]
        for number, block in enumerate(spec['blocks']):
            block['windows'] = [number]
            block['cost_weights'] = {name: [1.] for name in ['ram', 'vram', 'latency', 'unfinished']}
            block['point'] = 'ram-budget' if number < 2 else 'vram-budget'
        # A named group cannot be reserved for one budget while another group
        # appears only under the other budget.
        for block in spec['blocks'][2:]:
            block['quality']['risk'][0]['group_id'] = 'other'
        with self.assertRaisesRegex(ValueError, 'rotation'):
            budget.validate(spec, 'ppo', 'train', 4)

    def test_unequal_windows_use_frozen_request_denominators_once(self):
        spec = specification()
        spec['blocks'][0]['cost_weights'] = dict(ram=[.25, .25], vram=[.25, .25],
            latency=[.25, .25], unfinished=[.25, .75])
        spec = budget.validate(spec, 'ppo', 'train', 2)
        items = [item(0, 'a', .2, 4, 2), item(1, 'b', .6, 8, 4)]
        items[1]['task_ids'] = ['b', 'c', 'd']
        items[1]['service'] = dict(arrived=3, completed=2)
        items[1]['parallelism']['charged_task_seconds'] = 18.
        dual = [0., 1., 1., 1., 0.]
        self.assertTrue(budget.settle_block(items, spec, spec['blocks'][0], 0, dual, True))
        totals = items[0]['budget_policy']['normalized']
        self.assertEqual({k: totals[k] for k in ['ram', 'vram', 'latency', 'unfinished']},
                         dict(ram=3., vram=1.5, latency=5., unfinished=.25))
        for name in ['ram', 'vram', 'latency', 'unfinished']:
            self.assertAlmostEqual(sum(x['transitions'][0]['transition']['normalized_cost_delta'][name] for x in items), totals[name])
        self.assertAlmostEqual(sum(x['reward'] for x in items), -9.75)
        self.assertEqual(items[0]['transitions'][0]['transition']['block_normalizer'], 3.)

    def test_cost_weights_reject_missing_misaligned_and_nonfinite_values(self):
        for weights in [{}, dict(ram=[.5], vram=[.5,.5], latency=[.5,.5], unfinished=[.5,.5]),
                        dict(ram=[float('nan'),.5], vram=[.5,.5], latency=[.5,.5], unfinished=[.5,.5])]:
            spec = specification()
            spec['blocks'][0]['cost_weights'] = weights
            with self.assertRaisesRegex(ValueError, 'cost weights'):
                budget.validate(spec, 'ppo', 'train', 2)

    @unittest.skipUnless(importlib.util.find_spec('torch'), 'PPO requires optional PyTorch')
    def test_gae_does_not_cross_independent_windows(self):
        import torch
        from seampact.scheduling.learners import Learner
        learner = Learner('ppo', 1, 1, dict(hidden_sizes=[4], gamma=1., gae_lambda=1., epochs=1,
                                            batch_size=8, rollout_steps=1), seed=1)
        for parameter in learner.networks['value'].parameters():
            parameter.data.zero_()
        for episode, state, reward, terminal in [('a', [1.], 1., False), ('a', [2.], 2., True),
                                                  ('b', [3.], 3., True)]:
            candidates, legal = [[0.], [1.]], [True, True]
            decision = learner.act(state, candidates, legal, training=True)
            learner.observe(dict(state=state, candidates=candidates, legal=legal, index=decision['index'],
                reward=reward, terminated=terminal, truncated=False, next_state=None if terminal else [2.],
                next_candidates=[] if terminal else candidates, next_legal=[] if terminal else legal,
                episode_id=episode, policy_version=decision['policy_version'], old_log_prob=decision['log_prob'],
                value=decision['value'], block_normalizer=2.))
        actual, mse = [], torch.nn.functional.mse_loss
        def capture(value, target):
            actual.extend(target.detach().tolist())
            return mse(value, target)
        with patch.object(torch.nn.functional, 'mse_loss', side_effect=capture):
            learner.update(force=True)
        self.assertEqual(sorted(actual), [2., 3., 3.])


if __name__ == '__main__':
    unittest.main()
