# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import inspect
import sys
import unittest
from itertools import accumulate
from typing import List, Tuple

import fbgemm_gpu
import torch
import torch._dynamo
from fbgemm_gpu.permute_pooled_embedding_modules import PermutePooledEmbeddings
from hypothesis import given, HealthCheck, settings
from torch import nn, Tensor

# pyre-fixme[16]: Module `fbgemm_gpu` has no attribute `open_source`.
if getattr(fbgemm_gpu, "open_source", False):
    # pyre-ignore[21]
    from test_utils import cpu_and_maybe_gpu, gpu_unavailable, on_arm_platform
else:
    from fbgemm_gpu.test.test_utils import (
        cpu_and_maybe_gpu,
        gpu_unavailable,
        on_arm_platform,
    )

typed_gpu_unavailable: Tuple[bool, str] = gpu_unavailable
typed_on_arm_platform: Tuple[bool, str] = on_arm_platform

suppressed_list: List[HealthCheck] = (
    [HealthCheck.not_a_test_method]
    if getattr(HealthCheck, "not_a_test_method", False)
    else []
) + (
    # pyre-fixme[16]: Module `HealthCheck` has no attribute `differing_executors`.
    [HealthCheck.differing_executors]
    if getattr(HealthCheck, "differing_executors", False)
    else []
)

INTERN_MODULE = "fbgemm_gpu.permute_pooled_embedding_modules"
FIXED_EXTERN_API = {
    "PermutePooledEmbeddings": {
        "__init__": ["self", "embs_dims", "permute", "device"],
        "__call__": ["self", "pooled_embs"],
    },
}

FWD_COMPAT_MSG = (
    "WARNING: If this test is failing, you are probably trying "
    "to make changes to a module that has been marked external to PyPer torch packages. "
    "This can break forward compatibility of torch packages on training_platform "
    "(see https://fb.workplace.com/groups/pyper/permalink/808155810065803/). "
    "You need to split up your changes as follows:\n"
    "\t1. Edit your diff so it only contains the changes as optional, and not any usage of the"
    " new optional changes.\n"
    "\t2. Edit FIXED_EXTERN_API in this test so your diff passes the test.\n"
    "\t3. Land your diff and wait for the diff to be picked up by the production version of"
    " fbpkg training_platform.\n"
    "\t4. Once step 3. is complete, you can push the rest of your changes that use the new"
    " changes."
)


class Net(torch.nn.Module):
    def __init__(self) -> None:
        super(Net, self).__init__()
        self.fc1 = torch.nn.Linear(1, 10, bias=False)
        self.permute_pooled_embeddings = PermutePooledEmbeddings(
            [2, 3, 1, 4], [3, 0, 2, 1]
        )
        self.fc2 = torch.nn.Linear(10, 1, bias=False)

    def forward(self, x: Tensor) -> Tensor:
        x = self.fc1(x)
        x = self.permute_pooled_embeddings(x)
        x = self.fc2(x)
        return x


