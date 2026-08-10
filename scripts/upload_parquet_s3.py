"""Upload the local parquet_export/ tree to S3 under a prefix.

Usage:
    python scripts/upload_parquet_s3.py
    python scripts/upload_parquet_s3.py --src parquet_export --prefix parquet
"""
from __future__ import annotations

import argparse
import os
import time

import boto3

BUCKET = os.environ.get("S3_BUCKET", "irs-990-263839540825-us-east-2-an")
REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-2")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="parquet_export")
    ap.add_argument("--prefix", default="parquet")
    args = ap.parse_args()

    s3 = boto3.client("s3", region_name=REGION)
    files = []
    for root, _dirs, names in os.walk(args.src):
        for n in names:
            if n.endswith(".parquet"):
                files.append(os.path.join(root, n))

    total = len(files)
    total_bytes = sum(os.path.getsize(f) for f in files)
    print(f"Uploading {total} files ({total_bytes/1e6:.0f} MB) to "
          f"s3://{BUCKET}/{args.prefix}/ ...")

    done = 0
    t0 = time.perf_counter()
    for f in files:
        rel = os.path.relpath(f, args.src).replace(os.sep, "/")
        key = f"{args.prefix}/{rel}"
        s3.upload_file(f, BUCKET, key)
        done += 1
        if done % 5 == 0 or done == total:
            print(f"  {done}/{total}  {key}")
    print(f"\nDone: {done} files in {time.perf_counter()-t0:.0f}s -> "
          f"s3://{BUCKET}/{args.prefix}/")


if __name__ == "__main__":
    main()
