#!/usr/bin/python
# -*- coding: utf-8 -*-

import json
import logging
import math
import numpy as np
import os
import pdb
import random
import re
import string
import tensorflow as tf
tf.logging.set_verbosity(logging.WARN)

from ml import base as mlbase
from ml import scoring
from pytils import adjutant, check
from pytils.log import user_log


LOSS = "loss"
PERPLEXITY = "perplexity"


class Model:
    def __init__(self, scope="model"):
        self.scope = scope

    def evaluate(self, x, handle_unknown=False):
        raise NotImplementedError()

    def test(self, xys, debug=False, score_fns=[], include_loss=False):
        total_scores = {name: 0 for name, function in score_fns}

        if include_loss:
            total_scores[LOSS] = 0.0

        count = 0
        case_slot_length = len(str(len(xys) if hasattr(xys, "__len__") else 1000000))
        case_template = "{{Case {:%dd}}}" % case_slot_length
        batch = []
        cases = []
        total_loss = 0.0

        for case, xy in enumerate(xys):
            count += 1
            batch += [xy]
            cases += [case]

            if len(batch) == 100:
                for key, score in self._invoke(batch, cases, debug, score_fns, include_loss).items():
                    total_scores[key] += score

                batch = []
                cases = []

        if len(batch) > 0:
            for key, score in self._invoke(batch, cases, debug, score_fns, include_loss).items():
                total_scores[key] += score

        #logging.info("Tested on %d instances." % count)
        # We count (rather then using len()) in case the xys come from a stream.
        #                         v
        out = {key: score / float(count) for key, score in total_scores.items()}

        if include_loss:
            out[PERPLEXITY] = math.exp(out[LOSS])

        return out

    def _invoke(self, batch, cases, debug, score_fns, include_loss):
        results, total_loss = self.evaluate(batch, True)
        scores = {name: 0 for name, function in score_fns}

        if include_loss:
            scores[LOSS] = total_loss

        if len(score_fns) > 0:
            for i, case in enumerate(cases):
                if debug:
                    logging.debug("[%s] %s" % (self.scope, case_template.format(case)))

                for name, function in score_fns:
                    passed, score = function(batch[i], results[i])
                    scores[name] += score

                    if debug:
                        if passed:
                            logging.debug("  Passed '%s' (%.4f)!\n  Full correctly predicted output: '%s'." % (name, score, results[i].prediction()))
                        else:
                            logging.debug("  Failed '%s' (%.4f)!\n  Expected: %s\n  Predicted: %s" % (name, score, str(results[i].y), str(results[i].prediction())))

        return scores


class TfModel(Model):
    def __init__(self, scope):
        super(TfModel, self).__init__(scope)

    def placeholder(self, name, shape, dtype=tf.float32):
        return tf.placeholder(dtype, shape, name=name)

    def variable(self, name, shape, initial=None):
        with tf.variable_scope(self.scope):
            return tf.get_variable(name, shape=shape,
                initializer=tf.contrib.layers.xavier_initializer() if initial is None else tf.constant_initializer(initial))


