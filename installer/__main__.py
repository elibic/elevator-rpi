"""
נקודת הכניסה של חבילת ההתקנה: `python -m installer [--web] [--dry-run] ...`
מופעל ע"י setup.sh. בוחר בין ממשק הטרמינל (cli) לממשק הגרפי (web).
"""
from __future__ import annotations

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="installer",
        description="מתקין והגדרת מערכת מעלית RFID (טרמינל / דפדפן)",
    )
    parser.add_argument("--web", action="store_true",
                        help="הפעל את הכלי הגרפי בדפדפן במקום אשף הטרמינל")
    parser.add_argument("--dry-run", action="store_true",
                        help="הצג את הצעדים בלי לבצע שינויים במערכת")
    parser.add_argument("--port", type=int, default=8080,
                        help="פורט לכלי הגרפי (ברירת מחדל 8080)")
    parser.add_argument("--no-browser", action="store_true",
                        help="אל תפתח דפדפן אוטומטית (web)")
    parser.add_argument("--mock-serial", action="store_true",
                        help="החזר תג מדומה במקום סריקת חומרה (בדיקות)")
    args = parser.parse_args()

    if args.web:
        from . import web
        web.run_web(port=args.port, dry_run=args.dry_run,
                    open_browser=not args.no_browser, mock_serial=args.mock_serial)
    else:
        from . import cli
        cli.run_cli(dry_run=args.dry_run, mock_serial=args.mock_serial)


if __name__ == "__main__":
    sys.exit(main())
