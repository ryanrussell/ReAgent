#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates. All rights reserved.

import copy
import unittest
from dataclasses import replace
from unittest.mock import MagicMock

import torch
from pytorch_lightning.loggers import TensorBoardLogger
from reagent.core.types import CBInput
from reagent.evaluation.cb.policy_evaluator import PolicyEvaluator
from reagent.models.linear_regression import LinearRegressionUCB


def _compare_state_dicts(state_dict_1, state_dict_2):
    if len(state_dict_1) != len(state_dict_2):
        return False

    for ((k_1, v_1), (k_2, v_2)) in zip(
        sorted(state_dict_1.items()), sorted(state_dict_2.items())
    ):
        if k_1 != k_2:
            return False
        if not torch.allclose(v_1, v_2):
            return False
    return True


class TestPolicyEvaluator(unittest.TestCase):
    def setUp(self):
        self.policy_network = LinearRegressionUCB(2)
        self.eval_module = PolicyEvaluator(self.policy_network)
        self.batch = CBInput(
            context_arm_features=torch.tensor(
                [
                    [
                        [1, 2],
                        [1, 3],
                    ],
                    [
                        [1, 4],
                        [1, 5],
                    ],
                ],
                dtype=torch.float,
            ),
            action=torch.tensor([[0], [1]], dtype=torch.long),
            reward=torch.tensor([[1.5], [2.3]], dtype=torch.float),
        )

    def test_process_all_data(self):

        state_dict_before = copy.deepcopy(self.eval_module.state_dict())
        self.eval_module._process_all_data(self.batch)
        state_dict_after = copy.deepcopy(self.eval_module.state_dict())

        # all_data_sum_weight_local got updated properly
        self.assertAlmostEqual(
            state_dict_after["all_data_sum_weight_local"].item()
            - state_dict_before["all_data_sum_weight_local"].item(),
            len(self.batch),
        )
        # all_data_sum_weight didn't change (bcs we haven't aggregated across instances yet)
        self.assertAlmostEqual(
            state_dict_after["all_data_sum_weight"].item(),
            state_dict_before["all_data_sum_weight"].item(),
        )

        # sum_weight and sum_reward_weighted didn't change (as well as local values)
        self.assertAlmostEqual(
            state_dict_after["sum_weight"].item(),
            state_dict_before["sum_weight"].item(),
        )
        self.assertAlmostEqual(
            state_dict_after["sum_weight_local"].item(),
            state_dict_before["sum_weight_local"].item(),
        )
        self.assertAlmostEqual(
            state_dict_after["sum_reward_weighted"].item(),
            state_dict_before["sum_reward_weighted"].item(),
        )
        self.assertAlmostEqual(
            state_dict_after["sum_reward_weighted_local"].item(),
            state_dict_before["sum_reward_weighted_local"].item(),
        )

    def test_process_used_data_reject_all(self):
        # make sure calling _process_used_data() doesn't change internal state if all weights are 0
        state_dict_before = copy.deepcopy(self.eval_module.state_dict())
        batch = replace(
            self.batch,
            weight=torch.zeros_like(self.batch.action, dtype=torch.float),
        )
        self.eval_module._process_used_data(batch)
        state_dict_after = copy.deepcopy(self.eval_module.state_dict())
        self.assertTrue(_compare_state_dicts(state_dict_before, state_dict_after))

    def test_process_used_data_accept_some(self):
        # calling _process_used_data with non-zero weights should change the state and lead to correct reward value
        policy_network = LinearRegressionUCB(2)
        eval_module = PolicyEvaluator(policy_network)
        state_dict_before = copy.deepcopy(eval_module.state_dict())
        weight_value = 2.0
        batch = replace(
            self.batch,
            weight=torch.tensor([[0.0], [weight_value]]),
        )
        eval_module._process_used_data(batch)
        eval_module._aggregate_across_instances()
        state_dict_after = copy.deepcopy(eval_module.state_dict())
        self.assertFalse(_compare_state_dicts(state_dict_before, state_dict_after))
        self.assertEqual(eval_module.sum_weight_local.item(), 0.0)
        self.assertEqual(eval_module.sum_weight.item(), weight_value)
        self.assertEqual(
            eval_module.sum_reward_weighted.item(),
            weight_value * self.batch.reward[1, 0].item(),
        )
        self.assertEqual(eval_module.sum_reward_weighted_local.item(), 0.0)
        self.assertEqual(eval_module.get_avg_reward(), self.batch.reward[1, 0].item())

    def test_update_eval_model(self):
        policy_network_1 = LinearRegressionUCB(2)
        policy_network_1.avg_A += 0.3
        policy_network_2 = LinearRegressionUCB(2)
        policy_network_2.avg_A += 0.1
        eval_module = PolicyEvaluator(policy_network_1)
        self.assertTrue(
            _compare_state_dicts(
                eval_module.eval_model.state_dict(), policy_network_1.state_dict()
            )
        )

        eval_module.update_eval_model(policy_network_2)
        self.assertTrue(
            _compare_state_dicts(
                eval_module.eval_model.state_dict(), policy_network_2.state_dict()
            )
        )

        # change to the source model shouldn't affect the model in the eval module
        original_state_dict_2 = copy.deepcopy(policy_network_2.state_dict())
        policy_network_2.avg_A += 0.4
        self.assertTrue(
            _compare_state_dicts(
                eval_module.eval_model.state_dict(), original_state_dict_2
            )
        )

    def test_ingest_batch(self):
        model_actions = torch.tensor([[1], [1]], dtype=torch.long)
        _ = self.eval_module.ingest_batch(self.batch, model_actions)
        self.eval_module._aggregate_across_instances()
        # correct average reward
        self.assertEqual(
            self.eval_module.get_avg_reward(), self.batch.reward[1, 0].item()
        )

    def test_formatted_output(self):
        model_actions = torch.tensor([[1], [1]], dtype=torch.long)
        _ = self.eval_module.ingest_batch(self.batch, model_actions)
        self.eval_module._aggregate_across_instances()
        output = self.eval_module.get_formatted_result_string()
        self.assertIsInstance(output, str)

    def test_logger(self):
        logger = TensorBoardLogger("/tmp/tb")
        logger.log_metrics = MagicMock()
        self.eval_module.attach_logger(logger)
        self.eval_module.log_metrics(step=5)

        expected_metric_dict = {
            "[model]Offline_Eval_avg_reward": 0.0,
            "[model]Offline_Eval_sum_weight": 0.0,
            "[model]Offline_Eval_all_data_sum_weight": 0.0,
            "[model]Offline_Eval_num_eval_model_updates": 0,
        }
        logger.log_metrics.assert_called_once_with(expected_metric_dict, step=5)
