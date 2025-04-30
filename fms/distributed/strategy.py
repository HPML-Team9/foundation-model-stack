import os
import re
from abc import abstractmethod
from typing import List, Any

import torch
import torch.distributed as dist
import torch.distributed
from torch import nn

from fms.utils import tp_wrapping
from fms.modules.layernorm import LayerNormParameterized
from torch.distributed.device_mesh import init_device_mesh


from torch.distributed.tensor import Shard, Replicate
from torch.distributed.tensor.parallel import (
   parallelize_module,
   ColwiseParallel,
   RowwiseParallel,
   SequenceParallel,
   PrepareModuleInput,
   PrepareModuleOutput,
   SequenceParallel
)


if "DISTRIBUTED_STRATEGY_IGNORE_MODULES" in os.environ:
    _distributed_strategy_ignore_modules = os.environ[
        "DISTRIBUTED_STRATEGY_IGNORE_MODULES"
    ].split(",")
else:
    _distributed_strategy_ignore_modules = []


class DistributedStrategy:
    def __init__(self, from_meta=False):
        self.from_meta = from_meta

    def __should_distribute(self, module_name: str) -> bool:
        return module_name not in _distributed_strategy_ignore_modules

    def distribute_module(
        self, module: nn.Module, final_layers: bool = False, model = None
    ) -> nn.Module:
        """
        Optionally a distributed strategy may distribute modules that are not
        numbered layers
        """
        module_name = type(module).__name__
        if self.__should_distribute(module_name):
            return self._distribute_module(module, final_layers, model)
        else:
            print(f"ignoring module={module_name} when distributing module")
            return module

    def distribute_layer(self, block: nn.Module, layer: int, model = None) -> nn.Module:
        """
        Distribute each layer as-appropriate
        """
        block_name = type(block).__name__
        if self.__should_distribute(block_name):
            return self._distribute_layer(block, layer, model)
        else:
            print(f"ignoring block={block_name} when distributing layer")
            return block

    @abstractmethod
    def _distribute_module(
        self, module: nn.Module, final_layers: bool = False, model = None
    ) -> nn.Module:
        """
        Distribute modules that are not numbered layers
        """
        pass

    @abstractmethod
    def _distribute_layer(self, block: nn.Module, layer: int, model = None) -> nn.Module:
        """
        Distribute each layer
        """
        pass


class NotDistributed(DistributedStrategy):
    def __init__(self, from_meta=False):
        super().__init__(from_meta)

    def _distribute_module(
        self, module: nn.Module, final_layers: bool = False, model = None
    ) -> nn.Module:
        return module

    def _distribute_layer(self, block: nn.Module, layer: int, model = None) -> nn.Module:
        return block


NoOpStrategy = NotDistributed()


class DeviceMover(nn.Module):
    def __init__(self, module: nn.Module, device):
        super().__init__()
        self.device = device
        # make this wrapper module behave as if it was the wrapped module.
        attr = module.__dict__
        attr["module"] = module.to(device)
        attr["device"] = device
        self.__dict__ = attr

    def forward(self, *args, **kwargs):
        device = self.device
        args = [
            arg.to(device) if isinstance(arg, torch.Tensor) else arg for arg in args
        ]
        kwargs = {
            k: (
                kwargs[k].to(device)
                if isinstance(kwargs[k], torch.Tensor)
                else kwargs[k]
            )
            for k in kwargs
        }
        return self.module(*args, **kwargs)


class UniformModelParallelStrategy(DistributedStrategy):
    def __init__(self, devices: List[int], num_layers: int, from_meta=False):
        super().__init__(from_meta)
        num_dev = len(devices)
        layers_per_dev = num_layers // num_dev
        remainder = num_layers - (layers_per_dev * num_dev)
        self.layer_to_device = [0] * num_layers
        layer_id = 0
        for dev_idx in range(len(devices)):
            for i in range(layers_per_dev):
                self.layer_to_device[layer_id] = devices[dev_idx]
                layer_id = layer_id + 1
            if remainder > 0:
                self.layer_to_device[layer_id] = devices[dev_idx]
                layer_id = layer_id + 1
                remainder -= 1

    def _distribute_layer(self, block: nn.Module, layer: int, model = None) -> nn.Module:
        device = self.layer_to_device[layer]
        if self.from_meta:
            # https://github.com/pytorch/pytorch/pull/113647
            block.to_empty(device=device)  # type: ignore[arg-type]
        wrapped = DeviceMover(block, device)
        return wrapped

    def _distribute_module(
        self, module: nn.Module, final_layers: bool = False, model = None
    ) -> nn.Module:
        if final_layers:
            device = self.layer_to_device[len(self.layer_to_device) - 1]
        else:
            device = self.layer_to_device[0]
        if self.from_meta:
            return module.to_empty(device=device)  # type: ignore[arg-type]
        wrapped = DeviceMover(module, device)
        return wrapped


