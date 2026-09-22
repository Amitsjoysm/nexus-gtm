#!/usr/bin/env python
"""Export one training dataset to JSONL or Parquet (spec §18.4).

    python scripts/export_training_dataset.py --dataset sft_outreach_email --split train
    python scripts/export_training_dataset.py --dataset cls_reply --format parquet --out data/

Reads the TRAINING store only, which holds no names, addresses, phone numbers, links or company
names — identities are HMAC keys and free text has been through the scrubber. Every row carries the
``scrubber_version`` and ``consent_terms_version`` it was built under, so an export can be filtered
if either changes.

JSONL is written in the chat format fine-tuning APIs take (``{"messages": [...]}``) for the SFT
datasets, and one JSON object per row for the others. Parquet needs the optional ``export`` extra
(``pip install -e ".[export]"``), because pyarrow is 40 MB and no server needs it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys

DATASETS = ("sft_outreach_email", "pref_outreach_email", "cls_reply", "sft_reply_response",
            "tab_engagement", "ai_calls")
SPLITS = ("train", "val", "test")


async def rows(dataset: str, split: str, limit: int, min_scrubber: int) -> list[dict]:
    from nexus.engagement.ledger.stores import connect

    query = ["SELECT example_id, dataset, created_at, split, workspace_key, schema_version, "
             "       scrubber_version, consent_terms_version, quality, record "
             "FROM nexus_ledger.examples WHERE dataset = $1 AND scrubber_version >= $2"]
    params: list = [dataset, min_scrubber]
    if split:
        query.append("AND split = $3")
        params.append(split)
    query.append("ORDER BY created_at, example_id")
    if limit:
        query.append(f"LIMIT {int(limit)}")
    async with connect("training") as conn:
        found = await conn.fetch(" ".join(query), *params)
    return [dict(row) for row in found]


def to_record(row: dict) -> dict:
    record = row["record"]
    if isinstance(record, str):
        record = json.loads(record)
    quality = row["quality"]
    if isinstance(quality, str):
        quality = json.loads(quality)
    out = dict(record)
    out["_meta"] = {
        "example_id": row["example_id"], "split": row["split"],
        "workspace_key": row["workspace_key"], "created_at": str(row["created_at"]),
        "schema_version": row["schema_version"], "scrubber_version": row["scrubber_version"],
        "consent_terms_version": row["consent_terms_version"], "quality": quality,
    }
    return out


def write_jsonl(path: pathlib.Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_parquet(path: pathlib.Path, records: list[dict]) -> None:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:  # pragma: no cover - depends on an optional extra
        raise SystemExit('Parquet needs the optional extra: pip install -e ".[export]"') from None

    # Records are nested and ragged, so the columns are the JSON of each top-level key. A schema
    # guessed per file would differ between exports of the same dataset, which is worse than one
    # honest string column.
    columns = sorted({key for record in records for key in record})
    table = pa.table({
        column: [json.dumps(record.get(column), ensure_ascii=False) if isinstance(
            record.get(column), (dict, list)) else record.get(column) for record in records]
        for column in columns
    })
    pq.write_table(table, path)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", required=True, choices=DATASETS)
    parser.add_argument("--split", default="", choices=("", *SPLITS),
                        help="train, val or test; omit for every split")
    parser.add_argument("--format", default="jsonl", choices=("jsonl", "parquet"))
    parser.add_argument("--out", default="exports", help="directory to write into")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--min-scrubber", type=int, default=1,
                        help="skip rows built by an older scrubber than this")
    args = parser.parse_args()

    found = await rows(args.dataset, args.split, args.limit, args.min_scrubber)
    if not found:
        print("no rows matched")
        return 1
    records = [to_record(row) for row in found]
    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"{args.dataset}{'-' + args.split if args.split else ''}.{args.format}"
    path = out_dir / name
    if args.format == "jsonl":
        write_jsonl(path, records)
    else:
        write_parquet(path, records)
    versions = sorted({r["_meta"]["consent_terms_version"] for r in records})
    print(f"wrote {len(records)} rows to {path}")
    print(f"consent terms in this export: {', '.join(v or '(none recorded)' for v in versions)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
