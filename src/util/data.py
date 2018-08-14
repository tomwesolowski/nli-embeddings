#!/usr/bin/env python

"""Dataset layout and data preparation.

Currently the following layouts are supported:

- standard
    The training, validation and development data are in
    train.txt, valid.txt and test.txt. All files are read
    sequentially.
- lambada
    Like standard, but the training data is stored in an
    HDF5 file "train.h5". The training data is read randomly
    by taking random spans.

TODO: Unit test SNLI data
"""

import functools
import math
import os

import fuel
import h5py
import numpy
from fuel.datasets import H5PYDataset
from fuel.schemes import IterationScheme, ConstantScheme, ShuffledExampleScheme, SequentialExampleScheme, IndexScheme
from fuel.streams import DataStream
from fuel.transformers import (
    Mapping, Batch, Padding, AgnosticSourcewiseTransformer,
    FilterSources, Transformer, Unpack)

from src.util.vocab import Vocabulary

#from dictlearn.datasets import TextDataset, SQuADDataset, PutTextTransfomer
#from dictlearn.util import str2vec

# We have to pad all the words to contain the same
# number of characters.
MAX_NUM_CHARACTERS = 100


def vectorize(words):
    """Replaces words with vectors."""
    return [str2vec(word, MAX_NUM_CHARACTERS) for word in words]


def listify(example):
    return tuple(list(source) for source in example)


def add_bos(bos, source_data):
    return [bos] + source_data


def add_eos(eos, source_data):
    source_data = list(source_data)
    source_data.append(eos)
    return source_data


class SourcewiseMapping(AgnosticSourcewiseTransformer):
    def __init__(self, data_stream, mapping, *args, **kwargs):
        kwargs.setdefault('which_sources', data_stream.sources)
        super(SourcewiseMapping, self).__init__(
            data_stream, data_stream.produces_examples, *args, **kwargs)
        self._mapping = mapping

    def transform_any_source(self, source_data, _):
        return self._mapping(source_data)


class RandomSpanScheme(IterationScheme):
    requests_examples = True

    def __init__(self, dataset_size, span_size, seed=None):
        self._dataset_size = dataset_size
        self._span_size = span_size
        if not seed:
            seed = fuel.config.default_seed
        self._rng = numpy.random.RandomState(seed)

    def get_request_iterator(self):
        # As for now this scheme produces an infinite stateless scheme,
        # it can itself play the role of an iterator. If we want to add
        # a state later, this trick will not cut it any more.
        return self

    def __iter__(self):
        return self

    def __next__(self):
        start = self._rng.randint(0, self._dataset_size - self._span_size)
        return slice(start, start + self._span_size)


class Data(object):
    """Builds the data stream for different parts of the data.

    TODO: refactor, only leave the caching logic.

    """
    def __init__(self, path, layout, **kwargs):
        self._path = path
        self._layout = layout
        if not self._layout in ['standard', 'lambada', 'squad', 'snli', 'mnli', 'breaking']:
            raise "layout {} is not supported".format(self._layout)

        self._vocab = None
        self._dataset_cache = {}

    @property
    def vocab(self):
        raise NotImplementedError()

    @property
    def part_map(self):
        if self._layout == 'snli':
            part_map = {'train': 'train.h5',
                        'dev': 'dev.h5',
                        'test': 'test.h5'}
        elif self._layout == 'mnli':
            part_map = {'train': 'train.h5',
                        'dev': 'dev.h5',
                        'test': 'dev_mismatched.h5'}
        elif self._layout == 'breaking':
            part_map = {'test': 'test.h5'}
        else:
            raise NotImplementedError('Not implemented layout ' + self._layout)
        return part_map

    def get_dataset_path(self, part):
        return os.path.join(self._path, self.part_map[part])

    def get_dataset(self, part):
        if not part in self._dataset_cache:
            part_path = self.get_dataset_path(part)
            if self._layout == 'lambada' and part == 'train':
                self._dataset_cache[part] = H5PYDataset(part_path, ('train',))
            elif self._layout == 'squad':
                self._dataset_cache[part] = SQuADDataset(part_path, ('all',))
            elif self._layout in ['snli', 'breaking']:
                self._dataset_cache[part] = H5PYDataset(h5py.File(part_path, "r"), \
                    ('all',), sources=('sentence1', 'sentence1_lemmatized',
                                       'sentence2', 'sentence2_lemmatized',
                                       'label',), load_in_memory=True)
            elif self._layout == 'mnli':
                self._dataset_cache[part] = H5PYDataset(h5py.File(part_path, "r"), \
                    ('all',), sources=('sentence1', 'sentence2', 'label',), load_in_memory=True)
            else:
                self._dataset_cache[part] = TextDataset(part_path)
        return self._dataset_cache[part]

    def get_stream(self, *args, **kwargs):
        raise NotImplementedError()


