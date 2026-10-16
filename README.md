---
title: Quotex API by Haider
emoji: 📈
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
pinned: false
license: mit
---

# Quotex API by Haider
Simple API for Quotex OHLCV historical data - fetch 200,000+ candles!

## Endpoint
`GET /quotex_candles?asset=ASSET&timeframe=TIMEFRAME&count=COUNT`

Examples:
- 10k candles: `/quotex_candles?asset=USDBDT_otc&timeframe=M1&count=10000`
- 100k candles: `/quotex_candles?asset=USDBDT_otc&timeframe=M1&count=100000`
- 200k candles: `/quotex_candles?asset=USDBDT_otc&timeframe=M1&count=200000`

## Parameters
- `asset`: Asset pair (e.g., USDBDT_otc, EURUSD_otc)
- `timeframe`: 5s, 10s, 15s, 30s, M1, M5, M15, M30, H1, H4, D1
- `count`: Number of candles (100, 500, 1000, 10000, 100000, 200000+)

## Candle to Day Conversion Guide
| Timeframe | 100,000 Candles | 200,000 Candles |
|-----------|-----------------|-----------------|
| M1 (1 min) | ~70 days | ~140 days |
| M5 (5 min) | ~347 days (~1 year) | ~694 days (~2 years) |
| M15 (15 min) | ~1,041 days (~3 years) | ~2,083 days (~6 years) |

## Created by Haider
