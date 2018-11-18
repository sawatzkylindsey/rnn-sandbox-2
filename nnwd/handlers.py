
import json
import logging
import pdb
import threading

from pytils.log import setup_logging, user_log


class Echo:
    def get(self, data):
        return data


class Words:
    def __init__(self, words):
        self.words = sorted([w for w in words])

    def get(self, data):
        return self.words


class Weights:
    def __init__(self, neural_network):
        self.neural_network = neural_network

    def get(self, data):
        return self.neural_network.weights(data["sequence"])


class WeightExplain:
    def __init__(self, neural_network):
        self.neural_network = neural_network

    def get(self, data):
        name = data["name"][0]
        column = int(data["column"][0])
        #weights = json.loads(data["weights"][0])
        return self.neural_network.weight_explain(name, column)


