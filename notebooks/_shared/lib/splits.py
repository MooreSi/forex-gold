"""Walk-forward day splits. NEVER split signals randomly: signals minutes
apart ride the same market move, and a random split leaks the answer into
the test set. Always train on whole earlier days, test on whole later days.
"""
from __future__ import annotations

import pandas as pd


def day_folds(df: pd.DataFrame, day_col: str = "day", min_train_days: int = 3):
    """Yield (train_df, test_df) pairs walking forward one day at a time:
    days [0..k) -> train, day k -> test, for k >= min_train_days."""
    days = sorted(df[day_col].dropna().unique())
    for k in range(min_train_days, len(days)):
        train = df[df[day_col].isin(days[:k])]
        test = df[df[day_col] == days[k]]
        if len(test):
            yield train, test


def split_by_day(df: pd.DataFrame, frac: float = 0.6, day_col: str = "day"):
    """Single chronological split: first `frac` of days -> train, rest -> test."""
    days = sorted(df[day_col].dropna().unique())
    cut = max(1, int(len(days) * frac))
    return df[df[day_col].isin(days[:cut])], df[df[day_col].isin(days[cut:])]
