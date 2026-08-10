import sqlite3, time, os
import pandas as pd

c = sqlite3.connect("file:data/irs990_full.db?mode=ro", uri=True)
os.makedirs("parquet_export/organization_policy_links", exist_ok=True)

t = time.perf_counter()
df = pd.read_sql_query("SELECT * FROM organization_policy_links", c)
print(f"rows: {len(df):,}  built in {time.perf_counter()-t:.1f}s")
print("columns:", list(df.columns))

out = "parquet_export/organization_policy_links/organization_policy_links__all__0000.parquet"
df.to_parquet(out, compression="zstd", index=False)
print(f"wrote {out}  {os.path.getsize(out)/1e6:.1f} MB")