class LanguageModellingData(Data):

    @property
    def vocab(self):
        if not self._vocab:
            self._vocab = Vocabulary(
                os.path.join(self._path, "vocab.txt"))
        return self._vocab

    def get_stream(self, part, batch_size=None, max_length=None, seed=None):
        dataset = self.get_dataset(part)
        if self._layout == 'lambada' and part == 'train':
            stream = DataStream(
                dataset,
                iteration_scheme=RandomSpanScheme(
                    dataset.num_examples, max_length, seed))
            stream = Mapping(stream, listify)
        else:
            stream = dataset.get_example_stream()

        stream = SourcewiseMapping(stream, functools.partial(add_bos, Vocabulary.BOS))
        stream = SourcewiseMapping(stream, vectorize)
        if not batch_size:
            return stream
        stream = Batch(
            stream,
            iteration_scheme=ConstantScheme(batch_size))
        stream = Padding(stream)
        return stream


def select_random_answer(rng, example):
    index = rng.randint(0, len(example['answer_begins']))
    example['answer_begins'] = example['answer_begins'][index]
    example['answer_ends'] = example['answer_ends'][index]
    return example


def retrieve_and_pad_squad(retrieval, example):
    contexts = example['contexts']
    questions = example['questions']
    text = list(contexts) + list(questions)
    defs, def_mask, def_map = retrieval.retrieve_and_pad(text)
    context_defs = def_map[:, 0] < len(contexts)
    contexts_def_map = def_map[context_defs]
    questions_def_map = (
        def_map[numpy.logical_not(context_defs)]
        - numpy.array([len(contexts), 0, 0]))
    return {'defs': defs,
            'def_mask': def_mask,
            'contexts_def_map': contexts_def_map,
            'questions_def_map': questions_def_map}


def retrieve_and_pad_snli(retrieval, example):
    # TODO(kudkudak): We could joint retrieve retrieve_and_pad_squad and retrieve_and_pad_snli
    # this will be done along lookup refactor
    s1, s2, label = example
    assert label.ndim == 1
    text = list(s1) + list(s2)
    defs, def_mask, def_map = retrieval.retrieve_and_pad(text)
    context_defs = def_map[:, 0] < len(s1)
    s1_def_map = def_map[context_defs]
    # Def map is (batch_index, time_step, def_index)
    s2_def_map = (
        def_map[numpy.logical_not(context_defs)]
        - numpy.array([len(s1), 0, 0]))
    return [defs, def_mask, s1_def_map, s2_def_map]


def digitize(vocab, source_data):
    return numpy.array([vocab.encode(words) for words in source_data])


def surround_sentence(vocab, source_data):
    sentences = [[vocab.bos] + words.tolist() + [vocab.eos] for words in source_data]
    return numpy.array(sentences)


def surround_sentence_lemma(vocab, source_data):
    sentences = [[Vocabulary.BOS] + words.tolist() + [Vocabulary.EOS] for words in source_data]
    return numpy.array(sentences)


def shuffle_like_kim(batch_size, rng, batch):
    source_buffer, source_lemma_buffer, target_buffer, target_lemma_buffer, label_buffer = batch

    # sort by target buffer
    tlen = numpy.array([len(t) for t in target_buffer])
    tidx = tlen.argsort(kind='mergesort')
    # tidx = list(range(target_buffer.shape[0]))
    # shuffle mini-batch
    tindex = []
    small_index = numpy.array(list(range(int(math.ceil(len(tidx) * 1. / batch_size)))))
    rng.shuffle(small_index)
    for i in small_index:
        if (i + 1) * batch_size > len(tidx):
            tindex.extend(tidx[i * batch_size:])
        else:
            tindex.extend(tidx[i * batch_size:(i + 1) * batch_size])

    tidx = tindex

    _sbuf = [source_buffer[i] for i in tidx]
    _tbuf = [target_buffer[i] for i in tidx]
    _slbuf = [source_lemma_buffer[i] for i in tidx]
    _tlbuf = [target_lemma_buffer[i] for i in tidx]
    _lbuf = [label_buffer[i] for i in tidx]

    source_buffer = _sbuf
    target_buffer = _tbuf
    source_lemma_buffer = _slbuf
    target_lemma_buffer = _tlbuf
    label_buffer = _lbuf

    return [source_buffer, source_lemma_buffer, target_buffer, target_lemma_buffer, label_buffer]



