# MT5 Bridge executor

Warm MetaTrader 5 worker for MT5 Bridge. This repository is generated from
the private control-plane repository. Do not edit it by hand.

Workflows: `mt5-warm` keeps a terminal alive and chains itself, `mt5-watchdog`
dispatches a replacement if no worker is online.

Required secrets: `MT5_API_URL`, `MT5_WORKER_SECRET`, `MT5_DISPATCH_TOKEN`,
`MT5_SUPABASE_URL`, `MT5_SUPABASE_KEY`.
