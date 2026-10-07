import numpy as np
import pandas as pd

TRADING_DAYS = 365  # crypto 7x24


def sortino(returns, rf=0.0, periods=TRADING_DAYS):
    excess = returns - rf / periods
    downside = excess[excess < 0].std()
    return excess.mean() / downside * np.sqrt(periods) if downside else np.nan


def sharpe(returns, rf=0.0, periods=TRADING_DAYS):
    excess = returns - rf / periods
    return excess.mean() / excess.std() * np.sqrt(periods) if excess.std() else np.nan


def calmar(returns, periods=TRADING_DAYS):
    equity = (1 + returns).cumprod()
    mdd = (equity / equity.cummax() - 1).min()
    ann_ret = equity.iloc[-1] ** (periods / len(returns)) - 1
    return ann_ret / abs(mdd) if mdd else np.nan


def composite(returns):
    return 0.4 * sortino(returns) + 0.3 * sharpe(returns) + 0.3 * calmar(returns)