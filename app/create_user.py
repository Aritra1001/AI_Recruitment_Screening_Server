"""Create or update a portal user.

    python -m app.create_user --email admin@example.com --name "Jane Admin" --role admin
    python -m app.create_user --email rec@example.com --name "Rob Recruiter" --role recruiter

The password is prompted for unless --password is given. Running it again for an
existing email updates that user's name, role and password.
"""

import argparse
import getpass
import sys

from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import Role, User
from app.security import MIN_PASSWORD_LENGTH, hash_password


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--role", choices=[r.value for r in Role], default=Role.RECRUITER.value)
    parser.add_argument("--password", help="omit to be prompted (keeps it out of shell history)")
    args = parser.parse_args()

    password = args.password or getpass.getpass("Password: ")
    if not args.password and password != getpass.getpass("Confirm password: "):
        print("Passwords do not match", file=sys.stderr)
        return 1
    if len(password) < MIN_PASSWORD_LENGTH:
        print(f"Password must be at least {MIN_PASSWORD_LENGTH} characters", file=sys.stderr)
        return 1
    try:
        password_hash = hash_password(password)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1

    email = args.email.strip().lower()
    with SessionLocal() as db:
        user = db.scalar(select(User).where(func.lower(User.email) == email))
        action = "Updated" if user else "Created"
        if user is None:
            user = User(email=email)
            db.add(user)
        user.full_name = args.name
        user.role = args.role
        user.password_hash = password_hash
        user.is_active = True
        db.commit()
    print(f"{action} {args.role} user {email}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
