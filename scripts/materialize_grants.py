"""Materialize grant tables from the local Parquet, sorted for row-group pruning.

Produces two datasets used by the slow grant pages:
  * org_grants          - one row per grant, with grantor EIN; sorted by grantor
                          EIN so `WHERE grantor_ein = ?` prunes to a few row groups.
                          Backs the Organizations page "Grants paid" panel.
  * grant_network_edges - aggregated grantor->grantee edges, sorted by source EIN.
                          Backs the Grant network page.

Uses DuckDB over parquet_export/ (local) so no big SQLite scan / high memory.
Files are split under 100 MB via FILE_SIZE_BYTES.
"""
from __future__ import annotations

import os
import duckdb

BASE = os.path.abspath("parquet_export")
con = duckdb.connect(":memory:")
con.execute(f"CREATE VIEW filings AS SELECT * FROM read_parquet('{BASE}/irs990_filings/*.parquet', union_by_name=true)")
con.execute(f"CREATE VIEW grants AS SELECT * FROM read_parquet('{BASE}/irs990_filing_grants/*.parquet', union_by_name=true)")

os.makedirs(f"{BASE}/org_grants", exist_ok=True)
os.makedirs(f"{BASE}/grant_network_edges", exist_ok=True)

print("Building org_grants (grant rows, sorted by grantor_ein) ...")
con.execute(f"""
    COPY (
        SELECT f.ein AS grantor_ein, f.tax_year, g.grantee_name, g.grantee_ein, g.amount
        FROM filings f JOIN grants g USING (filing_id)
        WHERE g.amount IS NOT NULL
        ORDER BY f.ein
    ) TO '{BASE}/org_grants'
      (FORMAT PARQUET, COMPRESSION ZSTD, FILE_SIZE_BYTES '80MB', OVERWRITE_OR_IGNORE)
""")

print("Building grant_network_edges (aggregated, sorted by source_ein) ...")
con.execute(f"""
    COPY (
        SELECT f.ein AS source_ein, g.grantee_ein AS target_ein, 'grant' AS edge_type,
               SUM(g.amount) AS amount, COUNT(*) AS supporting_rows
        FROM filings f JOIN grants g USING (filing_id)
        WHERE g.grantee_ein IS NOT NULL AND g.grantee_ein <> ''
        GROUP BY f.ein, g.grantee_ein
        ORDER BY source_ein
    ) TO '{BASE}/grant_network_edges'
      (FORMAT PARQUET, COMPRESSION ZSTD, FILE_SIZE_BYTES '80MB', OVERWRITE_OR_IGNORE)
""")

for t in ("org_grants", "grant_network_edges"):
    files = [f for f in os.listdir(f"{BASE}/{t}") if f.endswith(".parquet")]
    tot = sum(os.path.getsize(f"{BASE}/{t}/{f}") for f in files)
    biggest = max((os.path.getsize(f"{BASE}/{t}/{f}") for f in files), default=0)
    n = con.execute(f"SELECT COUNT(*) FROM read_parquet('{BASE}/{t}/*.parquet')").fetchone()[0]
    print(f"{t:22} {len(files)} files  {tot/1e6:7.1f} MB total  biggest {biggest/1e6:5.1f} MB  rows={n:,}")
