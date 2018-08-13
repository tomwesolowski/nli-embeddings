#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Trains a simple baseline on SNLI
Run like: python src/scripts/train_esim.py cc840 results/test_run
"""

import argparse
import json
import os

from src.models import build_model
from src.util import modified_stream, evaluate_wv, load_embedding_from_h5
from src.scripts.train_eval.utils import build_data_and_streams, compute_metrics

from numpy.random import seed
from numpy.random import RandomState
from tensorflow import set_random_seed


def eval_model():
    results_dict = {}

    with open(os.path.join('results', args.model_name, 'config.json'), 'r') as f:
        config = json.load(f)

    seed(config["seed"])
    set_random_seed(config["seed"])
    rng = RandomState(config["seed"])

    # To evaluate on more streams, add them here
    # config["batch_size"][stream] = ...

    data_and_streams = build_data_and_streams(config, rng)
    model = build_model(config, data_and_streams["data"])

    # Restore the best model found during validation
    model.load_weights(os.path.join('results', args.model_name, "best_model.h5"))

    metrics = compute_metrics(config, model, data_and_streams,
                              eval_streams=["dev", "test"])

    results_dict['accuracies'] = {}
    results_dict['losses'] = {}
    for stream_name, stream_metrics in metrics.items():
        loss, accuracy = stream_metrics
        print('{} loss / accuracy = {:.4f} / {:4f}'.format(stream_name, loss, accuracy))
        results_dict['accuracies'][stream_name] = accuracy
        results_dict['losses'][stream_name] = loss

    if args.embedding_name is not None:
        _, _, wv = load_embedding_from_h5(args.embedding_name)
        results_dict['backup'] = evaluate_wv(wv, simlex_only=False)

    model_name = args.model_name.split('/')[0]

    with open('results/%s/retrofitting_results.json' % model_name, 'w') as f:
        json.dump(results_dict, f)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-name", type=str)
    parser.add_argument("--embedding-name", type=str)

    args = parser.parse_args()
    eval_model()