def generate_layer_plan(block: nn.Module, use_sequence_parallelism: bool = False) -> dict[str, Any]:
    tp_plan = {}

    colwise_patterns = [
        r"attn\.in_proj\.qkv_fused",
        r"attn\.in_proj\.(query|key|value)",
        r"ff_sub_layer\.wg1_fused",
        r"ff_sub_layer\.w1",
        r"ff_sub_layer\.wg",
    ]
    rowwise_patterns = [
        r"attn\.dense",
        r"ff_sub_layer\.w2",
    ]

    for name, module in block.named_modules():
        print(f"\n[TP] Layer Name: {name}")
        print(f"[TP] Module: {module}")
        
        children = list(module.children())
        if children:
            print(f"[TP] Children of {name}:")
            for child in children:
                print(f"*********{child}")
            print(f"[TP] Printed all children:")
        else:
            print(f"[TP] {name} has no children.")

        if use_sequence_parallelism and isinstance(module, (nn.LayerNorm, nn.Dropout, LayerNormParameterized)):
            tp_plan[name] = SequenceParallel()
        elif isinstance(module, nn.Linear):
            for pattern in colwise_patterns:
                if re.fullmatch(pattern, name):
                    tp_plan[name] = ColwiseParallel()
                    break
            for pattern in rowwise_patterns:
                if re.fullmatch(pattern, name):
                    tp_plan[name] = RowwiseParallel()
                    break
        else:
            print(f"[TP] Unmatched Layer: {name}")

    print()

    
    return tp_plan

class TensorParallelStrategy(DistributedStrategy):
    def __init__(self, group=None, from_meta=False):
        super().__init__(from_meta)
        assert torch.distributed.is_initialized(), "must initialize a process group"
        self.group = group if group is not None else torch.distributed.GroupMember.WORLD
        self.use_sequence_parallelism = os.getenv("USE_SEQUENCE_PARALLELISM", "False").lower() == "true"
        if self.use_sequence_parallelism:
            print("Using TP strategy with sequence parallelism")
        else:
            print("Using TP strategy without sequence parallelism")
        device_type = "cuda" if torch.cuda.is_available() else "cpu"
        world = torch.distributed.get_world_size()
        self.device_mesh = init_device_mesh(device_type, (world,))

    def _distribute_module(
        self, module: nn.Module, final_layers: bool = False, model = None
    ) -> nn.Module:
        if not model:
            return tp_wrapping.apply_tp(module, self.group)
        elif model == 'llama': 
            if final_layers:
                tp_plan = {
                    "shared.head": ColwiseParallel(output_layouts=Replicate(),),
                }
                return parallelize_module(module, self.device_mesh, tp_plan)
            else:
                tp_plan = {
                    "shared.emb": RowwiseParallel(input_layouts=Replicate()),
                }
                return parallelize_module(module, self.device_mesh, tp_plan)
        elif model == 'granite':
            tp_plan = {
                "head": ColwiseParallel(output_layouts=Replicate(),),
                "base_model.embedding": RowwiseParallel(input_layouts=Replicate()),
            }
            return parallelize_module(module, self.device_mesh, tp_plan)

    def _distribute_layer(self, block: nn.Module, layer: int, model = None) -> nn.Module:
        if not model:
            return tp_wrapping.apply_tp(block, self.group)
        elif model == 'llama' or model == 'granite':
            layer_tp_plan = generate_layer_plan(block, use_sequence_parallelism=self.use_sequence_parallelism)
        else:
            raise ValueError(f"Unsupported model: {model}")

        # Adjust attention module to use the local number of heads
        attn_layer = block.attn
        attn_layer.nheads = attn_layer.nheads // self.device_mesh.size()
        attn_layer.kvheads = attn_layer.kvheads // self.device_mesh.size()

        #Custom parallelization plan for the model
        block = parallelize_module(
            module=block,
            device_mesh=self.device_mesh,
            parallelize_plan=layer_tp_plan
        )

        print(f"\n[Rank {dist.get_rank()}] Test Plan:")
        module_names = dict(block.named_modules())

        for name, strategy in layer_tp_plan.items():
            if name in module_names:
                print(f"[Rank {dist.get_rank()}] {name}: {strategy.__class__.__name__}")
            else:
                print(f"[Rank {dist.get_rank()}] {name}: {strategy.__class__.__name__} (NOT FOUND)")
        
        return block