# @parameterized_class([{"device_type": "cpu"}, {"device_type": "cuda"}])
class PooledEmbeddingModulesTest(unittest.TestCase):
    @settings(deadline=10000, suppress_health_check=suppressed_list)
    # pyre-fixme[56]: Pyre was not able to infer the type of argument
    @given(device_type=cpu_and_maybe_gpu())
    def setUp(self, device_type: torch.device) -> None:
        self.device = device_type

    def test_permutation(self) -> None:
        net = Net().to(self.device)

        input = torch.Tensor([range(10)]).to(self.device)
        self.assertEqual(
            net.permute_pooled_embeddings(input).view(10).tolist(),
            [6, 7, 8, 9, 0, 1, 5, 2, 3, 4],
        )

    @unittest.skipIf(*typed_on_arm_platform)
    def test_permutation_autograd(self) -> None:
        net = Net().to(self.device)

        input = torch.randn(2, 1).to(self.device)
        input_sum = input.sum().item()

        output = net(input)
        output.sum().backward()

        # check grads for fc1 when permuted, equals to fc2 weights times input_sum
        # pyre-fixme[16]: Optional type has no attribute `view`.
        permute_res = net.permute_pooled_embeddings(net.fc1.weight.grad.view(1, 10))
        permute_ref = input_sum * net.fc2.weight
        torch.testing.assert_close(permute_res, permute_ref, rtol=1e-03, atol=1e-03)

    def test_compatibility(self) -> None:
        members = inspect.getmembers(sys.modules[INTERN_MODULE])
        for name, clazz in members:
            if getattr(clazz, "__module__", None) != INTERN_MODULE:
                continue

            self.assertIn(name, FIXED_EXTERN_API.keys(), FWD_COMPAT_MSG)

            for fn, fixed_params in FIXED_EXTERN_API[name].items():
                current_params = inspect.getfullargspec(getattr(clazz, fn)).args
                self.assertEqual(
                    fixed_params,
                    current_params,
                    msg=f"\nForward incompatible change in {name} : {fn}\n\n"
                    f"{FWD_COMPAT_MSG}",
                )

    def test_pooled_table_batched_embedding(self) -> None:
        num_emb_bags = 5
        num_embeddings = 10
        embedding_dims = [1, 2, 3, 4, 5]
        emb_weight_range = 1
        embedding_bags = [
            nn.EmbeddingBag(
                num_embeddings=num_embeddings,
                embedding_dim=embedding_dims[i],
                mode="sum",
                sparse=True,
            )
            for i in range(num_emb_bags)
        ]
        for emb_bag in embedding_bags:
            torch.nn.init.uniform_(
                emb_bag.weight,
                -emb_weight_range,
                emb_weight_range,
            )
        indices = [[0], [1, 2], [0, 1, 2], [3, 6], [8]]
        indices = [torch.tensor(i).view(-1, len(i)) for i in indices]
        pooled_embs = [emb_bag(indices[i]) for i, emb_bag in enumerate(embedding_bags)]

        cat_pooled_embs = torch.cat(pooled_embs, dim=1)

        permute_order = [2, 1, 3, 0, 4]

        permute_pooled_embeddings = PermutePooledEmbeddings(
            embedding_dims,
            permute_order,
            device=self.device,
        )
        permuted_pooled_emb = permute_pooled_embeddings(cat_pooled_embs.to(self.device))

        ref_permuted_pooled_emb = [pooled_embs[i] for i in permute_order]
        ref_permuted_pooled_emb = torch.cat(ref_permuted_pooled_emb, dim=1)

        assert torch.allclose(
            ref_permuted_pooled_emb.to(self.device), permuted_pooled_emb
        )

    @unittest.skipIf(*typed_on_arm_platform)
    def test_permutation_autograd_meta(self) -> None:
        """
        Test that permute_pooled_embeddings_autograd works with meta tensor and
        dynamo export mode
        """
        input = torch.randn(2, 1)
        net = Net()

        output_cpu = net(input)
        output_meta = net.to("meta")(input.to("meta"))

        assert output_meta.shape == output_cpu.shape
        assert input.shape == output_meta.shape

    @unittest.skipIf(*typed_gpu_unavailable)
    def test_duplicate_permutations(self) -> None:
        embs_dims = [2, 3, 1, 4]
        permute = [3, 0, 2, 0, 1, 3]
        expected_result = [6, 7, 8, 9, 0, 1, 5, 0, 1, 2, 3, 4, 6, 7, 8, 9]
        input = torch.Tensor([range(10)]).to(device="cuda")

        _permute = torch.tensor(permute, device=self.device, dtype=torch.int64)
        _offset_dim_list = torch.tensor(
            [0] + list(accumulate(embs_dims)), device=self.device, dtype=torch.int64
        )
        inv_permute: List[int] = [0] * len(permute)
        for i, p in enumerate(permute):
            inv_permute[p] = i
        _inv_permute = torch.tensor(inv_permute, device=self.device, dtype=torch.int64)
        inv_embs_dims = [embs_dims[i] for i in permute]
        _inv_offset_dim_list = torch.tensor(
            [0] + list(accumulate(inv_embs_dims)),
            device=self.device,
            dtype=torch.int64,
        )

        result = torch.ops.fbgemm.permute_duplicate_pooled_embs_auto_grad(
            input,
            _offset_dim_list.to(device=input.device),
            _permute.to(device=input.device),
            _inv_offset_dim_list.to(device=input.device),
            _inv_permute.to(device=input.device),
        )
        self.assertEqual(
            result.view(16).tolist(),
            expected_result,
        )

        input = input.to(device="cpu")
        result = torch.ops.fbgemm.permute_duplicate_pooled_embs_auto_grad(
            input,
            _offset_dim_list.to(device=input.device),
            _permute.to(device=input.device),
            _inv_offset_dim_list.to(device=input.device),
            _inv_permute.to(device=input.device),
        )
        self.assertEqual(
            result.view(16).tolist(),
            expected_result,
        )


if __name__ == "__main__":
    unittest.main()
