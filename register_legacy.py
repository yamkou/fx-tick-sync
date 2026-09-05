"""Explicit, offline, single-file Dukascopy reference registration."""
import argparse
from fxtick.artifacts import register_legacy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", help="Exact owner-verified Dukascopy file; no glob or folder scan")
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--confirm-dukascopy-owner-reference", action="store_true", required=True)
    args = parser.parse_args()
    artifact = register_legacy(args.file, args.ledger,
        owner_confirmed=args.confirm_dukascopy_owner_reference)
    print(f"Registered PRIVATE_REFERENCE / LOCAL_TEST: {artifact.sha256}, {artifact.size} bytes")


if __name__ == "__main__":
    main()
