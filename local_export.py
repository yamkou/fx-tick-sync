"""Offline LOCAL_TEST CLI. Explicit legacy ledger, new output, no network."""
import argparse
from pathlib import Path
from fxtick.policy import ExportPurpose


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+")
    parser.add_argument("--ledger", help="Owner-approved legacy registration ledger")
    parser.add_argument("--output", required=True, help="NEW output path")
    parser.add_argument("--format", choices=("parquet", "mt4", "mt5", "hst"), required=True)
    parser.add_argument("--tz", choices=("broker", "utc", "jst"), default="broker")
    parser.add_argument("--symbol", default="CUSTOM")
    parser.add_argument("--period", type=int, default=1)
    parser.add_argument("--digits", type=int, default=5)
    args = parser.parse_args()
    from fxtick import duck, mt_export
    query = duck.union_sources(args.inputs, ledger=args.ledger)
    query.check(ExportPurpose.LOCAL_TEST)
    con = duck.connect()
    try:
        if args.format == "parquet":
            duck.write_parquet(con, query, args.output)
        elif args.format == "hst":
            mt_export.export_hst(con, query, args.output, args.symbol, args.period, args.digits,
                                 args.tz, purpose=ExportPurpose.LOCAL_TEST)
        else:
            writer = mt_export.export_mt4_ticks if args.format == "mt4" else mt_export.export_mt5_ticks
            writer(con, query, args.output, args.tz, args.digits, purpose=ExportPurpose.LOCAL_TEST)
    finally:
        con.close()


if __name__ == "__main__":
    main()
