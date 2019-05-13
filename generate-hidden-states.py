
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
from nnwd import sequential
from nnwd import states
from nnwd import parameters
from nnwd import pickler
from nnwd import rnn
from nnwd import view

from pytils.log import setup_logging, user_log


def main(argv):
    ap = ArgumentParser(prog="generate-hidden-states")
    ap.add_argument("--verbose", "-v", default=False, action="store_true", help="Turn on verbose logging.")
    ap.add_argument("-s", "--sample-rates", type=float, default=0.1, nargs=2, help="train then test sampling rates.")
    ap.add_argument("-d", "--dry-run", default=False, action="store_true")
    ap.add_argument("data_dir")
    ap.add_argument("sequential_dir")
    ap.add_argument("states_dir")
    aargs = ap.parse_args(argv)
    setup_logging(".%s.log" % os.path.splitext(os.path.basename(__file__))[0], aargs.verbose, False, True, True)
    logging.debug(aargs)

    if isinstance(aargs.sample_rates, list):
        sample_rate_train = aargs.sample_rates[0]
        sample_rate_test = aargs.sample_rates[1]
    else:
        sample_rate_train = aargs.sample_rates
        sample_rate_test = aargs.sample_rates

    if aargs.dry_run:
        dry_run(data.stream_train(aargs.data_dir), sample_rate_train, is_train=True)
        dry_run(data.stream_test(aargs.data_dir), sample_rate_test, is_train=False)
        return 0

    rnn = sequential.model_for(aargs.data_dir)
    sequential.load_model(rnn, aargs.sequential_dir)
    description = data.get_description(aargs.data_dir)

    if description.task == data.LM:
        annotation_fn = lambda y, i: y[i][0]
    else:
        annotation_fn = lambda y, i: y

    threads1 = elicit_hidden_states(rnn, data.stream_train(aargs.data_dir), annotation_fn, sample_rate_train, aargs.states_dir, is_train=True)
    threads2 = elicit_hidden_states(rnn, data.stream_test(aargs.data_dir), annotation_fn, sample_rate_test, aargs.states_dir, is_train=False)

    # Technically, we don't need to wait on these threads (they will keep the program alive until complete).
    # But this way it is more clear what is going on.
    for thread in threads1 + threads2:
        thread.join()

    return 0


def elicit_hidden_states(rnn, xys, annotation_fn, sample_rate, states_dir, is_train):
    hidden_states = {}
    threads = []

    for key in view.keys():
        threads.append(start_queue(hidden_states, states_dir, is_train, key))

    total = 0
    sampled = 0
    instances = 0

    for j, xy in enumerate(xys):
        total += 1

        if random.random() <= sample_rate:
            sampled += 1
            instances += len(xy.x)
            stepwise_rnn = rnn.stepwise(handle_unknown=True)

            for i, word_pos in enumerate(xy.x):
                # Set the annotation to that which the rnn has been trained against, not the actual learned annotation (which will be fixed).
                # For example, consider the two training examples: "the little prince" -> "was" and "the little prince" -> "is".
                # We need predictor samples for both "was" and "is", but if we use the actual rnn annotation this will fixate on just one of these.
                annotation = annotation_fn(xy.y, i)
                result, instruments = stepwise_rnn.step(word_pos[0], view.INSTRUMENTS)

                for part, layer in view.part_layers():
                    hidden_states[view.encode_key(part, layer)].put((word_pos[0], tuple(instruments[part][layer]), annotation))

    # Mark the queue as finished.
    for value in hidden_states.values():
        value.put(None)

    prefix = "Train" if is_train else "Test"
    user_log.info("%s %.4f: %d sentences sampled down to %d, eliciting %d hidden states (per part-layer)." % (prefix, sample_rate, total, sampled, instances))
    return threads


def dry_run(xys, sample_rate, is_train):
    total = 0
    sampled = 0
    instances = 0

    for j, xy in enumerate(xys):
        total += 1

        if random.random() <= sample_rate:
            sampled += 1
            instances += len(xy.x)

    prefix = "Train" if is_train else "Test"
    user_log.info("(dry run) %s %.4f: %d sentences sampled down to %d, eliciting %d hidden states (per part-layer)." % (prefix, sample_rate, total, sampled, instances))


def start_queue(hidden_states, states_dir, is_train, key):
    states_queue = queue.Queue()
    hidden_states[key] = states_queue
    return states.set_hidden_states(states_dir, is_train, key, states_queue)


if __name__ == "__main__":
    ret = main(sys.argv[1:])
    sys.exit(ret)

