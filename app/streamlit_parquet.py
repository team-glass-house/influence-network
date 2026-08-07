"""Parquet + DuckDB variant of the dashboard (SEPARATE test app).

Reads the Parquet export produced by scripts/db_to_parquet.py via DuckDB
instead of opening the 11.9 GB SQLite file. The original app
(app/streamlit_app.py) is left untouched.

Data source is configurable:
  * local (default):  PARQUET_BASE=parquet_export
  * S3:               PARQUET_BASE=s3://irs-990-263839540825-us-east-2-an/parquet
    (needs AWS creds in env or .streamlit/secrets.toml, and duckdb httpfs)

Run:
    python -m streamlit run app/streamlit_parquet.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import duckdb
import networkx as nx
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _cfg(name: str, default: str | None = None) -> str | None:
    """Read config from Streamlit Secrets first (Community Cloud), then env,
    then a default. Lets the same code run deployed and locally."""
    try:
        val = st.secrets.get(name)
        if val:
            return val
    except Exception:
        pass
    return os.environ.get(name, default)


# On Community Cloud set PARQUET_BASE in Secrets (e.g. s3://.../parquet).
# Locally it defaults to the on-disk export.
PARQUET_BASE = _cfg("PARQUET_BASE", str(ROOT / "parquet_export"))

# Base tables that were exported to Parquet (members had 0 rows -> skipped).
TABLES = [
    # Only the tables the app actually queries (others were materialized away).
    "organizations", "irs990_filings", "irs990_filing_people",
    "committees", "bills",
    "dash_filings_by_year", "dash_most_lobbied_bills", "dash_political_orgs",
    "lda_filings", "lda_lobbying_activities", "lobbying_bill_links",
    "organization_policy_links",  # pre-materialized (scripts/materialize_policy_links.py)
    "org_grants", "grant_network_edges",  # pre-materialized (scripts/materialize_grants.py)
]

# Derived views still computed live (small). Heavy ones were pre-materialized.
# lobbying_bill_facts uses integer division (//) for the congress calc so DuckDB
# matches SQLite.
VIEW_SQL: list[tuple[str, str]] = [
    ("committee_spending_summary", """
        SELECT committee_id, name, committee_type, cycle,
               total_receipts, total_disbursements,
               independent_expenditures, cash_on_hand_end_period
        FROM committees
        WHERE total_disbursements IS NOT NULL
    """),
    ("lobbying_bill_facts", """
        SELECT l.filing_uuid, l.filing_year, l.client_name, l.registrant_name,
               COALESCE(l.income, l.expenses, 0) AS reported_lobbying_amount,
               link.bill_type, link.bill_number, bills.bill_id, bills.title,
               bills.policy_area, activity.general_issue_code, activity.description
        FROM lobbying_bill_links AS link
        JOIN lda_filings AS l ON l.filing_uuid = link.filing_uuid
        JOIN bills ON bills.bill_type = link.bill_type
          AND bills.bill_number = link.bill_number
          AND bills.congress = CAST((l.filing_year - 1789) // 2 AS INTEGER) + 1
        LEFT JOIN lda_lobbying_activities AS activity
               ON activity.filing_uuid = l.filing_uuid
    """),
]

st.set_page_config(page_title="Influence Network (Parquet/DuckDB)", layout="wide")


def _resolve_aws_creds() -> tuple[str | None, str | None]:
    """Find AWS creds for DuckDB's S3 reader: Streamlit Secrets -> env -> local
    AWS profile (~/.aws). This lets it work on Community Cloud (Secrets) and
    locally (your configured profile) without code changes."""
    key = sec = None
    try:
        key = st.secrets.get("AWS_ACCESS_KEY_ID")
        sec = st.secrets.get("AWS_SECRET_ACCESS_KEY")
    except Exception:
        pass
    key = key or os.environ.get("AWS_ACCESS_KEY_ID")
    sec = sec or os.environ.get("AWS_SECRET_ACCESS_KEY")
    if not (key and sec):
        try:
            import boto3
            c = boto3.Session().get_credentials()
            if c:
                fc = c.get_frozen_credentials()
                key, sec = fc.access_key, fc.secret_key
        except Exception:
            pass
    return key, sec


@st.cache_resource
def get_con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(database=":memory:")
    is_s3 = PARQUET_BASE.startswith("s3://")
    if is_s3:
        con.execute("INSTALL httpfs; LOAD httpfs;")
        region = _cfg("AWS_DEFAULT_REGION", "us-east-2")
        key, sec = _resolve_aws_creds()
        if key and sec:
            # CREATE SECRET (not SET s3_*): a secret is shared across cursors,
            # whereas SET s3_access_key_id is session-scoped and a fresh
            # get_con().cursor() would not inherit it -> "missing credentials".
            con.execute(
                "CREATE OR REPLACE SECRET s3secret "
                f"(TYPE S3, KEY_ID '{key}', SECRET '{sec}', REGION '{region}')"
            )
        else:
            con.execute(f"SET s3_region='{region}';")

    base = PARQUET_BASE.rstrip("/")
    for t in TABLES:
        glob = f"{base}/{t}/*.parquet"
        try:
            con.execute(
                f"CREATE OR REPLACE VIEW {t} AS "
                f"SELECT * FROM read_parquet('{glob}', union_by_name=true)"
            )
        except Exception as exc:  # missing export -> skip that table
            print(f"skip {t}: {exc}")
    for name, sql in VIEW_SQL:
        con.execute(f"CREATE OR REPLACE VIEW {name} AS {sql}")
    return con


@st.cache_data(ttl=600)
def run_query(sql: str, params: tuple = ()) -> pd.DataFrame:
    # A fresh cursor per query: DuckDB connections aren't thread-safe, and
    # Streamlit runs across threads, so sharing one cursor clobbers results.
    return get_con().cursor().execute(sql, list(params)).df()


@st.cache_data(ttl=600)
def scalar(sql: str, params: tuple = ()) -> int:
    row = get_con().cursor().execute(sql, list(params)).fetchone()
    return row[0] if row and row[0] is not None else 0


# --- Sidebar ----------------------------------------------------------------
st.sidebar.title("Influence Network")
st.sidebar.caption("Parquet + DuckDB build (test)")
page = st.sidebar.radio(
    "View",
    ["Overview", "Organizations (IRS 990)", "Grant network",
     "Shared-personnel network", "Politically active orgs", "Super PAC spending",
     "Lobbying \u2192 Bills",
     "Org \u2192 Policy links"],
)
st.sidebar.caption(f"Source: {PARQUET_BASE}")


def page_overview() -> None:
    st.title("Overview")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("IRS 990 filings", f"{scalar('SELECT SUM(filings) FROM dash_filings_by_year'):,}")
    c2.metric("Organizations", f"{scalar('SELECT COUNT(*) FROM organizations'):,}")
    c3.metric("Super PACs", f"{scalar('SELECT COUNT(*) FROM committee_spending_summary'):,}")
    c4.metric("LDA filings", f"{scalar('SELECT COUNT(*) FROM lda_filings'):,}")

    st.divider()
    left, right = st.columns(2)
    with left:
        st.subheader("990 filings by tax year")
        df = run_query("SELECT tax_year, filings FROM dash_filings_by_year ORDER BY tax_year")
        if not df.empty:
            st.plotly_chart(px.bar(df, x="tax_year", y="filings"), use_container_width=True)
    with right:
        st.subheader("Most-lobbied bills")
        df = run_query(
            "SELECT bill_id, lobbying_filings FROM dash_most_lobbied_bills "
            "ORDER BY lobbying_filings DESC LIMIT 15")
        if not df.empty:
            st.plotly_chart(
                px.bar(df.sort_values("lobbying_filings"), x="lobbying_filings",
                       y="bill_id", orientation="h"), use_container_width=True)


def page_organizations() -> None:
    st.title("Organizations (IRS 990)")
    query = st.text_input("Search organization name or EIN", "")
    if not query:
        st.info("Enter part of an organization name or an EIN to search.")
        return
    q = query.strip()
    if q.isdigit():
        orgs = run_query("""
            SELECT ein, filer_name, COUNT(*) AS filings, MAX(tax_year) AS latest_year,
                   SUM(total_revenue) AS total_revenue, SUM(total_expenses) AS total_expenses
            FROM irs990_filings WHERE ein = ?
            GROUP BY ein, filer_name ORDER BY total_revenue DESC LIMIT 100
        """, (q,))
    else:
        orgs = run_query("""
            SELECT ein, filer_name, COUNT(*) AS filings, MAX(tax_year) AS latest_year,
                   SUM(total_revenue) AS total_revenue, SUM(total_expenses) AS total_expenses
            FROM irs990_filings
            WHERE ein IN (SELECT ein FROM organizations WHERE current_name ILIKE ? LIMIT 300)
            GROUP BY ein, filer_name ORDER BY total_revenue DESC LIMIT 100
        """, (f"%{q}%",))
    if orgs.empty:
        st.warning("No organizations found.")
        return
    st.caption(f"{len(orgs)} matching organizations (top 100 by revenue)")
    st.dataframe(orgs, use_container_width=True, hide_index=True)

    ein = st.selectbox("Inspect an EIN", orgs["ein"].tolist(),
                       format_func=lambda e: f"{e} — {orgs.loc[orgs.ein == e, 'filer_name'].iloc[0]}")
    if not ein:
        return
    st.subheader("Filing history")
    st.dataframe(run_query("""
        SELECT tax_year, form_type, total_revenue, total_expenses,
               political_activity_flag, mission
        FROM irs990_filings WHERE ein = ? ORDER BY tax_year DESC
    """, (ein,)), use_container_width=True, hide_index=True)
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Grants paid (Schedule I)")
        st.dataframe(run_query("""
            SELECT tax_year, grantee_name, grantee_ein, amount
            FROM org_grants
            WHERE grantor_ein = ? ORDER BY amount DESC LIMIT 200
        """, (ein,)), use_container_width=True, hide_index=True)
    with col2:
        st.subheader("Lobbying linked to this org")
        links = run_query("""
            SELECT DISTINCT filing_year, client_name, bill_id, title, reported_lobbying_amount
            FROM organization_policy_links WHERE ein = ?
            ORDER BY reported_lobbying_amount DESC LIMIT 200
        """, (ein,))
        if links.empty:
            st.caption("No approved lobbying links for this org.")
        else:
            st.dataframe(links, use_container_width=True, hide_index=True)


def page_committees() -> None:
    st.title("Super PAC spending")
    min_disb = st.slider("Minimum total disbursements ($M)", 0, 100, 1) * 1_000_000
    df = run_query("""
        SELECT name, committee_type, cycle, total_receipts, total_disbursements,
               independent_expenditures, cash_on_hand_end_period
        FROM committee_spending_summary WHERE total_disbursements >= ?
        ORDER BY total_disbursements DESC
    """, (min_disb,))
    st.caption(f"{len(df):,} committees at or above ${min_disb/1e6:.0f}M disbursements")
    if not df.empty:
        st.plotly_chart(
            px.bar(df.head(20).sort_values("total_disbursements"),
                   x="total_disbursements", y="name", orientation="h",
                   labels={"total_disbursements": "Total disbursements ($)", "name": ""}),
            use_container_width=True)
    st.dataframe(df, use_container_width=True, hide_index=True)


def page_political() -> None:
    st.title("Politically active organizations")
    only_flagged = st.checkbox("Only orgs flagged for political activity", value=False)
    limit = st.slider("How many organizations", 20, 300, 100)
    where = "WHERE lobbying_spend > 0"
    if only_flagged:
        where += " AND political_flag = 1"
    df = run_query(f"""
        SELECT ein, filer_name, political_flag, latest_year, lobbying_spend, filings
        FROM dash_political_orgs {where}
        ORDER BY lobbying_spend DESC LIMIT ?
    """, (limit,))
    if df.empty:
        st.warning("No matching organizations.")
        return
    st.plotly_chart(
        px.bar(df.head(20).sort_values("lobbying_spend"),
               x="lobbying_spend", y="filer_name", orientation="h",
               labels={"lobbying_spend": "Reported lobbying spend ($)", "filer_name": ""}),
        use_container_width=True)
    st.dataframe(df, use_container_width=True, hide_index=True)


def page_lobbying() -> None:
    st.title("Lobbying → Bills")
    tab1, tab2 = st.tabs(["Most-lobbied bills", "Search by client"])
    with tab1:
        st.dataframe(run_query("""
            SELECT bill_id, title, policy_area, lobbying_filings, distinct_clients
            FROM dash_most_lobbied_bills ORDER BY lobbying_filings DESC LIMIT 200
        """), use_container_width=True, hide_index=True)
    with tab2:
        client = st.text_input("Client name contains", "")
        if client:
            df = run_query("""
                SELECT DISTINCT filing_year, client_name, bill_id, title, policy_area,
                       reported_lobbying_amount
                FROM lobbying_bill_facts
                WHERE client_name ILIKE ? AND bill_id IS NOT NULL
                ORDER BY filing_year DESC LIMIT 500
            """, (f"%{client.strip()}%",))
            if df.empty:
                st.warning("No matching lobbying records.")
            else:
                st.dataframe(df, use_container_width=True, hide_index=True)


def page_policy_links() -> None:
    st.title("Org → Policy links")
    st.caption("Only reviewer-approved entity matches connect an EIN to a bill. "
               "Search by client name or EIN.")
    query = st.text_input("Filter by client name or EIN", "")
    if not query:
        st.info("Enter a client name or EIN to see approved org \u2192 bill links.")
        return
    q = query.strip()
    if q.isdigit():
        where, param = "WHERE ein = ?", (q,)
    else:
        where, param = "WHERE client_name ILIKE ?", (f"%{q}%",)
    df = run_query(f"""
        SELECT client_name, ein, bill_id, title, policy_area,
               COUNT(*) AS supporting_rows, SUM(reported_lobbying_amount) AS reported_amount
        FROM organization_policy_links
        {where}
        GROUP BY client_name, ein, bill_id, title, policy_area
        ORDER BY reported_amount DESC LIMIT 500
    """, param)
    if df.empty:
        st.warning("No approved policy links for that search.")
        return
    st.caption(f"{len(df)} links")
    st.dataframe(df, use_container_width=True, hide_index=True)


@st.cache_data(ttl=600)
def org_name(ein: str) -> str:
    df = run_query("SELECT current_name FROM organizations WHERE ein = ?", (ein,))
    if not df.empty and df.iloc[0, 0]:
        return df.iloc[0, 0]
    return ein


def page_network() -> None:
    st.title("Grant network")
    st.caption("Grant flows between organizations (990 Schedule I).")
    query = st.text_input("Search a center organization by name or EIN", "")
    if not query:
        st.info("Search for an organization to center the network on.")
        return
    q = query.strip()
    if q.isdigit():
        matches = run_query(
            "SELECT ein, current_name FROM organizations WHERE ein = ? LIMIT 50", (q,))
    else:
        matches = run_query(
            "SELECT ein, current_name FROM organizations WHERE current_name ILIKE ? "
            "ORDER BY current_name LIMIT 50", (f"%{q}%",))
    if matches.empty:
        st.warning("No organizations found.")
        return
    center = st.selectbox(
        "Center organization", matches["ein"].tolist(),
        format_func=lambda e: f"{matches.loc[matches.ein == e, 'current_name'].iloc[0]} ({e})")
    max_neighbors = st.slider("Max neighbors per direction", 5, 40, 15)

    out_edges = run_query("""
        SELECT source_ein, target_ein, amount FROM grant_network_edges
        WHERE source_ein = ? AND target_ein <> '' ORDER BY amount DESC LIMIT ?
    """, (center, max_neighbors))
    in_edges = run_query("""
        SELECT source_ein, target_ein, amount FROM grant_network_edges
        WHERE target_ein = ? AND source_ein <> '' ORDER BY amount DESC LIMIT ?
    """, (center, max_neighbors))
    edges = pd.concat([out_edges, in_edges], ignore_index=True)
    if edges.empty:
        st.warning("This organization has no recorded grant edges.")
        return

    g = nx.DiGraph()
    for _, r in edges.iterrows():
        g.add_edge(r["source_ein"], r["target_ein"], weight=float(r["amount"] or 0))
    pos = nx.spring_layout(g, seed=42, k=0.6)
    names = {n: org_name(n) for n in g.nodes()}
    max_w = max((d["weight"] for *_, d in g.edges(data=True)), default=1) or 1

    edge_traces = []
    for u, v, d in g.edges(data=True):
        x0, y0 = pos[u]; x1, y1 = pos[v]
        edge_traces.append(go.Scatter(
            x=[x0, x1, None], y=[y0, y1, None], mode="lines",
            line=dict(width=1 + 5 * d["weight"] / max_w, color="rgba(120,120,120,0.4)"),
            hoverinfo="none", showlegend=False))
    node_x, node_y, text, size, color = [], [], [], [], []
    for n in g.nodes():
        x, y = pos[n]; node_x.append(x); node_y.append(y)
        vol = sum(d["weight"] for *_, d in g.out_edges(n, data=True)) + \
              sum(d["weight"] for *_, d in g.in_edges(n, data=True))
        text.append(f"{names[n]}<br>EIN {n}<br>${vol:,.0f}")
        size.append(12 if n == center else 8 + 14 * (vol / max_w if max_w else 0))
        color.append("#d62728" if n == center else "#1f77b4")
    node_trace = go.Scatter(
        x=node_x, y=node_y, mode="markers", hoverinfo="text", text=text,
        marker=dict(size=size, color=color, line=dict(width=1, color="white")),
        showlegend=False)
    fig = go.Figure(edge_traces + [node_trace])
    fig.update_layout(height=650, margin=dict(l=0, r=0, t=0, b=0),
                      xaxis=dict(visible=False), yaxis=dict(visible=False))
    st.plotly_chart(fig, use_container_width=True)

    disp = edges.copy()
    disp["grantor"] = disp["source_ein"].map(names).fillna(disp["source_ein"])
    disp["grantee"] = disp["target_ein"].map(names).fillna(disp["target_ein"])
    st.dataframe(disp[["grantor", "grantee", "amount"]].sort_values("amount", ascending=False),
                 use_container_width=True, hide_index=True)


def page_people_network() -> None:
    st.title("Shared-personnel network")
    st.caption("Organizations linked when they list the same officer or director "
               "on their 990s. Note: this scans a 33M-row table from S3, so it is "
               "the slowest page.")
    query = st.text_input("Search a center organization by name or EIN", "")
    if not query:
        st.info("Search for an organization to map its shared-personnel connections.")
        return
    q = query.strip()
    if q.isdigit():
        matches = run_query(
            "SELECT ein, current_name AS filer_name FROM organizations WHERE ein = ? LIMIT 50", (q,))
    else:
        matches = run_query(
            "SELECT ein, current_name AS filer_name FROM organizations "
            "WHERE current_name ILIKE ? ORDER BY current_name LIMIT 50", (f"%{q}%",))
    if matches.empty:
        st.warning("No organizations found.")
        return
    center = st.selectbox(
        "Center organization", matches["ein"].tolist(),
        format_func=lambda e: f"{matches.loc[matches.ein == e, 'filer_name'].iloc[0]} ({e})")
    center_name = matches.loc[matches.ein == center, "filer_name"].iloc[0]
    max_people = st.slider("Max shared people to trace", 5, 40, 20)

    people = run_query("""
        SELECT DISTINCT p.person_name
        FROM irs990_filings f JOIN irs990_filing_people p USING (filing_id)
        WHERE f.ein = ? AND (p.is_officer = 1 OR p.is_indiv_trustee_or_director = 1)
          AND p.person_name IS NOT NULL AND length(p.person_name) > 6
        LIMIT ?
    """, (center, max_people))
    if people.empty:
        st.warning("No named officers/directors found for this organization.")
        return

    names_list = people["person_name"].tolist()
    ph = ",".join("?" * len(names_list))
    others = run_query(f"""
        WITH matched AS (
            SELECT p.person_name, f.ein, f.filer_name,
                   ROW_NUMBER() OVER (PARTITION BY p.person_name ORDER BY f.ein) AS rn
            FROM irs990_filing_people p JOIN irs990_filings f USING (filing_id)
            WHERE p.person_name IN ({ph}) AND f.ein <> ?
        )
        SELECT person_name, ein, filer_name FROM matched WHERE rn <= 8
    """, tuple(names_list) + (center,))

    connections: dict[str, dict] = {}
    for _, o in others.iterrows():
        rec = connections.setdefault(o["ein"], {"name": o["filer_name"], "people": set()})
        rec["people"].add(o["person_name"])
    if not connections:
        st.info("No other organizations share officers/directors with this org.")
        return

    g = nx.Graph()
    g.add_node(center)
    for ein, rec in connections.items():
        g.add_edge(center, ein, shared=len(rec["people"]))
    pos = nx.spring_layout(g, seed=42, k=0.7)
    edge_x, edge_y = [], []
    for u, v in g.edges():
        x0, y0 = pos[u]; x1, y1 = pos[v]
        edge_x += [x0, x1, None]; edge_y += [y0, y1, None]
    edge_trace = go.Scatter(x=edge_x, y=edge_y, mode="lines",
                            line=dict(width=1, color="rgba(120,120,120,0.4)"),
                            hoverinfo="none", showlegend=False)
    node_x, node_y, text, size, color = [], [], [], [], []
    for n in g.nodes():
        x, y = pos[n]; node_x.append(x); node_y.append(y)
        if n == center:
            text.append(f"{center_name}<br>EIN {n}<br>center"); size.append(20); color.append("#d62728")
        else:
            rec = connections[n]; shared = ", ".join(sorted(rec["people"]))
            text.append(f"{rec['name']}<br>EIN {n}<br>shared: {shared}")
            size.append(8 + 3 * len(rec["people"])); color.append("#2ca02c")
    node_trace = go.Scatter(x=node_x, y=node_y, mode="markers", hoverinfo="text", text=text,
                            marker=dict(size=size, color=color, line=dict(width=1, color="white")),
                            showlegend=False)
    fig = go.Figure([edge_trace, node_trace])
    fig.update_layout(height=650, margin=dict(l=0, r=0, t=0, b=0),
                      xaxis=dict(visible=False), yaxis=dict(visible=False))
    st.plotly_chart(fig, use_container_width=True)
    rows = [{"organization": rec["name"], "ein": ein,
             "shared_people": ", ".join(sorted(rec["people"])), "count": len(rec["people"])}
            for ein, rec in connections.items()]
    st.dataframe(pd.DataFrame(rows).sort_values("count", ascending=False),
                 use_container_width=True, hide_index=True)


PAGES = {
    "Overview": page_overview,
    "Organizations (IRS 990)": page_organizations,
    "Grant network": page_network,
    "Shared-personnel network": page_people_network,
    "Politically active orgs": page_political,
    "Super PAC spending": page_committees,
    "Lobbying \u2192 Bills": page_lobbying,
    "Org \u2192 Policy links": page_policy_links,
}
PAGES[page]()