class Ffnn(TfModel):
    def __init__(self, scope, hyper_parameters, extra, input_field, output_labels):
        super(Ffnn, self).__init__(scope)
        self.hyper_parameters = check.check_instance(hyper_parameters, HyperParameters)
        self.extra = extra
        self.input_field = check.check_instance(input_field, mlbase.Field)
        self.output_labels = check.check_instance(output_labels, mlbase.Labels)

        batch_size_dimension = None

        # Notation:
        #   _p      placeholder
        #   _c      constant

        # Base variable setup
        self.input_p = self.placeholder("input_p", [batch_size_dimension, len(self.input_field)])
        self.output_p = self.placeholder("output_p", [batch_size_dimension], tf.int32)

        if self.hyper_parameters.layers > 0:
            self.E = self.variable("E", [len(self.input_field), self.hyper_parameters.width])
            self.E_bias = self.variable("E_bias", [1, self.hyper_parameters.width], 0.)

            self.Y = self.variable("Y", [self.hyper_parameters.width, len(self.output_labels)])
            self.Y_bias = self.variable("Y_bias", [1, len(self.output_labels)], 0.)

            # The E layer is the first layer.
            if self.hyper_parameters.layers > 1:
                self.H = self.variable("H", [self.hyper_parameters.layers - 1, self.hyper_parameters.width, self.hyper_parameters.width])
                self.H_bias = self.variable("H_bias", [self.hyper_parameters.layers - 1, 1, self.hyper_parameters.width], 0.)

            # Computational graph encoding
            self.embedded_input = tf.tanh(tf.matmul(self.input_p, self.E) + self.E_bias)
            mlbase.assert_shape(self.embedded_input, [batch_size_dimension, self.hyper_parameters.width])
            hidden = self.embedded_input
            mlbase.assert_shape(hidden, [batch_size_dimension, self.hyper_parameters.width])

            for l in range(self.hyper_parameters.layers - 1):
                hidden = tf.tanh(tf.matmul(hidden, self.H[l]) + self.H_bias[l])
                mlbase.assert_shape(hidden, [batch_size_dimension, self.hyper_parameters.width])

            mlbase.assert_shape(hidden, [batch_size_dimension, self.hyper_parameters.width])
        else:
            self.Y = self.variable("Y", [len(self.input_field), len(self.output_labels)])
            self.Y_bias = self.variable("Y_bias", [1, len(self.output_labels)], 0.)

            # Computational graph encoding
            hidden = self.input_p
            mlbase.assert_shape(hidden, [batch_size_dimension, len(self.input_field)])

        self.output_logit = tf.matmul(hidden, self.Y) + self.Y_bias
        mlbase.assert_shape(self.output_logit, [batch_size_dimension, len(self.output_labels)])
        self.output_distributions = tf.nn.softmax(self.output_logit)
        mlbase.assert_shape(self.output_distributions, [batch_size_dimension, len(self.output_labels)])
        #self.cost = tf.reduce_mean(tf.nn.nce_loss(
        #    weights=tf.transpose(self.Y),
        #    biases=self.Y_bias,
        #    labels=self.output_p,
        #    inputs=hidden,
        #    num_sampled=1,
        #    num_classes=len(self.output_labels)))
        loss_fn = tf.nn.sparse_softmax_cross_entropy_with_logits
        self.cost = tf.reduce_sum(loss_fn(labels=tf.stop_gradient(self.output_p), logits=self.output_logit))
        self.updates = tf.train.AdamOptimizer().minimize(self.cost)

        self.session = tf.Session()
        self.session.run(tf.global_variables_initializer())

    def train(self, xys_stream, training_parameters):
        check.check_instance(training_parameters, mlbase.TrainingParameters)
        slot_length = len(str(training_parameters.epochs())) - 1
        epoch_template = "[%s] Epoch {:%dd}: {:f}" % (self.scope, slot_length)
        final_loss = None
        epochs_tenth = max(1, int(training_parameters.epochs() / 10))
        losses = training_parameters.losses()
        finished = False
        epoch = -1

        while not finished:
            epoch += 1
            epoch_loss = 0
            # Start at a different offset for every epoch to help avoid overfitting.
            offset = random.randint(0, training_parameters.batch() - 1)
            batch = []
            first = True
            batch_set = False
            count = 0

            for xy in xys_stream():
                batch += [xy]

                if first and len(batch) == offset:
                    first = False
                    batch_set = True
                elif len(batch) == training_parameters.batch():
                    batch_set = True

                if batch_set:
                    count += len(batch)
                    xs = [self.input_field.vector_encode(xy.x, True) for xy in batch]
                    ys = [self.output_labels.encode(xy.y, True) for xy in batch]
                    feed = {
                        self.input_p: xs,
                        self.output_p: ys,
                    }
                    _, training_loss = self.session.run([self.updates, self.cost], feed_dict=feed)
                    epoch_loss += training_loss
                    batch_set = False
                    batch = []

            if len(batch) > 0:
                count += len(batch)
                xs = [self.input_field.vector_encode(xy.x, True) for xy in batch]
                ys = [self.output_labels.encode(xy.y, True) for xy in batch]
                feed = {
                    self.input_p: xs,
                    self.output_p: ys,
                }
                _, training_loss = self.session.run([self.updates, self.cost], feed_dict=feed)
                epoch_loss += training_loss

            epoch_loss /= count
            losses.append(epoch_loss)
            finished, reason = training_parameters.finished(epoch, losses)

            if not finished and epoch % epochs_tenth == 0:
                logging.debug(epoch_template.format(epoch, epoch_loss))

                if training_parameters.debug():
                    # Run the training data and compare the network's output with that of what is expected.
                    self.test(xys)

        logging.debug(epoch_template.format(epoch, epoch_loss))
        logging.debug("Training on %d instances finished due to %s (%s)." % (count, reason, losses))
        return epoch_loss

    def evaluate(self, batch, handle_unknown=False):
        xs = [self.input_field.vector_encode(xy.x, handle_unknown) for xy in batch]
        ys = [self.output_labels.encode(xy.y, True) for xy in batch]
        feed = {
            self.input_p: xs,
            self.output_p: ys,
        }

        distributions, loss = self.session.run([self.output_distributions, self.cost], feed_dict=feed)

        if isinstance(batch, list):
            return [mlbase.Result(self.output_labels, distribution) for distribution in distributions], loss
        else:
            return mlbase.Result(self.output_labels, distributions[0]), loss

    def load_parameters(self, model_dir):
        model = tf.train.get_checkpoint_state(model_dir)
        assert model is not None, "No saved model in '%s'." % model_dir
        saver = tf.train.Saver(tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, scope=self.scope))
        saver.restore(self.session, model.model_checkpoint_path)

    def save_parameters(self, model_dir):
        if os.path.isfile(model_dir) or (model_dir.endswith("/") and os.path.isfile(os.path.dirname(model_dir))):
            raise ValueError("model_dir '%s' must not be a file." % model_dir)

        os.makedirs(model_dir, exist_ok=True)
        model_file = os.path.join(model_dir, "basename")
        saver = tf.train.Saver(tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, scope=self.scope))
        saver.save(self.session, model_file)


