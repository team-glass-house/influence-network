"""_summary_
"""
from io import BytesIO

import boto3
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .config import settings

S3_CLIENT = boto3.client(
    's3',
    aws_access_key_id=settings.s3_key,
    aws_secret_access_key=settings.s3_secret,
    region_name=settings.s3_region,
)


# Heavily inspired by suggestions below
# Source - https://stackoverflow.com/a/57838851
# Posted by gurjarprateek, modified by community. See post 'Timeline' for change history
# Retrieved 2026-08-05, License - CC BY-SA 4.0
def df_to_s3(df: pd.DataFrame, path: str) -> None:
    buf = BytesIO()
    df.to_parquet(buf, index=False, compression="zstd")
    
    S3_CLIENT.put_object(
        Bucket=settings.s3_arn,
        Key=path,
        Body=buf.getvalue()
    )