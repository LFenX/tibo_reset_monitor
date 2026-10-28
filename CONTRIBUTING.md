# Contributing

Thanks for looking at tibo-reset-monitor.

- Python 3.10 or newer. Keep the core and the built-in channels importable on the standard library. Pillow is an optional extra (`pip install '.[calendar]'`) used only to draw the calendar. Tests must pass with and without it.
- Run tests before sending a change:

  ```bash
  PYTHONPATH=src python -m unittest discover -s tests -v
  ```

- Do not commit secrets, real app ids, chat ids, tokens, or a filled-in `config.json` / `.env`. `config.example.json` stays full of obvious placeholders.
- Core code under `src/tibo_reset_monitor/core/` must not import a channel. New destinations go through the channel interface. See [docs/adding-a-channel.md](docs/adding-a-channel.md).
- Every channel must render both `zh` and `en` from `tibo_reset_monitor.core.i18n.compose`. Do not hard-code a single language. `tests/test_i18n.py` renders every class in `REGISTRY` in both languages.
- A fetch failure must not overwrite the last good snapshot. One target's failure must not resend a message that another target already accepted.
- The confirmation time shown to people is the source post time from AIHOT, not an account credit time. Keep that visible in new channel formats.
