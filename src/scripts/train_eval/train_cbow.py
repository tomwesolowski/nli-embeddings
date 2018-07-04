#!/usr/bin/env python
"""
Trains a simple baseline on SNLI
Run like: python src/scripts/train_cbow.py cc840 results/test_run
"""


import logging
import os

import pandas as pd
from keras.utils import np_utils

from src import DATA_DIR
from src.configs.cbow import baseline_configs
from src.models import build_model
from src.util import calculate_spectral_norm
from src.util.data import SNLIData
from src.util.training_loop import baseline_training_loop
from src.util.vegab import main, MetaSaver, AutomaticNamer
import matplotlib
from keras import optimizers
matplotlib.use('Agg')
import matplotlib.pyplot as plt

logger = logging.getLogger(__name__)


def train_model(config, save_path):

    # Load data
    logger.info("Loading data for training...")

    if config["dataset"]["name"] == "snli":
        data = SNLIData(os.path.join(DATA_DIR, "snli"), "snli")
    elif config["dataset"]["name"] == "mnli":
        data = SNLIData(os.path.join(DATA_DIR, "mnli"), "mnli")
    else:
        raise NotImplementedError('Dataset not supported: ' + config["dataset"]["name"])

    train = data.get_stream("train", batch_size=config["batch_size"])
    dev = data.get_stream("dev", batch_size=config["batch_size"])
    test = data.get_stream("test", batch_size=config["batch_size"])
    # need different stream format

    def modified_stream(stream):
        def _stream():
            while True:
                it = stream.get_epoch_iterator()
                for x1, _, x2, _, y in it:
                    yield [x1, x2], np_utils.to_categorical(y, 3)
        return _stream

    stream_train = modified_stream(train)()
    stream_dev = modified_stream(dev)()
    stream_test = modified_stream(test)()

    # Load model
    model, embedding_matrix, statistics = build_model(config)

    spectral_norm = list(calculate_spectral_norm(embedding_matrix))
    stats = [['spectral norm: '], spectral_norm,
             ["Vectors from emb in vocab/ out of vocab"], statistics]
    my_df = pd.DataFrame(stats)
    my_df.to_csv(os.path.join(save_path, 'statistics.xlsx'), index=False, header=False)

    if config["optimizer"] == 'rmsprop':
        model.compile(optimizer=optimizers.RMSprop(lr=config["learning_rate"]),
                      loss='categorical_crossentropy', metrics=['accuracy'])

    # Call training loop
    baseline_training_loop(model=model, train=stream_train, test=stream_test, dev=stream_dev,
                           save_path=save_path, early_stopping=config["early_stopping"],
                           n_epochs=config["n_epochs"], batch_size=config["batch_size"],
                           config=config)

    # Restore the best model found during validation
    model.load_weights(os.path.join(save_path, "best_model.h5"))

    dev_metrics = model.evaluate_generator(stream_dev, 9842 / config["batch_size"])
    logger.info('Dev loss / dev accuracy = {:.4f} / {:4f}'.format(dev_metrics[0], dev_metrics[1]))
    test_metrics = model.evaluate_generator(stream_test, 9824 / config["batch_size"])
    logger.info('Test loss / test accuracy = {:.4f} / {:.4f}'.format(test_metrics[0], test_metrics[1]))


if __name__ == "__main__":
    main(baseline_configs, train_model,
         plugins=[MetaSaver(), AutomaticNamer(namer="timestamp_namer")])
