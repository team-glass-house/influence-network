from io import BytesIO

import pandas as pd

from .config import settings

def df_to_s3(df: pd.DataFrame, path: str) -> None:
    if not settings.s3_arn:
        raise RuntimeError("S3_BUCKET is required for S3 output")
    import boto3

    client = boto3.client(
        "s3",
        aws_access_key_id=settings.s3_key or None,
        aws_secret_access_key=settings.s3_secret or None,
        region_name=settings.s3_region or None,
    )
    buf = BytesIO()
    df.to_parquet(buf, index=False, compression="zstd")
    client.put_object(Bucket=settings.s3_arn, Key=path, Body=buf.getvalue())