# Product Specification

Last consolidated: 2026-09-05

## 1. Product objective
Commercial software for validation/conversion of historical tick data for MT4/MT5.

Primary planned instruments:
- 28 major FX pairs
- XAUUSD
- US30
- BTCUSD
- ETHUSD

Japanese equities / J-Quants are outside the current product scope.

## 2. Proposed product layers
### Desktop/tool layer
A paid Windows application / executable providing:
- conversion engine
- local historical-data workflows
- MT4/MT5 output
- local reference-data validation
- potentially external source connectors

### Data/service layer
Possible recurring delivery of legally distributable broker/source data, subject to source-specific licensing review.

Do not define the subscription merely as access to an old static archive. The intended value is ongoing, quality-controlled updates and broker-relevant data.

## 3. Product differentiation
The product should not depend on the claim that one public reference source is universally "more accurate".

Desired value:
- test an EA/strategy against data closer to the user's actual broker environment
- compare results across sources
- quantify spread/tick-density/gap differences
- make MT4/MT5 historical validation easier and repeatable

## 4. Planned sources
### Dukascopy
Role: private/reference/QA source, not a commercial redistributable dataset.

Historical Dukascopy data already exists in the current workflow and is valuable for long-horizon backtests. Do not casually delete it or force repeated redownloads.

### cTrader Open API
Candidate acquisition path for broker data because it can support headless/server collection better than a GUI MT5 terminal. Actual history depth, paging, tick fields, demo/live equivalence, symbol mapping and licensing must be verified.

Candidate brokers:
- IC Markets — primary candidate
- AXIORY — secondary candidate
- FxPro — fallback candidate

No redistribution permission is assumed merely from API availability.

### Binance
Candidate source for BTC/ETH historical data. Confirm current licence/terms before commercial redistribution.

## 5. MT5 target
Preferred approach:
- create a custom symbol
- copy required symbol specifications from the relevant source/broker symbol where appropriate
- inject ticks in chunks, potentially using `CustomTicksReplace()`

Important symbol properties may include:
- digits
- tick size
- contract size
- margin/profit currency
- swap and other calculation-relevant settings

Target tick representation under investigation:
`<DATE> <TIME> <BID> <ASK> <LAST> <VOLUME> <FLAGS>`

Milliseconds are required for tick fidelity. FLAGS behaviour must be verified on a real MT5 installation.

## 6. MT4 target
Direct tick CSV is insufficient for a true MT4 tick-testing workflow. Determine whether the existing exporter creates only HST or also appropriate FXT/tester data.

## 7. Time handling
Internal/master time basis: UTC.

Source/server-time conversion should happen at output/presentation boundaries. Existing broker/UTC/JST modes should be preserved where useful.

## 8. Backtesting direction
A major future capability is Python-side bulk comparison so that many strategy variants and multiple data sources can be evaluated faster than repeated manual MT5 tests.

Potential report metrics:
- PF
- drawdown
- win rate
- trade count
- net profit
- equity curve
- annual/monthly results
- spread sensitivity
- source-to-source variance

MT5 remains important for final platform-specific validation.