class SwitchFfnn(Model):
    def __init__(self, scope, hyper_parameters, extra, case_field, statement_field, output_labels):
        super(SwitchFfnn, self).__init__(scope)
        self.hyper_parameters = check.check_instance(hyper_parameters, HyperParameters)
        self.extra = extra
        self.case_field = check.check_instance(case_field, mlbase.Labels)
        self.statement_field = check.check_instance(statement_field, mlbase.Field)
        self.output_labels = check.check_instance(output_labels, mlbase.Labels)
        self.cases = []
        self.case_encodings = []

        for case, encoding in sorted(self.case_field.encoding().items(), key=lambda item: item[1]):
            self.cases += [case]
            self.case_encodings += [encoding]

        self.ffnns = [Ffnn(scope + "/" + case, hyper_parameters, extra, self.statement_field, output_labels) for case in self.cases]

    def train(self, xys_streams, training_parameters):
        def sub_stream(stream, encoding):
            def stream_fn():
                for xy in stream():
                    case, *statement = xy.x

                    if self.case_field.encode(case) == encoding:
                        yield mlbase.Xy(statement, xy.y)

            return stream_fn

        loss = 0
        count = 0

        for case, xys_stream in xys_streams.items():
            count += 1
            encoding = self.case_field.encode(case)
            loss += self.ffnns[encoding].train(xys_stream, training_parameters)

        return loss / count

    def evaluate(self, batch, handle_unknown=False):
        cased_batches = {}
        mapping = {i: {} for i in range(len(self.case_field))}

        for i, xy in enumerate(batch):
            case, *statement = xy.x
            encoding = self.case_field.encode(case)

            if encoding not in cased_batches:
                cased_batches[encoding] = []

            mapping[encoding][len(cased_batches[encoding])] = i
            cased_batches[encoding] += [xy]

        mapped_results = [None for i in range(len(batch))]
        total_loss = 0

        for encoding, cased_batch in cased_batches.items():
            results, loss = self.ffnns[encoding].evaluate(cased_batch, handle_unknown)
            total_loss += loss

            for i, result in enumerate(results):
                mapped_results[mapping[encoding][i]] = result

        assert not any([r is None for r in results])
        return mapped_results, total_loss / len(cased_batches)

    def load_parameters(self, model_dir):
        for encoding in self.case_encodings:
            self.ffnns[encoding].load_parameters(os.path.join(model_dir, str(encoding)))

    def save_parameters(self, model_dir):
        for encoding in self.case_encodings:
            self.ffnns[encoding].save_parameters(os.path.join(model_dir, str(encoding)))


class CustomOutput(Model):
    def __init__(self, scope, output_labels, output_distribution):
        super(CustomOutput, self).__init__(scope)
        self.output_labels = check.check_instance(output_labels, mlbase.Labels)
        self.output_distribution = check.check_pdist(output_distribution)
        assert len(self.output_labels) == len(self.output_distribution), "%d != %d" % (len(self.output_labels), len(self.output_distribution))

    def evaluate(self, batch, handle_unknown=False):
        if isinstance(batch, list):
            return [mlbase.Result(self.output_labels, self.output_distribution) for i in range(len(batch))], None
        else:
            return mlbase.Result(self.output_labels, self.output_distribution), None


class HyperParameters:
    def __init__(self, layers, width):
        self.layers = layers
        self.width = width

    def __repr__(self):
        return "HyperParameters{l=%d, w=%d}" % (self.layers, self.width)

