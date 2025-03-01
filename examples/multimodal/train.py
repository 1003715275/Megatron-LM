# Copyright (c) 2024, NVIDIA CORPORATION.  All rights reserved.
"""Pretrain or SFT multimodal."""
import os
import sys
from functools import partial

import torch
import yaml

sys.path.append(
    os.path.abspath(os.path.join(os.path.dirname(__file__), os.path.pardir, os.path.pardir))
)

from dataloader_provider import train_valid_test_dataloaders_provider
from model import model_provider
from multimodal_args import add_multimodal_extra_args

from megatron.core import mpu, tensor_parallel
from megatron.core.enums import ModelType
from megatron.core.models.multimodal.llava_model import IGNORE_INDEX, LLaVAModel
from megatron.core.parallel_state import get_tensor_model_parallel_rank
from megatron.training import get_args, get_timers, get_tokenizer, pretrain
from megatron.training.utils import is_last_rank


def get_batch(data_iterator):
    """Generate a batch"""

    args = get_args()

    imgs = None
    tokens = None
    labels = None
    loss_mask = None
    attention_mask = None
    position_ids = None
    num_tiles = None

    # Broadcast data.
    torch.cuda.nvtx.range_push("get_data")
    if data_iterator is not None and get_tensor_model_parallel_rank() == 0:
        data = next(data_iterator)
    else:
        data = None

    data_text = tensor_parallel.broadcast_data(["text"], data, torch.int64)["text"]
    target = tensor_parallel.broadcast_data(["target"], data, torch.int64)["target"]

    imgs = tensor_parallel.broadcast_data(["imgs"], data, torch.float32)["imgs"]
    num_tiles = tensor_parallel.broadcast_data(["num_tiles"], data, torch.int)["num_tiles"]

    # Dummy image, no image.
    if imgs.shape == torch.Size([1, 1]):
        imgs = torch.tensor([], dtype=torch.float32, device=data_text.device)
        num_tiles = torch.tensor([], dtype=torch.int, device=data_text.device)

    torch.cuda.nvtx.range_pop()

    tokens_ = data_text.long()

    torch.cuda.nvtx.range_push("index tokens")
    tokenizer = get_tokenizer()
    text_length = tokens_.shape[1]
    tokens = tokens_[:, :text_length].contiguous()
    labels = target[:, 1 : text_length + 1].contiguous()

    assert tokens.shape == labels.shape, f"tokens: {tokens.shape} != labels: {labels.shape}"
    torch.cuda.nvtx.range_pop()

    torch.cuda.nvtx.range_push("get_ltor_masks_and_position_ids")
    attention_mask, loss_mask, position_ids = get_ltor_masks_and_position_ids(
        tokens, labels, tokenizer.pad
    )
    torch.cuda.nvtx.range_pop()

    return tokens, labels, loss_mask, attention_mask, position_ids, imgs, num_tiles


def get_ltor_masks_and_position_ids(input_ids, target, pad_token):
    """Build masks and position id for left to right model."""
    seq_length = input_ids.shape[1]

    # Position ids.
    position_ids = torch.arange(seq_length, dtype=torch.long, device=input_ids.device)
    position_ids = position_ids.unsqueeze(0).expand_as(input_ids)

    # Loss mask.
    loss_mask = torch.ones(target.size(), dtype=torch.float, device=input_ids.device)
    loss_mask[target == pad_token] = 0.0  # mask paddings
    loss_mask[target == IGNORE_INDEX] = 0.0  # mask prompts

    # Attention mask.
    attention_mask = None

    return attention_mask, loss_mask, position_ids


def loss_func(loss_mask, output_tensor):
    losses = output_tensor.float()

    loss_mask = loss_mask.contiguous().view(-1).float()

    total_tokens = loss_mask.sum()
    total_loss = torch.sum(losses.view(-1) * loss_mask)
    loss = torch.cat([total_loss.view(1), total_tokens.view(1)])

    reporting_loss = loss.clone().detach()
    torch.distributed.all_reduce(reporting_loss, group=mpu.get_data_parallel_group())

    local_num_tokens = loss[1].clone().detach().to(torch.int)

    return (total_loss, local_num_tokens, {'lm loss': (reporting_loss[0], reporting_loss[1])})