class ExtractiveQAData(Data):

    def __init__(self, retrieval=None, *args, **kwargs):
        super(ExtractiveQAData, self).__init__(*args, **kwargs)
        self._retrieval = retrieval

    @property
    def vocab(self):
        if not self._vocab:
            with h5py.File(self.get_dataset_path('train')) as h5_file:
                # somehow reading the data before zipping is important
                self._vocab = Vocabulary(list(zip(h5_file['vocab_words'][:],
                                             h5_file['vocab_freqs'][:])))
        return self._vocab

    def get_stream(self, part, batch_size=None, shuffle=False, max_length=None,
                   raw_text=False, q_ids=False, seed=None):
        if not seed:
            seed = fuel.config.default_seed
        rng = numpy.random.RandomState(seed)
        dataset = self.get_dataset(part)
        if shuffle:
            stream = DataStream(
                dataset,
                iteration_scheme=ShuffledExampleScheme(dataset.num_examples, rng=rng))
        else:
            stream = dataset.get_example_stream()
        if not q_ids:
            stream = FilterSources(stream, [source for source in dataset.sources
                                            if source != 'q_ids'])
        stream = PutTextTransfomer(stream, dataset, raw_text=True)
        # <eos> is added for two purposes: to serve a sentinel for coattention,
        # and also to ensure the answer span ends at a token
        eos = self.vocab.EOS
        stream = SourcewiseMapping(stream, functools.partial(add_eos, eos),
                                   which_sources=('contexts', 'questions'))
        stream = Mapping(stream, functools.partial(select_random_answer, rng),
                         mapping_accepts=dict)
        if not batch_size:
            if self._retrieval:
                raise NotImplementedError()
            return stream
        stream = Batch(stream, iteration_scheme=ConstantScheme(batch_size))
        if self._retrieval:
            stream = Mapping(
                stream,
                functools.partial(retrieve_and_pad_squad, self._retrieval),
                mapping_accepts=dict,
                add_sources=('defs', 'def_mask', 'contexts_def_map', 'questions_def_map'))
        if not raw_text:
            stream = SourcewiseMapping(stream, functools.partial(digitize, self.vocab),
                                       which_sources=('contexts', 'questions'))

        stream = Padding(stream, mask_sources=('contexts', 'questions'), mask_dtype='float32')
        return stream

# TODO(kudkudak): Introduce this to Fuel
class FixedMapping(Transformer):
    """Applies a mapping to the data of the wrapped data stream.

    Parameters
    ----------
    data_stream : instance of :class:`DataStream`
        The wrapped data stream.
    mapping : callable
        The mapping to be applied.
    add_sources : tuple of str, optional
        When given, the data produced by the mapping is added to original
        data under source names `add_sources`.

    """
    def __init__(self, data_stream, mapping, add_sources=None, **kwargs):
        super(FixedMapping, self).__init__(
            data_stream, data_stream.produces_examples, **kwargs)
        self.mapping = mapping
        self.add_sources = add_sources

    @property
    def sources(self):
        return self.data_stream.sources + (self.add_sources
                                           if self.add_sources else ())

    def get_data(self, request=None):
        if request is not None:
            raise ValueError
        data = next(self.child_epoch_iterator)
        image = self.mapping(data)
        if not self.add_sources:
            return image
        # This is the fixed line. We need to transform data to list(data) to concatenate the two!
        return tuple(list(data) + image)


class NLIData(Data):
    def __init__(self, fraction_train, *args, **kwargs):
        super(NLIData, self).__init__(*args, **kwargs)
        self._retrieval = None
        self.fraction_train = fraction_train
        if 'vocab_dir' in kwargs:
            self.vocab_dir = kwargs['vocab_dir']
        else:
            self.vocab_dir = self._path

    def set_retrieval(self, retrieval):
        self._retrieval = retrieval

    @property
    def vocab(self):
        if not self._vocab:
            self._vocab = Vocabulary(
                os.path.join(self.vocab_dir, "vocab.txt"))
        return self._vocab

    def num_examples(self, part):
        return int(self.get_dataset(part).num_examples * (
            self.fraction_train if part == 'train' else 1.0
        ))

    def total_num_examples(self, part):
        return self.get_dataset(part).num_examples

    def get_stream(self, part, batch_size, shuffle, rng, raw_text=False):
        d = self.get_dataset(part)
        print(("Dataset with {} examples".format(self.num_examples(part))))
        it = SequentialExampleScheme(
            examples=rng.choice(
                    a=self.total_num_examples(part),
                    size=self.num_examples(part),
                    replace=False))
        # it = SequentialExampleScheme(examples=self.total_num_examples(part))
        stream = DataStream(d, iteration_scheme=it)

        if shuffle:
            stream = Batch(stream, iteration_scheme=ConstantScheme(20 * batch_size))
            stream = FixedMapping(stream, functools.partial(shuffle_like_kim, batch_size, rng))
            stream = Unpack(stream)
        stream = Batch(stream, iteration_scheme=ConstantScheme(batch_size))

        if self._retrieval:
            stream = FixedMapping(
                stream,
                functools.partial(retrieve_and_pad_snli, self._retrieval),
                add_sources=("defs", "def_mask", "sentence1_def_map", "sentence2_def_map")) # This is because there is bug in Fuel :( Cannot concatenate tuple and list

        if not raw_text:
            stream = SourcewiseMapping(stream, functools.partial(digitize, self.vocab),
                                       which_sources=('sentence1', 'sentence2'))
            stream = SourcewiseMapping(stream, functools.partial(surround_sentence, self.vocab),
                                       which_sources=('sentence1', 'sentence2'))
            stream = SourcewiseMapping(stream, functools.partial(surround_sentence_lemma, self.vocab),
                                       which_sources=('sentence1_lemmatized', 'sentence2_lemmatized'))

        stream = Padding(stream, mask_sources=('sentence1', 'sentence2'))  # Increases amount of outputs by x2

        return stream


