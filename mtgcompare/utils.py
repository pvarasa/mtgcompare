import yfinance as yf


def get_fx(ccy: str) -> float:
    return yf.Ticker(f"{ccy.upper()}=X").info["previousClose"]
