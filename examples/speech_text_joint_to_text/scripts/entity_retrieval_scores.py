# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
import logging
import pickle
import sys

import torch

from fairseq import checkpoint_utils, options, tasks, utils
from fairseq.logging import progress_bar
from fairseq.data import data_utils as fairseq_data_utils
from fairseq.logging.meters import StopwatchMeter

DEBUG = False


TEXT_CACHE = {}


def generate_scores(model, samples, candidate_list, src_dict, use_cuda, scores_activation_function):
    encoder = model.encoder
    with torch.no_grad():
        speech_encoder_outs = encoder.spch_encoder(
            samples['net_input']['src_tokens'], samples['net_input']['src_lengths'], return_all_hiddens=False)
        scores = {}
        for candidate in candidate_list:
            if candidate in TEXT_CACHE:
                text_encoder_outs = TEXT_CACHE[candidate]
            else:
                src_txt_tokens = src_dict.encode_line(
                    candidate, add_if_not_exist=False, append_eos=True).long()
                src_txt_tokens = fairseq_data_utils.collate_tokens(
                    [src_txt_tokens],
                    src_dict.pad(),
                    src_dict.eos(),
                    left_pad=False,
                    move_eos_to_beginning=False,
                )
                src_txt_lengths = torch.tensor([src_txt_tokens.size()[0]], dtype=torch.long)
                if use_cuda:
                    src_txt_lengths = utils.move_to_cuda(src_txt_lengths)
                    src_txt_tokens = utils.move_to_cuda(src_txt_tokens)
                text_encoder_outs = encoder.text_encoder(
                    src_txt_tokens, src_txt_lengths, return_all_hiddens=False)
                TEXT_CACHE[candidate] = text_encoder_outs

            batch_scores = model.retrieval_network(
                text_encoder_outs['encoder_out'][0].repeat(1, samples['id'].shape[0], 1),
                speech_encoder_outs['encoder_out'][0],
                text_encoder_outs['encoder_padding_mask'][0].repeat(samples['id'].shape[0], 1),
                speech_encoder_outs['encoder_padding_mask'][0])
            for id, score in zip(samples['id'], batch_scores):
                id = id.item()
                if id not in scores:
                    scores[id] = []
                scores[id].append(scores_activation_function(score).item())
    return scores


def main(args):
    assert args.path is not None, '--path required for generation!'
    assert args.results_path is not None
    _main(args)


def _main(args):
    logging.basicConfig(
        format='%(asctime)s | %(levelname)s | %(name)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        level=logging.INFO,
        stream=sys.stdout,
    )
    logger = logging.getLogger('fairseq_cli.candidates_similarity_score')

    utils.import_user_module(args)

    if args.max_tokens is None and args.max_sentences is None:
        args.max_tokens = 12000
    logger.info(args)

    use_cuda = torch.cuda.is_available() and not args.cpu

    # Load dataset splits
    task = tasks.setup_task(args)
    task.load_dataset(args.gen_subset)

    # Load candidates
    logger.info('loading candidate list from {}'.format(args.list_candidates))
    with open(args.list_candidates) as f:
        candidate_list = [l.strip() for l in f]

    # Load ensemble
    logger.info('loading model(s) from {}'.format(args.path))
    models, _model_args = checkpoint_utils.load_model_ensemble(
        utils.split_paths(args.path),
        arg_overrides=eval(args.model_overrides),
        task=task,
    )

    # Optimize ensemble for generation
    for model in models:
        model.make_generation_fast_(
            beamable_mm_beam_size=None if args.no_beamable_mm else args.beam,
            need_attn=args.print_alignment,
        )
        if args.fp16:
            model.half()
        if use_cuda:
            model.cuda()
    dataset = task.dataset(args.gen_subset)
    itr = task.get_batch_iterator(
        dataset=dataset,
        max_tokens=args.max_tokens,
        max_sentences=args.batch_size,
        max_positions=utils.resolve_max_positions(
            task.max_positions(), *[m.max_positions() for m in models]
        ),
        ignore_invalid_inputs=args.skip_invalid_size_inputs_valid_test,
        required_batch_size_multiple=args.required_batch_size_multiple,
        seed=args.seed,
        num_shards=args.distributed_world_size,
        shard_id=args.distributed_rank,
        num_workers=args.num_workers,
        data_buffer_size=args.data_buffer_size,
    ).next_epoch_itr(shuffle=False)
    progress = progress_bar.progress_bar(
        itr,
        log_format=args.log_format,
        log_interval=args.log_interval,
        default_log_format=("tqdm" if not args.no_progress_bar else "simple"),
    )

    scores_activation_function = getattr(args, 'scores_activation_function', None)
    if scores_activation_function is not None:
        scores_activation_function = getattr(torch, scores_activation_function)
    else:
        scores_activation_function = lambda x: x

    # Initialize generator
    gen_timer = StopwatchMeter()
    num_sentences = 0
    scores = {}
    for samples in progress:
        gen_timer.start()
        samples = utils.move_to_cuda(samples) if use_cuda else samples
        scores.update(generate_scores(models[0], samples, candidate_list, dataset.src_dict, use_cuda, scores_activation_function))
        gen_timer.stop(1)

    with open(args.results_path, 'wb') as handle:
        pickle.dump(scores, handle)

    logger.info('Predicted {} sentences in {:.1f}s ({:.2f} sentences/s)'.format(
        num_sentences, gen_timer.sum, num_sentences / gen_timer.sum))


def cli_main():
    # This script computes the similarity scores for the provided
    # candidates with respect to the input speech segments.
    # It uses a retrieval network to predict whether a NE is present or not.
    parser = options.get_generation_parser()
    parser.add_argument("--list-candidates", type=str, required=True)
    parser.add_argument("--scores-activation-function", type=str)
    args = options.parse_args_and_arch(parser)
    main(args)


if __name__ == '__main__':
    cli_main()
