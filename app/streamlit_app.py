"""Streamlit dashboard for the influence-network dataset.

Explores IRS 990 filings, Super PAC spending, lobbying-to-bill links, and
approved organization-to-policy connections from one SQLite database.

Run:
    streamlit run app/streamlit_app.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import networkx as nx
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DB_PATH = ROOT / "data" / "irs990_full.db"

st.set_page_config(page_title="Influence Network Explorer", layout="wide")


@st.cache_resource
def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


@st.cache_data(ttl=600)
def run_query(sql: str, params: tuple = ()) -> pd.DataFrame:
    return pd.read_sql_query(sql, get_connection(), params=params)


@st.cache_data(ttl=600)
def scalar(sql: str, params: tuple = ()) -> int:
    cur = get_connection().execute(sql, params)
    row = cur.fetchone()
    return row[0] if row else 0


if not DB_PATH.exists():
    st.error(f"Database not found at {DB_PATH}. Load data first.")
    st.stop()


# --- Sidebar ----------------------------------------------------------------
st.sidebar.title("Influence Network")
page = st.sidebar.radio(
    "View",
    ["Overview", "Organizations (IRS 990)", "Grant network",
     "Shared-personnel network", "Politically active orgs",
     "Super PAC spending", "Lobbying \u2192 Bills",
     "Lobbying \u2194 Bill alignment", "Figure 3 - What they lobby on",
     "Figure 4 - Organization to legislation", "Org \u2192 Policy links"],
)
st.sidebar.caption(f"Source: {DB_PATH.name}")


# --- Overview ---------------------------------------------------------------
def page_overview() -> None:
    st.title("Overview")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("IRS 990 filings", f"{scalar('SELECT COUNT(*) FROM irs990_filings'):,}")
    c2.metric("Organizations", f"{scalar('SELECT COUNT(*) FROM organizations'):,}")
    c3.metric("Super PACs", f"{scalar('SELECT COUNT(*) FROM committee_spending_summary'):,}")
    c4.metric("LDA filings", f"{scalar('SELECT COUNT(*) FROM lda_filings'):,}")

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Congress bills", f"{scalar('SELECT COUNT(*) FROM bills'):,}")
    c6.metric("Lobbying activities", f"{scalar('SELECT COUNT(*) FROM lda_lobbying_activities'):,}")
    c7.metric("Approved entity links",
              f"{scalar('SELECT COUNT(*) FROM approved_external_entity_links'):,}")
    c8.metric("990 grants", f"{scalar('SELECT COUNT(*) FROM irs990_filing_grants'):,}")

    st.divider()
    left, right = st.columns(2)

    with left:
        st.subheader("990 filings by tax year")
        df = run_query("""
            SELECT tax_year, filings
            FROM dash_filings_by_year
            ORDER BY tax_year
        """)
        if not df.empty:
            st.plotly_chart(px.bar(df, x="tax_year", y="filings"), use_container_width=True)

    with right:
        st.subheader("Top bill policy areas")
        df = run_query("""
            SELECT policy_area, COUNT(*) AS bills
            FROM bills WHERE policy_area IS NOT NULL AND policy_area <> ''
            GROUP BY policy_area ORDER BY bills DESC LIMIT 15
        """)
        if not df.empty:
            st.plotly_chart(
                px.bar(df.sort_values("bills"), x="bills", y="policy_area",
                       orientation="h"),
                use_container_width=True,
            )


# --- Organizations ----------------------------------------------------------
def page_organizations() -> None:
    st.title("Organizations (IRS 990)")
    query = st.text_input("Search organization name or EIN", "")

    if not query:
        st.info("Enter part of an organization name or an EIN to search.")
        return

    q = query.strip()
    if q.isdigit():
        # Exact EIN lookup — uses idx_irs990_filings_ein_year (instant).
        orgs = run_query("""
            SELECT ein, filer_name,
                   COUNT(*) AS filings,
                   MAX(tax_year) AS latest_year,
                   SUM(total_revenue) AS total_revenue,
                   SUM(total_expenses) AS total_expenses
            FROM irs990_filings
            WHERE ein = ?
            GROUP BY ein, filer_name
            ORDER BY total_revenue DESC
            LIMIT 100
        """, (q,))
    else:
        # Name search: resolve candidate EINs from the smaller organizations
        # table first, then aggregate only those filings via the EIN index.
        like = f"%{q}%"
        orgs = run_query("""
            SELECT ein, filer_name,
                   COUNT(*) AS filings,
                   MAX(tax_year) AS latest_year,
                   SUM(total_revenue) AS total_revenue,
                   SUM(total_expenses) AS total_expenses
            FROM irs990_filings
            WHERE ein IN (
                SELECT ein FROM organizations
                WHERE current_name LIKE ?
                LIMIT 300
            )
            GROUP BY ein, filer_name
            ORDER BY total_revenue DESC
            LIMIT 100
        """, (like,))

    if orgs.empty:
        st.warning("No organizations found.")
        return

    st.caption(f"{len(orgs)} matching organizations (top 100 by revenue)")
    st.dataframe(orgs, use_container_width=True, hide_index=True)

    eins = orgs["ein"].tolist()
    ein = st.selectbox("Inspect an EIN", eins,
                       format_func=lambda e: f"{e} — {orgs.loc[orgs.ein == e, 'filer_name'].iloc[0]}")
    if not ein:
        return

    st.subheader("Filing history")
    filings = run_query("""
        SELECT tax_year, form_type,
               doing_business_as_name,
               filer_address AS address_line1,
               filer_city AS city,
               filer_state AS state,
               filer_zip_code AS zip_code,
               total_revenue, total_expenses,
               political_activity_flag, mission
        FROM irs990_filings WHERE ein = ? ORDER BY tax_year DESC
    """, (ein,))
    address_columns = ["address_line1", "city", "state", "zip_code"]
    if not filings[address_columns].notna().any().any():
        st.warning(
            "No filer mailing addresses are populated in the loaded IRS 990 filings. "
            "Re-ingest the source XML with the current parser to backfill them."
        )
    st.dataframe(filings, use_container_width=True, hide_index=True)

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Grants paid (Schedule I)")
        grants = run_query("""
            SELECT f.tax_year, g.grantee_name, g.grantee_ein, g.amount
            FROM irs990_filings f
            JOIN irs990_filing_grants g USING (filing_id)
            WHERE f.ein = ? AND g.amount IS NOT NULL
            ORDER BY g.amount DESC LIMIT 200
        """, (ein,))
        st.dataframe(grants, use_container_width=True, hide_index=True)
    with col2:
        st.subheader("Lobbying linked to this org")
        links = run_query("""
            SELECT DISTINCT filing_year, client_name, bill_id, title, reported_lobbying_amount
            FROM organization_policy_links
            WHERE ein = ?
            ORDER BY reported_lobbying_amount DESC LIMIT 200
        """, (ein,))
        if links.empty:
            st.caption("No approved lobbying links for this org.")
        else:
            st.dataframe(links, use_container_width=True, hide_index=True)


# --- Super PAC spending -----------------------------------------------------
def page_committees() -> None:
    st.title("Super PAC spending")
    min_disb = st.slider("Minimum total disbursements ($M)", 0, 100, 1) * 1_000_000
    df = run_query("""
        SELECT name, committee_type, cycle, total_receipts, total_disbursements,
               independent_expenditures, cash_on_hand_end_period
        FROM committee_spending_summary
        WHERE total_disbursements >= ?
        ORDER BY total_disbursements DESC
    """, (min_disb,))
    st.caption(f"{len(df):,} committees at or above ${min_disb/1e6:.0f}M disbursements")
    if not df.empty:
        st.plotly_chart(
            px.bar(df.head(20).sort_values("total_disbursements"),
                   x="total_disbursements", y="name", orientation="h",
                   labels={"total_disbursements": "Total disbursements ($)", "name": ""}),
            use_container_width=True,
        )
    st.dataframe(df, use_container_width=True, hide_index=True)


# --- Lobbying -> Bills -------------------------------------------------------
def page_lobbying() -> None:
    st.title("Lobbying → Bills")
    st.caption("Bill references extracted from LDA lobbying-activity descriptions.")

    tab1, tab2 = st.tabs(["Most-lobbied bills", "Search by client"])

    with tab1:
        df = run_query("""
            SELECT bill_id, title, policy_area, lobbying_filings, distinct_clients
            FROM dash_most_lobbied_bills
            ORDER BY lobbying_filings DESC LIMIT 200
        """)
        st.dataframe(df, use_container_width=True, hide_index=True)

    with tab2:
        client = st.text_input("Client name contains", "")
        if client:
            df = run_query("""
                SELECT DISTINCT filing_year, client_name, bill_id, title, policy_area,
                       reported_lobbying_amount
                FROM lobbying_bill_facts
                WHERE client_name LIKE ? AND bill_id IS NOT NULL
                ORDER BY filing_year DESC LIMIT 500
            """, (f"%{client.strip()}%",))
            if df.empty:
                st.warning("No matching lobbying records.")
            else:
                st.dataframe(df, use_container_width=True, hide_index=True)


# --- Figure 3: policy-area concentration -------------------------------------
def page_figure_three() -> None:
    st.title("Figure 3 - What they lobby on")
    st.caption(
        "Lobbying clusters by policy area or bill. Counts are distinct clients "
        "or LDA filings, so this shows concentration of attention rather than "
        "causal influence."
    )
    dimension = st.radio("Group by", ["Policy area", "Bill"], horizontal=True)
    measure = st.radio(
        "Measure", ["Distinct lobbying clients", "Lobbying filings"], horizontal=True
    )
    top_n = st.slider("Policy areas or bills to show", 5, 40, 15)
    count_expression = (
        "COUNT(DISTINCT client_name)"
        if measure == "Distinct lobbying clients"
        else "COUNT(DISTINCT filing_uuid)"
    )
    if dimension == "Policy area":
        df = run_query(f"""
            SELECT COALESCE(NULLIF(policy_area, ''),
                            NULLIF(general_issue_code, ''),
                            'Unclassified') AS topic,
                   {count_expression} AS count
            FROM lobbying_bill_facts
            WHERE bill_id IS NOT NULL
            GROUP BY topic
            ORDER BY count DESC
            LIMIT ?
        """, (top_n,))
        axis_label = "Policy area"
    else:
        df = run_query(f"""
            SELECT bill_id || ' - ' || COALESCE(title, '(untitled)') AS topic,
                   {count_expression} AS count
            FROM lobbying_bill_facts
            WHERE bill_id IS NOT NULL
            GROUP BY bill_id, title
            ORDER BY count DESC
            LIMIT ?
        """, (top_n,))
        axis_label = "Bill"
    if df.empty:
        st.info("No linked lobbying-to-bill records are available for this figure.")
        return
    df = df.sort_values("count")
    fig = px.bar(
        df,
        x="count",
        y="topic",
        orientation="h",
        labels={"count": measure, "topic": axis_label},
        title=f"{measure} by {dimension.lower()}",
        text="count",
    )
    fig.update_traces(textposition="outside")
    st.plotly_chart(fig, use_container_width=True)
    st.dataframe(
        df.sort_values("count", ascending=False),
        use_container_width=True,
        hide_index=True,
    )


# --- Figure 4: organization -> money -> legislation -------------------------
def page_figure_four() -> None:
    st.title("Figure 4 - Organization to dollars to legislation")
    st.caption(
        "A Sankey view of reviewer-approved organization-to-LDA links. Link "
        "width is reported lobbying income/expense allocated to linked bills; "
        "when one filing names several bills, the same filing amount can appear "
        "in more than one legislative path."
    )
    max_rows = st.slider(
        "Candidate linked paths to inspect", 500, 10000, 4000, step=500
    )
    max_orgs = st.slider("Organizations", 3, 25, 12)
    max_areas = st.slider("Policy areas", 3, 20, 10)
    max_bills = st.slider("Bills", 5, 40, 20)
    paths = run_query("""
        SELECT links.ein, links.client_name, links.bill_id, links.title,
               COALESCE(NULLIF(links.policy_area, ''),
                        NULLIF(issue.issue_code, ''),
                        'Unclassified') AS policy_area,
               SUM(COALESCE(reported_lobbying_amount, 0)) AS dollars
        FROM organization_policy_links AS links
        LEFT JOIN (
            SELECT filing_uuid, bill_id, MAX(general_issue_code) AS issue_code
            FROM lobbying_bill_facts
            GROUP BY filing_uuid, bill_id
        ) AS issue
          ON issue.filing_uuid = links.filing_uuid
         AND issue.bill_id = links.bill_id
        WHERE links.bill_id IS NOT NULL
        GROUP BY links.ein, links.client_name, links.bill_id, links.title,
                 links.policy_area, issue.issue_code
        HAVING dollars > 0
        ORDER BY dollars DESC
        LIMIT ?
    """, (max_rows,))
    if paths.empty:
        st.info("No approved organization-to-legislation links with reported dollars.")
        return

    top_orgs = (
        paths.groupby(["ein", "client_name"], dropna=False)["dollars"]
        .sum()
        .nlargest(max_orgs)
        .reset_index()[["ein", "client_name"]]
    )
    paths = paths.merge(top_orgs, on=["ein", "client_name"], how="inner")
    top_areas = paths.groupby("policy_area")["dollars"].sum().nlargest(max_areas).index
    paths = paths[paths["policy_area"].isin(top_areas)]
    top_bill_ids = paths.groupby("bill_id")["dollars"].sum().nlargest(max_bills).index
    paths = paths[paths["bill_id"].isin(top_bill_ids)]
    if paths.empty:
        st.info("The selected limits left no linked paths to display.")
        return

    org_nodes = {
        f"org:{row.ein}": f"{row.client_name or row.ein}"
        for row in paths[["ein", "client_name"]].drop_duplicates().itertuples()
    }
    area_nodes = {
        f"area:{area}": area for area in sorted(paths["policy_area"].unique())
    }
    bill_nodes = {
        f"bill:{row.bill_id}": f"{row.bill_id} - {(row.title or '')[:55]}"
        for row in paths[["bill_id", "title"]].drop_duplicates().itertuples()
    }
    node_keys = list(org_nodes) + list(area_nodes) + list(bill_nodes)
    labels = list(org_nodes.values()) + list(area_nodes.values()) + list(bill_nodes.values())
    node_index = {key: i for i, key in enumerate(node_keys)}

    org_area = (
        paths.groupby(["ein", "policy_area"], dropna=False)["dollars"]
        .sum()
        .reset_index()
    )
    area_bill = (
        paths.groupby(["policy_area", "bill_id"], dropna=False)["dollars"]
        .sum()
        .reset_index()
    )
    sources = (
        [node_index[f"org:{row.ein}"] for row in org_area.itertuples()]
        + [node_index[f"area:{row.policy_area}"] for row in area_bill.itertuples()]
    )
    targets = (
        [node_index[f"area:{row.policy_area}"] for row in org_area.itertuples()]
        + [node_index[f"bill:{row.bill_id}"] for row in area_bill.itertuples()]
    )
    values = (
        [float(row.dollars or 0) for row in org_area.itertuples()]
        + [float(row.dollars or 0) for row in area_bill.itertuples()]
    )
    fig = go.Figure(go.Sankey(
        arrangement="snap",
        node=dict(
            pad=15,
            thickness=18,
            label=labels,
            color=(
                ["#d62728"] * len(org_nodes)
                + ["#1f77b4"] * len(area_nodes)
                + ["#2ca02c"] * len(bill_nodes)
            ),
            line=dict(color="white", width=0.5),
        ),
        link=dict(source=sources, target=targets, value=values),
    ))
    fig.update_layout(height=750, margin=dict(l=0, r=0, t=10, b=10))
    st.plotly_chart(fig, use_container_width=True)
    st.caption(
        f"{paths['ein'].nunique()} organizations, {paths['policy_area'].nunique()} "
        f"policy areas, and {paths['bill_id'].nunique()} bills shown. "
        "Red = organizations; blue = policy areas; green = bills."
    )
    st.dataframe(
        paths.sort_values("dollars", ascending=False),
        use_container_width=True,
        hide_index=True,
    )


# --- Org -> Policy links -----------------------------------------------------
def page_policy_links() -> None:
    st.title("Org → Policy links")
    st.caption("Only reviewer-approved entity matches connect an EIN to a bill.")
    df = run_query("""
        SELECT client_name, ein, bill_id, title, policy_area,
               COUNT(*) AS supporting_rows,
               SUM(reported_lobbying_amount) AS reported_amount
        FROM organization_policy_links
        GROUP BY client_name, ein, bill_id, title, policy_area
        ORDER BY reported_amount DESC LIMIT 500
    """)
    st.dataframe(df, use_container_width=True, hide_index=True)


# --- Grant network ----------------------------------------------------------
@st.cache_data(ttl=600)
def org_name(ein: str) -> str:
    df = run_query("SELECT current_name FROM organizations WHERE ein = ?", (ein,))
    if not df.empty and df.iloc[0, 0]:
        return df.iloc[0, 0]
    df = run_query("SELECT filer_name FROM irs990_filings WHERE ein = ? LIMIT 1", (ein,))
    return df.iloc[0, 0] if not df.empty and df.iloc[0, 0] else ein


def page_network() -> None:
    st.title("Grant network")
    st.caption("Grant flows between organizations (990 Schedule I). "
               "Arrows point from grantor to grantee; edge width scales with dollars.")

    query = st.text_input("Search a center organization by name or EIN", "")
    if not query:
        st.info("Search for an organization to center the network on.")
        return

    # Step 1: shortlist candidate orgs fast (exact EIN or indexed name search).
    q = query.strip()
    if q.isdigit():
        matches = run_query(
            "SELECT ein, current_name FROM organizations WHERE ein = ? LIMIT 1000", (q,)
        )
    else:
        like = f"%{q}%"
        matches = run_query(
            "SELECT ein, current_name FROM organizations "
            "WHERE current_name LIKE ? ORDER BY current_name LIMIT 1000",
            (like,),
        )
    if matches.empty:
        st.warning("No organizations found.")
        return

    # Step 2: compute grant volume only for the shortlisted EINs (indexed edge
    # lookups), then order the picker by volume. Avoids scanning the full
    # grant_network_edges view for every organization.
    eins = matches["ein"].tolist()
    ph = ",".join("?" * len(eins))
    vol = run_query(
        f"""
        SELECT ein, SUM(amount) AS grant_volume FROM (
            SELECT source_ein AS ein, amount FROM grant_network_edges
            WHERE source_ein IN ({ph})
            UNION ALL
            SELECT target_ein AS ein, amount FROM grant_network_edges
            WHERE target_ein IN ({ph})
        ) GROUP BY ein
        """,
        tuple(eins) + tuple(eins),
    )
    matches = matches.merge(vol, on="ein", how="left")
    matches["grant_volume"] = matches["grant_volume"].fillna(0)
    matches = matches.sort_values("grant_volume", ascending=False).reset_index(drop=True)

    center = st.selectbox(
        "Center organization", matches["ein"].tolist(),
        format_func=lambda e: f"{matches.loc[matches.ein == e, 'current_name'].iloc[0]} ({e})",
    )
    max_neighbors = st.slider("Max neighbors per direction", 5, 40, 15)
    two_hop = st.checkbox("Expand one more hop (slower)", value=False)

    out_edges = run_query("""
        SELECT source_ein, target_ein, amount FROM grant_network_edges
        WHERE source_ein = ? AND target_ein <> '' ORDER BY amount DESC LIMIT ?
    """, (center, max_neighbors))
    in_edges = run_query("""
        SELECT source_ein, target_ein, amount FROM grant_network_edges
        WHERE target_ein = ? AND source_ein <> '' ORDER BY amount DESC LIMIT ?
    """, (center, max_neighbors))
    edges = pd.concat([out_edges, in_edges], ignore_index=True)

    if two_hop and not out_edges.empty:
        for nb in out_edges["target_ein"].tolist()[:10]:
            hop = run_query("""
                SELECT source_ein, target_ein, amount FROM grant_network_edges
                WHERE source_ein = ? AND target_ein <> '' ORDER BY amount DESC LIMIT 5
            """, (nb,))
            edges = pd.concat([edges, hop], ignore_index=True)

    if edges.empty:
        st.warning("This organization has no recorded grant edges.")
        return

    g = nx.DiGraph()
    for _, r in edges.iterrows():
        g.add_edge(r["source_ein"], r["target_ein"], weight=float(r["amount"] or 0))

    pos = nx.spring_layout(g, seed=42, k=0.6)
    names = {n: org_name(n) for n in g.nodes()}
    max_w = max((d["weight"] for *_ , d in g.edges(data=True)), default=1) or 1

    edge_traces = []
    for u, v, d in g.edges(data=True):
        x0, y0 = pos[u]; x1, y1 = pos[v]
        edge_traces.append(go.Scatter(
            x=[x0, x1, None], y=[y0, y1, None], mode="lines",
            line=dict(width=1 + 5 * d["weight"] / max_w, color="rgba(120,120,120,0.4)"),
            hoverinfo="none", showlegend=False,
        ))

    node_x, node_y, text, size, color = [], [], [], [], []
    for n in g.nodes():
        x, y = pos[n]; node_x.append(x); node_y.append(y)
        vol = sum(d["weight"] for *_ , d in g.out_edges(n, data=True)) + \
              sum(d["weight"] for *_ , d in g.in_edges(n, data=True))
        text.append(f"{names[n]}<br>EIN {n}<br>${vol:,.0f}")
        size.append(12 if n == center else 8 + 14 * (vol / max_w if max_w else 0))
        color.append("#d62728" if n == center else "#1f77b4")

    node_trace = go.Scatter(
        x=node_x, y=node_y, mode="markers", hoverinfo="text", text=text,
        marker=dict(size=size, color=color, line=dict(width=1, color="white")),
        showlegend=False,
    )
    fig = go.Figure(edge_traces + [node_trace])
    fig.update_layout(height=650, margin=dict(l=0, r=0, t=0, b=0),
                      xaxis=dict(visible=False), yaxis=dict(visible=False))
    st.plotly_chart(fig, use_container_width=True)
    st.caption(f"{g.number_of_nodes()} organizations, {g.number_of_edges()} grant edges. "
               "Red = center org. Hover a node for name and total grant volume.")

    st.subheader("Edges in view")
    disp = edges.copy()
    disp["grantor"] = disp["source_ein"].map(names).fillna(disp["source_ein"])
    disp["grantee"] = disp["target_ein"].map(names).fillna(disp["target_ein"])
    st.dataframe(disp[["grantor", "grantee", "amount"]].sort_values("amount", ascending=False),
                 use_container_width=True, hide_index=True)

# --- Shared-personnel network -----------------------------------------------
def page_people_network() -> None:
    st.title("Shared-personnel network")
    st.caption("Organizations linked when they list the same officer or director "
               "on their 990s. Name matching is exact and may include common names, "
               "so treat weakly-connected links as leads, not proof.")

    query = st.text_input("Search a center organization by name or EIN", "")
    if not query:
        st.info("Search for an organization to map its shared-personnel connections.")
        return

    q = query.strip()
    if q.isdigit():
        matches = run_query(
            "SELECT ein, current_name AS filer_name FROM organizations "
            "WHERE ein = ? LIMIT 50", (q,)
        )
    else:
        matches = run_query(
            "SELECT ein, current_name AS filer_name FROM organizations "
            "WHERE current_name LIKE ? ORDER BY current_name LIMIT 50",
            (f"%{q}%",),
        )
    if matches.empty:
        st.warning("No organizations found.")
        return

    center = st.selectbox(
        "Center organization", matches["ein"].tolist(),
        format_func=lambda e: f"{matches.loc[matches.ein == e, 'filer_name'].iloc[0]} ({e})",
    )
    center_name = matches.loc[matches.ein == center, "filer_name"].iloc[0]
    max_people = st.slider("Max shared people to trace", 5, 40, 20)

    people = run_query("""
        SELECT DISTINCT p.person_name
        FROM irs990_filings f
        JOIN irs990_filing_people p USING (filing_id)
        WHERE f.ein = ?
          AND (p.is_officer = 1 OR p.is_indiv_trustee_or_director = 1)
          AND p.person_name IS NOT NULL AND length(p.person_name) > 6
        LIMIT ?
    """, (center, max_people))
    if people.empty:
        st.warning("No named officers/directors found for this organization.")
        return

    names = people["person_name"].tolist()
    ph = ",".join("?" * len(names))
    others = run_query(f"""
        WITH matched AS (
            SELECT p.person_name, f.ein, f.filer_name,
                   ROW_NUMBER() OVER (PARTITION BY p.person_name ORDER BY f.ein) AS rn
            FROM irs990_filing_people p
            JOIN irs990_filings f USING (filing_id)
            WHERE p.person_name IN ({ph}) AND f.ein <> ?
        )
        SELECT person_name, ein, filer_name FROM matched WHERE rn <= 8
    """, tuple(names) + (center,))

    connections: dict[str, dict] = {}
    for _, o in others.iterrows():
        rec = connections.setdefault(o["ein"], {"name": o["filer_name"], "people": set()})
        rec["people"].add(o["person_name"])

    if not connections:
        st.info("No other organizations share officers/directors with this org "
                "in the data.")
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
            text.append(f"{center_name}<br>EIN {n}<br>center")
            size.append(20); color.append("#d62728")
        else:
            rec = connections[n]
            shared = ", ".join(sorted(rec["people"]))
            text.append(f"{rec['name']}<br>EIN {n}<br>shared: {shared}")
            size.append(8 + 3 * len(rec["people"])); color.append("#2ca02c")
    node_trace = go.Scatter(x=node_x, y=node_y, mode="markers", hoverinfo="text",
                            text=text, marker=dict(size=size, color=color,
                            line=dict(width=1, color="white")), showlegend=False)
    fig = go.Figure([edge_trace, node_trace])
    fig.update_layout(height=650, margin=dict(l=0, r=0, t=0, b=0),
                      xaxis=dict(visible=False), yaxis=dict(visible=False))
    st.plotly_chart(fig, use_container_width=True)
    st.caption(f"{len(connections)} organizations share at least one officer/director "
               f"with {center_name}. Node size scales with number of shared people.")

    rows = [{"organization": rec["name"], "ein": ein,
             "shared_people": ", ".join(sorted(rec["people"])),
             "count": len(rec["people"])}
            for ein, rec in connections.items()]
    st.dataframe(pd.DataFrame(rows).sort_values("count", ascending=False),
                 use_container_width=True, hide_index=True)


# --- Politically active orgs ------------------------------------------------
def page_political() -> None:
    st.title("Politically active organizations")
    st.caption("Nonprofits reporting lobbying expenditures on 990 Schedule C, "
               "ranked by total reported lobbying spend.")

    only_flagged = st.checkbox("Only orgs flagged for political activity", value=False)
    limit = st.slider("How many organizations", 20, 300, 100)

    where = "WHERE lobbying_spend > 0"
    if only_flagged:
        where += " AND political_flag = 1"
    df = run_query(f"""
        SELECT ein, filer_name, political_flag, latest_year, lobbying_spend, filings
        FROM dash_political_orgs
        {where}
        ORDER BY lobbying_spend DESC
        LIMIT ?
    """, (limit,))

    if df.empty:
        st.warning("No matching organizations.")
        return

    st.plotly_chart(
        px.bar(df.head(20).sort_values("lobbying_spend"),
               x="lobbying_spend", y="filer_name", orientation="h",
               labels={"lobbying_spend": "Reported lobbying spend ($)", "filer_name": ""}),
        use_container_width=True,
    )
    st.dataframe(df, use_container_width=True, hide_index=True)


# --- Lobbying <-> Bill alignment --------------------------------------------
def page_alignment() -> None:
    st.title("Lobbying \u2194 Bill text alignment")
    st.caption("Semantic similarity between lobbying-issue descriptions and a bill's "
               "title/policy area (TF-IDF cosine). The project's headline NLP signal: "
               "how closely lobbying language tracks the bill.")

    query = st.text_input("Search a bill by number or title (e.g. 'hr 1', 'inflation')", "")
    if not query:
        st.info("Search for a bill to score its lobbying-language alignment.")
        return

    like = f"%{query.strip()}%"
    bills = run_query("""
        SELECT bill_id, bill_type, bill_number, title, policy_area
        FROM bills
        WHERE bill_id LIKE ? OR title LIKE ?
        ORDER BY bill_number LIMIT 50
    """, (like, like))
    if bills.empty:
        st.warning("No bills found.")
        return

    bill_id = st.selectbox(
        "Bill", bills["bill_id"].tolist(),
        format_func=lambda b: f"{b} — {(bills.loc[bills.bill_id == b, 'title'].iloc[0] or '')[:80]}",
    )
    brow = bills[bills.bill_id == bill_id].iloc[0]
    bill_text = " ".join(str(x) for x in [brow["title"], brow["policy_area"]] if x)
    st.markdown(f"**Bill text used:** {bill_text or '(no title/policy area)'}")

    descs = run_query("""
        SELECT DISTINCT a.description, l.client_name
        FROM lobbying_bill_links k
        JOIN lda_filings l ON l.filing_uuid = k.filing_uuid
        JOIN lda_lobbying_activities a ON a.filing_uuid = k.filing_uuid
        WHERE k.bill_type = ? AND k.bill_number = ?
          AND a.description IS NOT NULL AND length(a.description) > 15
        LIMIT 300
    """, (brow["bill_type"], int(brow["bill_number"])))

    if descs.empty:
        st.info("No lobbying descriptions are linked to this bill.")
        return
    if not bill_text.strip():
        st.warning("This bill has no title/policy-area text to compare against.")
        return

    from analysis.align import similarity_matrix
    with st.spinner(f"Scoring {len(descs)} lobbying descriptions..."):
        matrix = similarity_matrix(descs["description"].tolist(), [bill_text], method="tfidf")
    descs = descs.assign(alignment=[float(row[0]) for row in matrix])
    descs = descs.sort_values("alignment", ascending=False)

    c1, c2, c3 = st.columns(3)
    c1.metric("Lobbying descriptions", f"{len(descs):,}")
    c2.metric("Max alignment", f"{descs['alignment'].max():.3f}")
    c3.metric("Mean alignment", f"{descs['alignment'].mean():.3f}")

    st.plotly_chart(
        px.histogram(descs, x="alignment", nbins=30,
                     labels={"alignment": "TF-IDF cosine similarity"}),
        use_container_width=True,
    )
    st.subheader("Highest-aligned lobbying descriptions")
    st.dataframe(descs[["alignment", "client_name", "description"]].head(50),
                 use_container_width=True, hide_index=True)

PAGES = {
    "Overview": page_overview,
    "Organizations (IRS 990)": page_organizations,
    "Grant network": page_network,
    "Shared-personnel network": page_people_network,
    "Politically active orgs": page_political,
    "Super PAC spending": page_committees,
    "Lobbying \u2192 Bills": page_lobbying,
    "Lobbying \u2194 Bill alignment": page_alignment,
    "Figure 3 - What they lobby on": page_figure_three,
    "Figure 4 - Organization to legislation": page_figure_four,
    "Org → Policy links": page_policy_links,
}
PAGES[page]()
