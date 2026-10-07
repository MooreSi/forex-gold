# Reversal research evidence

Starts automatically with the updated app research loop. Collects into `DATA_DIR/reversal_evidence.db`; models and reproducible datasets live in `DATA_DIR/reversal_experiments/`. No orders, stops, sizing, trading gates or production model files are changed. An app already running the previous Python code needs its normal restart to load this collector; this change does not restart it.

## Works without keys or new packages

- MT5 bid/ask samples every five seconds and bounded tick-range imports about every 30 seconds when the bridge supports them. Tick gaps after downtime are reported, not fabricated. Raw ticks/books expire after seven days; decision-linked feature vectors, executions, calendar revisions and experiments remain.
- Read-only engine/broker joins retain execution geometry, initial risk, partials, raw deal cashflows and millisecond deal timestamps. Position history now includes separate commission in `mt5_bridge.py` (Wine bridge must load that update too). Unknown commissions and incomplete deal histories remain marked incomplete. All deal cashflows, including entry commissions, are summed once for the research label; original trade bookkeeping is unchanged.
- Forex Factory snapshots reuse the existing cached public calendar and its rate limits. This feed gives scheduled times/consensus/previous values, **not actual release surprises**. Its calendar has only the current week.
- Yahoo `GC=F` five-minute closed price/volume bars via the existing yfinance dependency. These are delayed front-contract proxies, **not licensed live COMEX depth**. Arrival timestamps prevent their historical values from entering decisions before this app received them. Contract rolls are not inferred from this symbol. Stale/missing features have explicit flags.
- Local dataset/code/schema/policy/model hashes, forward validation metrics and candidate artifacts; no MLflow server needed.

The existing primary learner remains virtual. The separate broker candidate trains after 80 settled, commission-complete, valid decision vectors under one policy. Unmeasured-cost rows remain in audit artifacts but are excluded from fitting and validation. Every fold uses only labels already observed before the test decisions. Broker decisions without an immutable feature/policy snapshot remain collected but cannot train this candidate. Policies and demo/live environments are separate. At least 200 selected validation observations, 30 day clusters, positive halves, positive clustered lower bounds and incremental results over all broker trades actually executed by the deployed policy are needed even for review eligibility. Complete cashflows are required; this flag never auto-promotes a model. These tests concern executed trades only and do not establish performance on rejected/untraded opportunities.

## Inspect it

From the repository root:

```sh
.venv/bin/python -m tools.reversal_evidence status --env demo
.venv/bin/python -m tools.reversal_evidence evaluate --env demo
```

Status reports provider state, last health-update age, labels and local experiment results. Evaluation writes only research artifacts. Use `--data-dir` for an isolated copy. No saved production model is loaded or overwritten.

## Optional paid upgrades

Credentials are read only from environment variables; never commit them or put them in experiment parameters. They are unnecessary for the free stack.

| Integration | Configuration |
|---|---|
| Trading Economics calendar WebSocket | `RE_TE_ENABLED=1`, `RE_TE_CREDENTIAL` containing provider client key/secret |
| Databento COMEX MBP-1 | `RE_CME_ENABLED=1`, `DATABENTO_API_KEY`, `RE_CME_SYMBOL` specifying an explicit contract (for example `GCZ6`), and separately approved optional Databento Python SDK installation |
| Existing MLflow REST server | `MLFLOW_TRACKING_URI`; optional `MLFLOW_TRACKING_TOKEN` |

CME collection is at most one top-of-book observation per second per instrument, not every exchange event and not full order-by-order reconstruction. Explicit contracts prevent hidden roll changes; the operator must update the contract when appropriate. Streams retry after failure and report missing prerequisites; expiry or missing credentials never enable a trading fallback. MLflow export uses a durable local outbox and run-identity tags; network failures preserve pending runs. Local datasets/model artifacts remain authoritative and private; REST export sends metrics/hashes/parameters, not raw trade datasets or models. A custom experiment store is not an installed MLflow server or registry.

Licensed historical import is manual and can incur provider costs:

```sh
.venv/bin/python -m tools.reversal_evidence import-cme --env demo --symbol GCZ6 --start 2026-10-06T12:00:00Z --end 2026-10-06T13:00:00Z
```

The import requires a key, is capped at 100,000 returned records and timestamps availability at import. It cannot invent historical local arrival times or validate past decisions as though those records were live then. Source references: [Databento APIs](https://databento.com/docs/api-reference-live/client), [Trading Economics streaming](https://docs.tradingeconomics.com/economic_calendar/streaming/), [MLflow REST](https://mlflow.org/docs/latest/api_reference/rest-api.html), [Yahoo gold futures](https://finance.yahoo.com/quote/GC%3DF/).

## Remaining measurement limitations

The order transport does not publish an exact submit timestamp to this collector. It retains the decision timestamp, application open time and raw broker deal time; `send_time` stays null rather than inventing a value. Raw deal times retain their broker-server basis and are not falsely labelled UTC. Tick-range seconds can coalesce identical repeated quotes because the existing bridge does not expose milliseconds on that endpoint. Provider health age and raw event age must be considered before treating a stream as current. Collecting more data does not guarantee an edge.
