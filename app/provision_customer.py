from __future__ import annotations

import argparse

from app.db import create_customer, init_db


def main() -> None:
    parser = argparse.ArgumentParser(description="Provision a Telegram user as a separate customer/tenant.")
    parser.add_argument("--telegram-id", type=int, required=True, help="Numeric Telegram user ID from /myid")
    parser.add_argument("--name", required=True, help="Customer display name")
    parser.add_argument("--timezone", default="Europe/Berlin", help="IANA timezone, e.g. Europe/Berlin")
    parser.add_argument(
        "--style",
        default="Professional, authentic, clear, thoughtful, and not overly promotional.",
        help="Writing-style guidance for this customer",
    )
    args = parser.parse_args()

    init_db()
    customer = create_customer(
        telegram_user_id=args.telegram_id,
        display_name=args.name,
        writing_style=args.style,
        timezone_name=args.timezone,
    )
    print("Customer provisioned.")
    print(f"Customer ID: {customer['id']}")
    print(f"Display name: {customer['display_name']}")
    print(f"Telegram user ID: {customer['telegram_user_id']}")
    print(f"Photo directory: {customer['photo_dir']}")
    print("Copy only this customer's genuine photos into the photo directory.")


if __name__ == "__main__":
    main()
