import functools
import h5py
import json
import keras.backend as K
import numpy as np
import os
import tensorflow as tf
import warnings

from keras.layers import *
from keras.layers.recurrent import LSTM, LSTMCell

from keras.layers.embeddings import Embedding
from keras.layers import Add, Subtract, Dense, Dropout, Input, TimeDistributed, Lambda, Bidirectional, \
    Dot, Permute, Multiply, Concatenate, Activation, LSTM
from keras.regularizers import l2

from src import DATA_DIR
from src.models.elmo_lstm import LSTMCellWithClippingAndProjection, LSTMWithClippingAndProjection

DTYPE = 'float32'


class ElmoEmbeddings(Layer):
    def __init__(self, config, all_stages=[], **kwargs):
        super(ElmoEmbeddings, self).__init__(**kwargs)

        self.config = config
        self.elmo_dir = os.path.join(DATA_DIR, config['elmo_dir'])

        options_file = os.path.join(self.elmo_dir, 'options.json')
        with open(options_file, 'r') as fin:
            self.options = json.load(fin)

        self._elmo_embeddings = dict()

        self.all_stages = all_stages
        self.num_layers = 2
        self.lstm_dim = self.options['lstm']['dim']
        self.projection_dim = self.options['lstm']['projection_dim']
        self.n_lstm_layers = self.options['lstm'].get('n_layers', 1)
        self.cell_clip = self.options['lstm'].get('cell_clip')
        self.proj_clip = self.options['lstm'].get('proj_clip')
        self.use_skip_connections = self.options['lstm']['use_skip_connections']
        self.use_weighted_embeddings = self.config.get('elmo_use_weighted_embeddings', True)
        self.use_layer_normalization = self.config.get('elmo_use_layer_normalization', True)

        print("Elmo layer normalization:", self.use_layer_normalization)

        self.embedding_weight_file = os.path.join(self.elmo_dir, 'elmo_token_embeddings.hdf5')
        self.weight_file = os.path.join(self.elmo_dir, 'lm_weights.hdf5')

        self.initial_embeddings_weights = np.array(
            self.config.get('elmo_initial_embeddings_weights', np.zeros(self.num_layers + 1)))
        self.initial_gamma = np.array(self.config.get('elmo_initial_gamma', [1]))

        with h5py.File(self.embedding_weight_file, 'r') as fin:
            self.vocab_size, self.embedding_dim = fin['embedding'].shape

    @property
    def trainable_weights(self):
        if not self.trainable:
            return []
        trainables = list(self._trainable_weights)
        for sublayer in self.sublayers:
            trainables.extend(sublayer.trainable_weights)
        return trainables

    def get_embeddings(self, stage, name):
        dict_name = '%s_%s' % (stage, name)
        if dict_name not in self._elmo_embeddings:
            raise ValueError('Unknown stage: %s' % stage)
        return self._elmo_embeddings[dict_name]

    @property
    def non_trainable_weights(self):
        if not self.trainable:
            nontrainables = list(self._trainable_weights + self._non_trainable_weights)
        else:
            nontrainables = list(self._non_trainable_weights)
        for sublayer in self.sublayers:
            if not self.trainable:
                nontrainables.extend(sublayer.weights)
            else:
                nontrainables.extend(sublayer.trainable_weights)
        return nontrainables

    def compute_output_shape(self, input_shapes):
        output_dim = 2*(self.projection_dim or self.lstm_dim)
        return input_shapes[0] + (output_dim,)

    def _custom_getter(self, getter, name, *args, **kwargs):
        print("_custom_getter:", name)
        kwargs['trainable'] = False
        # kwargs['initializer'] = self._pretrained_initializer(
        #     name, self.weight_file, self.embedding_weight_file
        # )
        return getter(name, *args, **kwargs)

    def _load_embedding_weights(self, shape):
        print("loading embeddings...")
        with h5py.File(self.embedding_weight_file, 'r') as fin:
            # Have added a special 0 index for padding not present
            # in the original model.
            embed_weights = fin['embedding'][...]
            self.embedding_dim = embed_weights.shape[1]
            weights = np.zeros(
                (embed_weights.shape[0] + 1, self.embedding_dim),
                dtype='float32'
            )
            weights[1:, :] = embed_weights
        return weights

    def _load_lstm_weights(self, i_layer, i_dir, weight):
        with h5py.File(self.weight_file, 'r') as fin:
            prefix = 'RNN_%d/RNN/MultiRNNCell/Cell%d/LSTMCell/' % (i_dir, i_layer)
            weights = {
                'kernel': fin[prefix + 'W_0'][:self.embedding_dim],  # kernel
                'recurrent': fin[prefix + 'W_0'][self.embedding_dim:],  # recurrent kernel
                'projection': fin[prefix + 'W_P_0'][...],  # projection
                'bias': fin[prefix + 'B'][...]  # bias
            }

            def _initializer(shape):
                return K.variable(weights[weight], dtype=DTYPE)

            return _initializer

    def _embedding_weights_initializer(self, shape):
        assert shape == self.initial_embeddings_weights.shape
        return K.variable(self.initial_embeddings_weights, dtype=DTYPE)

    def _gamma_initializer(self, shape):
        assert shape == self.initial_gamma.shape
        return K.variable(self.initial_gamma, dtype=DTYPE)

    def _print_weights_shapes(self):
        for weight in self.lstms['forward'][0].get_weights():
            print(weight.shape)

    def build(self, input_shapes):
        self.sublayers = []

        with tf.variable_scope('', custom_getter=self._custom_getter):
            self.embed = Embedding(self.vocab_size + 1,
                                   self.embedding_dim,
                                   trainable=False,
                                   embeddings_initializer=self._load_embedding_weights,
                                   mask_zero=True)
            self.sublayers.append(self.embed)
            self.lstms = {}

            for i_dir, direction in enumerate(['forward', 'backward']):
                self.lstms[direction] = []
                for i_layer in range(self.num_layers):
                    lstm_initializer = functools.partial(self._load_lstm_weights, i_layer, i_dir)
                    lstm = LSTMWithClippingAndProjection(
                            recurrent_initializer=lstm_initializer('recurrent'),
                            bias_initializer=lstm_initializer('bias'),
                            kernel_initializer=lstm_initializer('kernel'),
                            projection_initializer=lstm_initializer('projection'),
                            recurrent_activation='sigmoid',
                            units=self.lstm_dim,
                            return_sequences=True,
                            cell_clip=self.cell_clip,
                            projection_dim=self.projection_dim,
                            proj_clip=self.proj_clip,
                            go_backwards=direction == 'backward',
                            unit_forget_bias=False,
                            trainable=False
                    )
                    self.lstms[direction].append(lstm)
                    self.sublayers.append(lstm)

        self.embeddings_weights = {}
        self.gammas = {}

        for stage in self.all_stages:
            if self.use_weighted_embeddings:
                self.embeddings_weights[stage] = self.add_weight(
                    name='{}_ELMo_W'.format(stage),
                    shape=(self.num_layers + 1,),
                    initializer=self._embedding_weights_initializer,
                    regularizer=l2(self.config['l2_elmo_regularization']),
                    trainable=True,
                )
            self.gammas[stage] = self.add_weight(
                name='{}_ELMo_gamma'.format(stage),
                shape=(1,),
                initializer=self._gamma_initializer,
                regularizer=l2(self.config['l2_elmo_regularization']),
                trainable=True,
            )

        self.residual_connection = Lambda(lambda x: x[0] + x[1])
        self.concat_layers = Concatenate(name='concat_layers', axis=1)
        self.expand_forwards = []
        self.expand_backwards = []
        self.concat_directions = []

        self.norm_weights = Lambda(
            lambda weights: tf.nn.softmax(weights))

        self.expand_normed_weights = Lambda(
            lambda x: K.expand_dims(K.expand_dims(K.expand_dims(x)), axis=0))

        self.normalize_embeddings = Lambda(lambda x: self._normalize_embeddings(x[0], x[1]))
        self.weight_embeddings = Multiply()
        self.add_embeddings = Lambda(lambda x: K.sum(x, axis=1))
        self.gamma_multiply = Lambda(lambda x: x[0] * x[1])

        self.reverse_sequence = Lambda(lambda x: x[:, ::-1, :])
        self.mask_sequence = Lambda(lambda x: x[0] * x[1])

        for i in range(self.num_layers + 1):
            self.expand_forwards.append(
                Lambda(lambda x: K.expand_dims(x, axis=1),
                   name='expand_forward_%d' % i))

            self.expand_backwards.append(
                Lambda(lambda x: K.expand_dims(x, axis=1),
                       name='expand_backward_%d' % i))

            self.concat_directions.append(
                Concatenate(name='concat_directions_%d' % i, axis=-1)
            )

        self.built = True

    def _get_embeddings(self, inputs, mask):
        input_embeddings = inputs
        input_embeddings = self.embed(input_embeddings)

        elmo_embeddings = {}
        for direction in ['forward', 'backward']:
            input = input_embeddings
            elmo_embeddings[direction] = [input]
            for i in range(self.num_layers):
                output = self.lstms[direction][i](input, mask=mask)  # [-1, maxlen, projection_dim]
                if direction == 'backward':
                    output = self.reverse_sequence(output)
                if i > 0 and self.use_skip_connections:
                    # Residual connection between between first hidden and output layer.
                    input = self.residual_connection([input, output])
                else:
                    input = output
                elmo_embeddings[direction].append(input)  # [-1, maxlen, projection_dim]
                # input = self.mask_sequence([input, mask])

        elmo_embeddings_both = []
        for i, (forward_layer, backward_layer) in enumerate(
                zip(elmo_embeddings['forward'], elmo_embeddings['backward'])):
            forward_layer_exp = self.expand_forwards[i](forward_layer)  # [-1, 1, maxlen, projection_dim]
            backward_layer_exp = self.expand_backwards[i](backward_layer)
            elmo_embeddings_both.append(
                # [-1, 1, maxlen, 2*projection_dim]
                self.concat_directions[i]([forward_layer_exp, backward_layer_exp])
            )

        # [-1, num_layers, maxlen, 2*projection_dim]
        return self.concat_layers(elmo_embeddings_both)

    def _normalize_embeddings(self, all_embeddings, mask):
        # embeddings: [-1, num_layers+1, maxlen, 2*proj_dim]
        # mask: [-1, maxlen, 1]

        separate_embeddings = tf.split(all_embeddings, self.num_layers + 1, axis=1)
        means = []
        variances = []

        for x in separate_embeddings:
            x = tf.squeeze(x, axis=1)
            x_masked = x * mask  # [-1, maxlen, 1024]
            num_mask = tf.reduce_sum(mask, axis=[1, 2])  # [-1]
            mean = tf.reduce_sum(x_masked, axis=[1, 2]) / num_mask  # [-1]
            mean = K.expand_dims(K.expand_dims(mean))  # [-1, 1, 1]
            variance = tf.reduce_sum(((x_masked - mean) * mask) ** 2, axis=[1, 2]) / num_mask
            variance = K.expand_dims(K.expand_dims(variance))  # [-1, 1, 1]
            means.append(mean)
            variances.append(variance)

        return tf.nn.batch_normalization(
            all_embeddings, tf.stack(means, axis=1), tf.stack(variances, axis=1), None, None, 1E-12
        )

    def _weight_embeddings(self, embeddings, mask, stage):
        normed_weights = self.norm_weights(self.embeddings_weights[stage])
        # [-1, -1, layers, 1]
        normed_weights_exp = self.expand_normed_weights(normed_weights)

        weighted_embeddings = self.weight_embeddings([normed_weights_exp, embeddings])
        weighted_embeddings = self.add_embeddings(weighted_embeddings)

        return weighted_embeddings

    def call(self, inputs, stage, name, **kwargs):
        # TODO(chledows): add charcnn and finetuning.
        # TODO(tomwesolowski): After it's done, check if embeddings are the same as in dumped embeddings file.
        embeddings, mask = inputs

        elmo_embeddings = self._get_embeddings(embeddings, mask)

        if self.use_layer_normalization:
            elmo_embeddings = self.normalize_embeddings([elmo_embeddings, mask])

        self._elmo_embeddings['%s_%s' % (stage, name)] = elmo_embeddings

        if self.use_weighted_embeddings:
            weighted_embeddings = self._weight_embeddings(elmo_embeddings, mask, stage)
        else:
            weighted_embeddings = Lambda(lambda x: K.sum(x, axis=1))(elmo_embeddings)

        gamma = self.gammas[stage]
        weighted_embeddings = self.gamma_multiply([gamma, weighted_embeddings])

        return weighted_embeddings


