# Git and competition compliance

- Make small, logical commits with messages that explain their purpose. Keep an auditable history; do not squash the entire project into one unexplained final judging commit.
- Never commit API credentials, `.env`, local SQLite files, runtime logs, signed requests, or authorization headers. Inspect staged diffs before committing.
- Never use the competition key for manual test orders. Experiments must use test credentials and approved test environments; D unit tests are fully offline.
- D changes must not alter alpha or strategy decisions. A owns Broker execution/signing/deployment; B owns data/factors; C owns alpha/backtest/target generation.
- Changes to the four interfaces in `CONTRACTS.md` require review by the affected owners before integration.
- Do not rewrite teammates' commits or discard uncommitted work. Preserve the decision → risk → intent → broker → fill → PnL audit trail.
