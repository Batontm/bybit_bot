from pybit.unified_trading import HTTP
import json

def debug():
    client = HTTP(testnet=False)
    res = client.get_tickers(category="spot")
    
    tickers = res['result']['list']
    for t in tickers:
        if t['symbol'] == 'BTCUSDT':
            print(f"BTCUSDT full data: {json.dumps(t, indent=2)}")
            change = float(t['price24hPcnt']) * 100
            print(f"Calculated change: {change}%")
            break

if __name__ == "__main__":
    debug()
