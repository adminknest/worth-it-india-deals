# Operations runbook

## Start-of-day check

```powershell
python dealbot.py doctor
python earnkaro_intake.py readiness
python earnkaro_intake.py dashboard
```

Do not start the publisher if `doctor` reports `healthy: false`. Inspect uncertain attempts and incidents first.

## Normal EarnKaro flow

1. Select a promising current deal from a permitted source.
2. Open the merchant page and verify the exact product, current price and stock.
3. Generate the Profit Link in EarnKaro's supported Make Links interface.
4. Record the deal with `earnkaro_intake.py record`.
5. Review it with `python dealbot.py shortlist`.
6. Approve it locally or through the private operator bot.
7. Render it with `python dealbot.py publish-next --dry-run`.
8. Publish only after the channel and bot configuration are active.

## Failure handling

### `No post: no fresh approved deal`

The price evidence is stale, missing, no longer matches the deal, or the material snapshot changed after approval. Verify again and approve the new snapshot.

### Operator paused after a Telegram error

Do not rerun publication. Check the Telegram channel manually.

- If the post exists, copy its Telegram message ID and run:

  ```powershell
  python dealbot.py reconcile ATTEMPT_KEY sent --message-id MESSAGE_ID
  ```

- If the post definitely does not exist, run:

  ```powershell
  python dealbot.py reconcile ATTEMPT_KEY failed
  ```

Then review incidents and resume only when the state is understood.

### Audit chain invalid

Stop the worker. Preserve the database and its `-wal` and `-shm` files together. Restore the last verified backup or investigate the modification before publishing again.

## Backup

Use SQLite's consistent backup API through the built-in command:

```powershell
python dealbot.py backup data\backups\deals-YYYY-MM-DD.db
```

Test a backup before relying on it:

```powershell
python dealbot.py --config config.json doctor
```

Keep at least seven daily copies once the pilot is live. Do not copy only `deals.db` while an active WAL database is being written; use the backup command.

## Useful diagnostics

```powershell
python dealbot.py doctor
python dealbot.py audit-tail --limit 50
python dealbot.py shortlist --limit 20
python earnkaro_intake.py dashboard
```

The database tables `incidents` and `publication_attempts` contain the recovery facts. Logs and screenshots are supporting evidence; the database is the operational record.
