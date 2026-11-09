"""Small, exact statistics for paired harness comparisons. Standard library only."""

from __future__ import annotations

import math
import random
from statistics import median


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - r) / d, (c + r) / d)


def binom_two_sided(k: int, n: int) -> float:
    """Exact two-sided binomial test of k successes in n at p = 0.5 (McNemar's exact test
    when k and n - k are the two discordant counts)."""
    if n == 0:
        return 1.0
    pmf = [math.comb(n, i) / 2 ** n for i in range(n + 1)]
    obs = pmf[k]
    return min(1.0, sum(p for p in pmf if p <= obs + 1e-12))


def mcnemar(pairs: list[tuple[bool, bool]]) -> dict:
    """pairs of (a_passed, b_passed) on the same task and trial index."""
    b = sum(1 for x, y in pairs if x and not y)
    c = sum(1 for x, y in pairs if y and not x)
    return {"a_only": b, "b_only": c, "both": sum(1 for x, y in pairs if x and y),
            "neither": sum(1 for x, y in pairs if not x and not y), "p": round(binom_two_sided(b, b + c), 4)}


def bootstrap_ci(diffs: list[float], iters: int = 4000, seed: int = 7, alpha: float = 0.05) -> tuple[float, float] | None:
    if not diffs:
        return None
    rng = random.Random(seed)
    n = len(diffs)
    means = sorted(sum(rng.choice(diffs) for _ in range(n)) / n for _ in range(iters))
    return (means[int(iters * alpha / 2)], means[int(iters * (1 - alpha / 2)) - 1])


def pass_hat_k(results_by_task: dict[str, list[bool]], k: int) -> float | None:
    """Share of tasks whose first k scored trials all passed (tau-bench's pass^k)."""
    eligible = [v[:k] for v in results_by_task.values() if len(v) >= k]
    if not eligible:
        return None
    return sum(1 for v in eligible if all(v)) / len(eligible)


def med(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    return median(xs) if xs else None
