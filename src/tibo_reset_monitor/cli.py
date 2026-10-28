"""Command line: poll, run, status, list, send-test, manual."""

from __future__ import annotations

import argparse
import sys

from tibo_reset_monitor import __version__
from tibo_reset_monitor.config import ConfigError, load_config
from tibo_reset_monitor.logutil import configure_logging
from tibo_reset_monitor.service import build_runtime, list_text, manual_send, poll, run_forever, send_test, status_text


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print("configuration error:", file=sys.stderr)
        print(exc, file=sys.stderr)
        return 2
    configure_logging(args.log_level or config.log_level, config.log_file)
    runtime = build_runtime(config)
    try:
        return _dispatch(args, runtime)
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    finally:
        runtime.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tibo-reset-monitor",
        description="Notify configured chats when Tibo posts a new Codex or ChatGPT quota-reset update.",
    )
    parser.add_argument("--config", default="config.json", help="path to config.json (default: ./config.json)")
    parser.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING, or ERROR")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("poll", help="fetch once, enqueue new posts, and deliver due notifications")
    sub.add_parser("run", help="poll in a loop (default every 10 minutes)")

    status = sub.add_parser("status", help="show baseline, snapshot, and outbox counts")
    status.add_argument("--json", action="store_true")

    listing = sub.add_parser("list", help="show detected notices and per-target delivery state")
    listing.add_argument("--limit", type=int, default=20)
    listing.add_argument("--json", action="store_true")

    test = sub.add_parser("send-test", help="send a test message to one target")
    test.add_argument("--channel", required=True)
    test.add_argument("--target", required=True)

    manual = sub.add_parser("manual", help="re-send a stored notice, or send custom text")
    manual.add_argument("--channel", required=True)
    manual.add_argument("--target", required=True)
    manual.add_argument("--event", type=int, help="stored notice id from list")
    manual.add_argument("--text", help="custom text instead of a stored notice")
    manual.add_argument("--preview", action="store_true", help="print the payload and do not send")
    return parser


def _dispatch(args: argparse.Namespace, runtime) -> int:
    if args.command == "poll":
        return poll(runtime)
    if args.command == "run":
        return run_forever(runtime)
    if args.command == "status":
        print(status_text(runtime, as_json=args.json))
        return 0
    if args.command == "list":
        print(list_text(runtime, limit=args.limit, as_json=args.json))
        return 0
    if args.command == "send-test":
        return send_test(runtime, args.channel, args.target)
    if args.command == "manual":
        if args.event is None and not args.text:
            print("manual needs --event or --text", file=sys.stderr)
            return 2
        if args.event is not None and args.text:
            print("pass only one of --event or --text", file=sys.stderr)
            return 2
        return manual_send(
            runtime,
            channel_name=args.channel,
            target_name=args.target,
            event_id=args.event,
            text=args.text,
            preview=args.preview,
        )
    print(f"unknown command {args.command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
