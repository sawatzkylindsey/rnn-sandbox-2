
from argparse import ArgumentParser
import collections
from csv import writer as csv_writer
import glob
import logging
import os
import pdb
import queue
import random
import sys

from nnwd import data
from nnwd.domain import NeuralNetwork
from nnwd import parameters
from nnwd import pickler
from nnwd import rnn

from pytils.log import setup_logging, user_log


#part_keys = {"%s-%d" % (part, layer): [] for layer in range(NeuralNetwork.LAYERS) for part in NeuralNetwork.LSTM_PARTS}
#part_keys["embedding-0"] = []
embedding_padding = tuple([0] * max(0, NeuralNetwork.HIDDEN_WIDTH - NeuralNetwork.EMBEDDING_WIDTH))
hidden_padding = tuple([0] * max(0, NeuralNetwork.EMBEDDING_WIDTH - NeuralNetwork.HIDDEN_WIDTH))


def main(argv):
    ap = ArgumentParser(prog="generate-hidden-states")
    ap.add_argument("--verbose", "-v", default=False, action="store_true", help="Turn on verbose logging.")
    ap.add_argument("-s", "--sample-rate", type=float, default=0.1)
    ap.add_argument("data_dir")
    ap.add_argument("rnn_dir")
    ap.add_argument("hs_dir")
    aargs = ap.parse_args(argv)
    setup_logging(".%s.log" % os.path.splitext(os.path.basename(__file__))[0], aargs.verbose, False, True, True)

    description = data.get_description(aargs.data_dir)
    words = data.get_words(aargs.data_dir)

    if description.task == data.LM:
        annotation_fn = lambda y, i: y[i][0]
        lstm = rnn.RnnLm(NeuralNetwork.LAYERS, NeuralNetwork.HIDDEN_WIDTH, NeuralNetwork.EMBEDDING_WIDTH, words)
    else:
        outputs = data.get_outputs(aargs.data_dir)
        annotation_fn = lambda y, i: y
        lstm = rnn.RnnSa(NeuralNetwork.LAYERS, NeuralNetwork.HIDDEN_WIDTH, NeuralNetwork.EMBEDDING_WIDTH, words, outputs)

    lstm.load(os.path.join(aargs.rnn_dir, parameters.LSTM))
    threads1 = elicit_hidden_states(lstm, data.stream_train(aargs.data_dir), annotation_fn, aargs.sample_rate, aargs.hs_dir, is_train=True)
    threads2 = elicit_hidden_states(lstm, data.stream_test(aargs.data_dir), annotation_fn, aargs.sample_rate, aargs.hs_dir, is_train=False)

    # Technically, we don't need to wait on these threads (they will keep the program alive until complete).
    # But this way it is more clear what is going on.
    for thread in threads1 + threads2:
        thread.join()

    return 0


def elicit_hidden_states(lstm, xys, annotation_fn, sample_rate, hs_dir, is_train):
    hidden_states = {}
    threads = []
    threads.append(start_queue(hidden_states, "embedding-0", hs_dir, is_train))

    for part in NeuralNetwork.LSTM_PARTS:
        for layer in range(NeuralNetwork.LAYERS):
            key = "%s-%d" % (part, layer)
            threads.append(start_queue(hidden_states, key, hs_dir, is_train))

    total = 0
    sampled = 0
    instances = 0

    for j, xy in enumerate(xys):
        total += 1

        if random.random() <= sample_rate:
            sampled += 1
            stepwise_lstm = lstm.stepwise(handle_unknown=True)

            for i, word_pos in enumerate(xy.x):
                instances += 1
                # Set the annotation to that which the lstm has been trained against, not the actual learned annotation (which will be fixed).
                # For example, consider the two training examples: "the little prince" -> "was" and "the little prince" -> "is".
                # We need predictor samples for both "was" and "is", but if we use the actual lstm annotation this will fixate on just one of these.
                annotation = annotation_fn(xy.y, i)
                result, instruments = stepwise_lstm.step(word_pos[0], NeuralNetwork.INSTRUMENTS)
                x = (tuple(instruments["embedding"]) + embedding_padding)
                hidden_states["embedding-0"].put((x, annotation))

                for part in NeuralNetwork.LSTM_PARTS:
                    for layer in range(NeuralNetwork.LAYERS):
                        point = tuple(instruments[part][layer]) + hidden_padding
                        hidden_states["%s-%d" % (part, layer)].put((point, annotation))

    # Mark the queue as finished.
    for value in hidden_states.values():
        value.put(None)

    prefix = "Train" if is_train else "Test"
    user_log.info("%s: %d sentences sampled down to %d, eliciting %d hidden states (per part-layer)." % (prefix, total, sampled, instances))
    return threads


def start_queue(hidden_states, key, hs_dir, is_train):
    states = queue.Queue()
    hidden_states[key] = states
    return pickler.dump(states, os.path.join(hs_dir, (parameters.STATES_TRAIN if is_train else parameters.STATES_TEST) + "." + key))


if __name__ == "__main__":
    ret = main(sys.argv[1:])
    sys.exit(ret)