def forward_step(data_iterator, model: LLaVAModel):
    """Forward training step.

    Args:
        data_iterator (torch.utils.data.dataloader): Input data iterator
        model: Multimodal model

    Returns:
        output_tensor (torch.Tensor): Loss of shape [b, s] if labels are provided, otherwise logits of shape [b, s, vocab_size].
        loss_func (callable): Loss function with a loss mask specified.
    """
    timers = get_timers()

    # Get the batch.
    timers('batch-generator', log_level=2).start()
    tokens, labels, loss_mask, attention_mask, position_ids, images, num_image_tiles = get_batch(
        data_iterator
    )
    timers('batch-generator').stop()

    output_tensor, loss_mask = model(
        images,
        tokens,
        position_ids,
        attention_mask,
        labels,
        loss_mask,
        num_image_tiles=num_image_tiles,
    )

    return output_tensor, partial(loss_func, loss_mask)


def llava_embedding_ranks(pp_ranks):
    """LLava's embedding ranks consist of the decoder's first and last ranks (ie, the ViT has no embeddings).
    Args:
        pp_ranks: A list of global ranks that constitute a pipeline group.
    """
    args = get_args()

    # encoder size is also the index to the first rank of the decoder.
    epp = args.encoder_pipeline_model_parallel_size

    last_rank = pp_ranks[-1]
    if len(pp_ranks) == 1 or pp_ranks[epp] == last_rank:
        return [last_rank]
    else:
        return [pp_ranks[epp], last_rank]


def llava_position_embedding_ranks(pp_ranks):
    """LLava's embedding ranks consist of the singular rank of the model or the decoder's first rank.
    Args:
        pp_ranks: A list of global ranks that constitute a pipeline group.
    """
    args = get_args()

    # encoder size is also the index to the first rank of the decoder.
    epp = args.encoder_pipeline_model_parallel_size

    last_rank = pp_ranks[-1]
    if len(pp_ranks) == 1:
        return [last_rank]
    else:
        return [pp_ranks[epp]]


def run_online_eval(model):
    """Run an evaluation benchmark during training."""
    args = get_args()

    # Online evaluation config is not defined. Do nothing.
    if not args.online_evaluation_config:
        return []

    from config import EvaluationConfig
    from run_text_generation import generate_and_write_samples

    with open(args.online_evaluation_config, "r") as f:
        config_dict = yaml.safe_load(f)

    config = EvaluationConfig(**config_dict)

    # The inference code assumes the first rank is the leader.
    # Tensorboard writer is on the last rank.
    # We must write to a storage space that all ranks see.
    output_dir = os.path.join(args.save, "online_eval")
    os.makedirs(output_dir, exist_ok=True)
    config.output_path = os.path.join(output_dir, args.language_model_type)

    # The actual generation.
    generate_and_write_samples(model[0].module, config, print_output=False)

    # Make sure the first rank is done writing so that the last rank can run eval.
    torch.distributed.barrier()

    if not is_last_rank():
        return []

    # Run evaluation.
    if config.task == "TextVQA":
        from evaluate_textvqa import textvqa_eval
        avg_acc = textvqa_eval(config.output_path)

        return [{"TextVQA accuracy": avg_acc}]
    else:
        raise NotImplementedError(f"online evaluation of {config.task} not implemented yet")


def write_online_eval_to_tensorboard(data, iteration, writer):
    """Write online evaluation data to Tensorboard."""
    if not writer:
        return

    for item in data:
        for k, v in item.items():
            writer.add_scalar(k, v, iteration)


if __name__ == "__main__":
    train_valid_test_dataloaders_provider.is_distributed = True

    pretrain(
        train_valid_test_dataloaders_provider,
        model_provider,
        ModelType.encoder_and_decoder,
        forward_step,
        args_defaults={'tokenizer_type': 'GPT2BPETokenizer'},
        extra_args_provider=add_multimodal_extra_args,
        process_non_loss_data_func=write_online_eval_to_tensorboard,
        get_embedding_ranks=llava_embedding_ranks,
        get_position_embedding_ranks=llava_position_embedding_ranks,
        non_loss_data_func=run_online_eval,
    )
