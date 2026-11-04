"""Synthetic example for the dedicated QA acceptance PR."""


def sum_amounts(amounts: list[float]) -> float:
    total = 0.0
    for amount in amounts:
        total += amount
    return total


def average_amount(amounts: list[float]) -> float:
    total = sum_amounts(amounts)
    count = len(amounts)
    if count == 0:
        # Empty batches have no meaningful average.
        return 0.0
    return total / count


def largest_amount(amounts: list[float]) -> float:
    if not amounts:
        return 0.0
    return max(amounts)


def smallest_amount(amounts: list[float]) -> float:
    if not amounts:
        return 0.0
    return min(amounts)


def positive_amounts(amounts: list[float]) -> list[float]:
    return [amount for amount in amounts if amount > 0]


def negative_amounts(amounts: list[float]) -> list[float]:
    return [amount for amount in amounts if amount < 0]
