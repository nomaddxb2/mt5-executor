# MT5 Bridge executor

Warm MetaTrader 5 worker for [MT5 Bridge](https://mt5bridge.vercel.app). This
repository is generated from the private control-plane repository. Do not edit
it by hand.

Workflows:

- `mt5-warm` installs MetaTrader 5 on `windows-latest`, runs the worker for 345
  minutes, then dispatches the next run.
- `mt5-watchdog` runs every 15 minutes and dispatches a warm run if no worker is
  online.

Required repository secrets: `MT5_API_URL`, `MT5_WORKER_SECRET`,
`MT5_DISPATCH_TOKEN`, `MT5_SUPABASE_URL`, `MT5_SUPABASE_KEY`.

The runner holds no broker credentials. They arrive per job from the control
plane and are decrypted in memory only.
