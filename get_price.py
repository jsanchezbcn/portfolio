import yfinance as yf
print("HPQ", yf.Ticker("HPQ").history(period="1d")["Close"].iloc[-1])
print("SPY", yf.Ticker("SPY").history(period="1d")["Close"].iloc[-1])
print("^SPX", yf.Ticker("^GSPC").history(period="1d")["Close"].iloc[-1])
