"""Offline numerical kernels adapted from Semantica (see the pinned sources below).

MIT License

Copyright (c) 2026 Semantica

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
from __future__ import annotations

from collections import Counter
import random

import numpy as np


_SOURCE = 'https://github.com/semantica-agi/semantica/blob/4d83266ca0d7f70185906bfce7c96ec71c6ebc04/semantica/kg/'
PAGERANK_SOURCE = _SOURCE + 'centrality_calculator.py#L590-L741'
COMMUNITY_SOURCE = _SOURCE + 'community_detector.py#L686-L824'
DAMPING = 0.85
TOLERANCE = 1e-6
MAX_ITERATIONS = 100
COMMUNITY_SEED = 0


def pagerank(outbound: list[list[int]]) -> tuple[list[float], dict]:
    """Semantica's power iteration with uniformly redistributed dangling mass.

    Bincount multiplies the sparse transition matrix without a SciPy dependency
    or a dense N-by-N allocation. Input neighborhoods are unique and ordered.
    """
    size = len(outbound)
    degree = np.asarray([len(targets) for targets in outbound], dtype=np.intp)
    scores = np.full(size, 1.0 / size) if size else np.empty(0)
    iterations, residual = 0, 0.0
    if degree.any():
        sources = np.repeat(np.arange(size), degree)
        targets = np.fromiter((target for group in outbound for target in group),
                              dtype=np.intp, count=len(sources))
        weights = 1.0 / degree[sources]
        dangling = degree == 0
        for iterations in range(1, MAX_ITERATIONS + 1):
            previous = scores
            incoming = np.bincount(targets, weights=weights * previous[sources], minlength=size)
            scores = (1.0 - DAMPING) / size + DAMPING * (incoming + previous[dangling].sum() / size)
            residual = float(np.linalg.norm(scores - previous))
            if residual < TOLERANCE:
                break
    return scores.tolist(), {
        'source': PAGERANK_SOURCE, 'method': 'power_iteration', 'topology': 'directed_unique_neighbors',
        'damping': DAMPING, 'max_iterations': MAX_ITERATIONS, 'tolerance_l2': TOLERANCE,
        'iterations': iterations, 'residual_l2': residual, 'converged': residual < TOLERANCE,
    }


def label_propagation(neighbors: list[list[int]]) -> tuple[list[int], dict]:
    """Semantica's asynchronous label propagation on a simple undirected graph.

    Keep an existing maximal label on ties so ties alone cannot cause an
    endless sequence of relabelings. Use a local RNG and sorted input for
    repeatability without changing the RNG of a concurrent construction worker.
    """
    labels = list(range(len(neighbors)))
    active = [i for i, adjacent in enumerate(neighbors) if adjacent]
    rng = random.Random(COMMUNITY_SEED)
    iterations, changes = 0, 0
    if active:
        for iterations in range(1, MAX_ITERATIONS + 1):
            changes = 0
            rng.shuffle(active)
            for node in active:
                counts = Counter(labels[neighbor] for neighbor in neighbors[node])
                highest = max(counts.values())
                if counts[labels[node]] == highest:
                    continue
                labels[node] = rng.choice([label for label, votes in counts.items() if votes == highest])
                changes += 1
            if not changes:
                break
    return labels, {
        'source': COMMUNITY_SOURCE, 'method': 'asynchronous_label_propagation',
        'topology': 'undirected_unique_nonself_neighbors', 'seed': COMMUNITY_SEED,
        'tie_rule': 'retain_current_else_seeded_choice', 'max_iterations': MAX_ITERATIONS,
        'iterations': iterations, 'changes_last_iteration': changes, 'converged': not changes,
    }
