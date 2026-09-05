"""Offline LOCAL_TEST CLI. Explicit legacy ledger, new output, no network."""
import argparse
from pathlib import Path
from fxtick.policy import ExportPurpose


def resolve_io(args):
    """Optional config; existing invocations retain their original path semantics."""
    if not args.config:
        return args.inputs, args.output, args.ledger, None
    from fxtick.config import ConfigError, load_config, native_path
    config_path = Path(args.config).resolve()
    config = load_config(config_path)
    paths = config.paths.resolve(config_path.parent)
    inputs = [native_path(p, paths["data_root"]) for p in args.inputs]
    output = native_path(args.output, paths["export_root"])
    if output == paths["export_root"] or paths["export_root"] not in output.parents:
        raise ConfigError("Configured local outputs must remain inside export_root")
    ledger = native_path(args.ledger, config_path.parent) if args.ledger else paths["provenance_registry"]
    # A new managed input needs no old ledger. Unregistered legacy inputs still
    # fail in the unchanged Phase 2 inspector, never receive an UNKNOWN exception.
    if not args.ledger and not ledger.exists():
        ledger = None
    return inputs, output, ledger, paths["temp_root"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Optional non-secret deployment JSON")
    parser.add_argument("inputs", nargs="+")
    parser.add_argument("--ledger", help="Owner-approved legacy registration ledger")
    parser.add_argument("--output", required=True, help="NEW output path")
    parser.add_argument("--format", choices=("parquet", "mt4", "mt5", "hst"), required=True)
    parser.add_argument("--tz", choices=("broker", "utc", "jst"), default="broker")
    parser.add_argument("--symbol", default="CUSTOM")
    parser.add_argument("--period", type=int, default=1)
    parser.add_argument("--digits", type=int, default=5)
    args = parser.parse_args()
    inputs, output, ledger, temp_root = resolve_io(args)
    from fxtick import duck, mt_export
    query = duck.union_sources(inputs, ledger=ledger)
    query.check(ExportPurpose.LOCAL_TEST)
    con = duck.connect(temp_dir=temp_root) if temp_root is not None else duck.connect()
    try:
        if args.format == "parquet":
            duck.write_parquet(con, query, output)
        elif args.format == "hst":
            mt_export.export_hst(con, query, output, args.symbol, args.period, args.digits,
                                 args.tz, purpose=ExportPurpose.LOCAL_TEST)
        else:
            writer = mt_export.export_mt4_ticks if args.format == "mt4" else mt_export.export_mt5_ticks
            writer(con, query, output, args.tz, args.digits, purpose=ExportPurpose.LOCAL_TEST)
    finally:
        con.close()


if __name__ == "__main__":
    main()
