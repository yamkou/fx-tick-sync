"""Validate a deployment and print a read-only plan. Does not start a collector."""
import argparse
import json
from pathlib import Path

from fxtick.config import ConfigError, load_config


def plan(config_path, collector_id):
    config_path = Path(config_path).resolve()
    config = load_config(config_path)
    collector = config.collector(collector_id)
    paths = config.paths.resolve(config_path.parent)
    # Stable logical namespace, independent of VPS hostname or OS machine ID.
    paths = {key: value if key == "provenance_registry" else value / collector.collector_id
             for key, value in paths.items()}
    return {
        "mode": "plan-only", "runtime_implemented": False,
        "collector_id": collector.collector_id, "location": collector.location,
        "environment": config.environment.value, "source_type": collector.source_type.value,
        "broker": collector.broker, "symbols": list(collector.symbols),
        "paths": {key: str(value) for key, value in paths.items()},
        "storage_destination": collector.storage_destination,
        "terminal_ids": [t.terminal_id for t in config.terminals if t.collector_id == collector_id],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--collector", required=True)
    args = parser.parse_args()
    try:
        result = plan(args.config, args.collector)
    except (ConfigError, OSError):
        parser.error("Invalid or inaccessible deployment configuration; values omitted")
    print(json.dumps(result, indent=2))


if __name__ == "__main__": main()
