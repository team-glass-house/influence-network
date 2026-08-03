"""This transparency index is based on Irvin, R.'s article (https://doi.org/10.1515/npf-2022-0032). Each component is ranked on a scale of 0-1, where a score of 1
indicates lower transparency and a score of 0 indicates higher transparency.
"""
import sqlite3
from pathlib import Path

import pandas as pd

from .semantic_url_verification import *

RELATED_527_QUERY = """-- Some related orgs don't have any EINs reported; see filing_id 1433 for example
    select filing_id, count(filing_id) as num_527s
    from irs990_filing_related_orgs
    where entity_type like '%527%'
    group by filing_id
"""

RELATED_C3_WEB_QUERY = """-- Pre-calculate the metrics of interest
    select distinct r.filing_id, lower(f2.website) as website
    from (
            select distinct r.filing_id, r.ein, f1.tax_year
            from irs990_filing_related_orgs r
            -- Get the tax year associated with the current related org
            left join irs990_filings f1 on
                r.filing_id = f1.filing_id
            where r.ein is not null
        ) r
        -- Get the matching filing data for the identified related org
        inner join irs990_filings f2 on
            r.ein = f2.ein
            and r.tax_year = f2.tax_year
            and f2.exempt_organization_type = '501(c)(3)'
            and f2.website is not null
"""

RELATED_C3_QUERY = """-- Pre-calculate the metrics of interest
    select r.filing_id, count(r.ein) as num_c3s, max(r.voting_members_independent) as max_board_size
    from (
            select distinct r.filing_id, r.ein, f2.voting_members_independent
                from (
                        select distinct r.filing_id, r.ein, f1.tax_year
                        from irs990_filing_related_orgs r
                        -- Get the tax year associated with the current related org
                        left join irs990_filings f1 on
                            r.filing_id = f1.filing_id
                        where r.ein is not null
                    ) r
                    -- Get the matching filing data for the identified related org
                    inner join irs990_filings f2 on
                        r.ein = f2.ein
                        and r.tax_year = f2.tax_year
                        and f2.exempt_organization_type = '501(c)(3)'
        ) r
    group by r.filing_id
"""

POLITICAL_CAMPAIGN_EXPENSES_QUERY = """
    select filing_id, total_exempt_function_expend_amt as political_grants
    from irs990_filing_lobbying
    where total_exempt_function_expend_amt is not null
"""

TRANSPARENCY_INDEX_SOURCE_QUERY = f"""
    select
        f.filing_id,
        f.ein,
        f.total_revenue,
        f.total_expenses,
        f.total_assets,
        f.voting_members_independent,
        f.total_volunteers,
        f.website,
        f.total_salaries,
        f.unrestricted_net_assets_eoy,
        f.fundraising_expenses,
        0 as lobbying,
        _527.num_527s,
        _c3.num_c3s,
        _c3.max_board_size,
        schedule_c.political_grants
    from irs990_filings f
        left join (
            {RELATED_527_QUERY}
        ) _527 on f.filing_id = _527.filing_id
        left join (
            {RELATED_C3_QUERY}
        ) _c3 on f.filing_id = _c3.filing_id
        left join (
            {POLITICAL_CAMPAIGN_EXPENSES_QUERY}
        ) schedule_c on f.filing_id = schedule_c.filing_id
    where
        f.form_type <> '990T' and f.exempt_organization_type = '501(c)(4)'
"""

VOTING_MEMBER_CUTOFF = 25
VOLUNTEER_CUTOFF = 0
RELATED_527_CUTOFF = 0
RELATED_C3_CUTOFF = 0

def get_connection(path_to_db: str | Path) -> sqlite3.Connection:
    return sqlite3.connect(path_to_db)

def get_transparency_index_data(conn: sqlite3.Connection, csv_path: str | None = None) -> pd.DataFrame:
    transparency_data = pd.read_sql(TRANSPARENCY_INDEX_SOURCE_QUERY, conn)
    # Handle null values based on column datatype
    transparency_data = transparency_data.fillna(
        value={col: 0 if transparency_data.loc[:, col].dtype == float else '' for col in transparency_data.columns}
    )
    
    if csv_path not in ("", None):
        transparency_data.to_csv(csv_path, index=False)
    return transparency_data

def calculate_index_components(transparency_df: pd.DataFrame, conn: sqlite3.Connection) -> pd.DataFrame:
    calculated_df = transparency_df.loc[:, ['filing_id', 'ein']]

    # * Board members: scale the largest board size by constant
    calculated_df['board_members'] = transparency_df.loc[:, ['voting_members_independent', 'max_board_size']].max(axis=1) / VOTING_MEMBER_CUTOFF

    # * Volunteers: 0 if total_volunteers > 0 else 1
    calculated_df['volunteers'] = (transparency_df.loc[:, 'total_volunteers'] == VOLUNTEER_CUTOFF).astype(int)

    # TODO: Website
    calculated_df = calculated_df.merge(
        right=website_component(transparency_df.loc[:, ['filing_id', 'website']], conn),
        on="filing_id",
        how="left"
    )

    # * Related 527s: 1 if num_527s > 0 else 0
    calculated_df['related_to_527s'] = (transparency_df.loc[:, 'num_527s'] > RELATED_527_CUTOFF).astype(int)

    # * Related C3s: 1 if num_C3s == 0 else 1
    calculated_df['related_to_C3s'] = (transparency_df.loc[:, 'num_c3s'] == RELATED_C3_CUTOFF).astype(int)

    # * Political Spending: 1 if num_C3s == 0 else 1
    # ! We are missing lobbying expenses
    # TODO: Incorporate political spending
    # calculated_df['political_spending'] = (transparency_df.loc[:, 'num_527'] == RELATED_C3_CUTOFF).astype(int)

    # * Total Salaries: scaled by total_expenses
    calculated_df['total_salaries'] = 1 - (transparency_df.loc[:, 'total_salaries'] / transparency_df.loc[:, 'total_expenses'])
    calculated_df.loc[:, 'total_salaries'] = calculated_df.loc[:, 'total_salaries'].fillna(0)

    # * Unrestricted Net Assets: scaled by triple the total_expenses
    calculated_df['unrestricted_net_assets'] = transparency_df.apply(
        lambda row: unrestricted_net_assets_component(
            total_expenses=row['total_expenses'],
            unrestricted_net_assets=row['unrestricted_net_assets_eoy']
        ),
        axis=1
    )

    # * Fundraising Expenses: scaled by total_revenue
    calculated_df['fundraising_expenses'] = 1 - (transparency_df.loc[:, 'fundraising_expenses'] / transparency_df.loc[:, 'total_revenue'])
    calculated_df.loc[:, 'fundraising_expenses'] = calculated_df.loc[:, 'fundraising_expenses'].fillna(0)

    # * Clamp values to [0, 1] range
    calculated_df[calculated_df.iloc[:, 2:] > 1] = 1
    calculated_df[calculated_df.iloc[:, 2:] < 0] = 0

    # * Final Index score
    calculated_df['index'] = calculated_df.iloc[:, 2:].sum(axis=1)

    return calculated_df


def website_component(website_df: pd.DataFrame, conn: sqlite3.Connection) -> pd.DataFrame:
    # First verification pass
    website_df['c4_valid'] = website_df.loc[:, 'website'].apply(lambda x: semantic_verification(x))

    # Only need to perform second pass on those that weren't successful in the first pass
    filings_missing_website = [str(filing_id) for filing_id in website_df.loc[~website_df.c4_valid, 'filing_id'].unique()]
    c3_web_query = RELATED_C3_WEB_QUERY + f"""
    where r.filing_id in ('{"','".join(filings_missing_website)}')
    """
    c3_websites = pd.read_sql(
        c3_web_query,
        conn
    )

    # Score the c3_websites and choose the best website, accounting for multiple related C3s
    c3_websites['c3_valid'] = c3_websites.website.apply(lambda x: semantic_verification(x))
    c3_websites = c3_websites.loc[:, ['filing_id', 'c3_valid']].groupby('filing_id').max().reset_index()

    # Attach C3 scores for comparison
    website_df = website_df.merge(
        c3_websites,
        on="filing_id",
        how="left"
    )

    # Obtain the highest verification status across the orgs
    website_df['valid'] = website_df.loc[:, ['c4_valid', 'c3_valid']].max(axis=1)

    # Drop intermediate columns
    website_df = website_df.drop(['website', 'c4_valid', 'c3_valid'], axis=1)

    # Rename to match naming convention in the calculate_index_components function
    website_df.columns = ['filing_id', 'website']

    # Invert the score to match higher scores indiciating less tranparency, and convert to an integer for analysis
    website_df['website'] = website_df.loc[:, 'website'].apply(lambda x: 0 if x else 1)

    return website_df

  
def board_member_component(voting_members: float) -> float:
    return min(1, voting_members / VOTING_MEMBER_CUTOFF)

def volunteer_component(volunteers: float) -> float:
    return 1 if volunteers == 0 else 0

def related_to_527_component(num_527s: float) -> float:
    return 1 if num_527s > 0 else 0

def related_to_c3_component(num_c3s: float) -> float:
    return 0 if num_c3s > 0 else 1

def political_expenses_component(total_expenses: float, political_expenses: float) -> float:
    if total_expenses == 0:
        return 0
    return political_expenses / total_expenses

def salary_component(total_expenses: float, total_salaries: float) -> float:
    if total_expenses == 0:
        return 0
    return 1 - (total_salaries / total_expenses)

def unrestricted_net_assets_component(total_expenses: float, unrestricted_net_assets: float) -> float:
    if total_expenses == 0:
        return 0
    return 1 - (unrestricted_net_assets / (3 * total_expenses))

def fundraising_expenses_component(total_revenue: float, fundraising_expenses: float) -> float:
    if total_revenue == 0:
        return 0
    return 1 - (fundraising_expenses / total_revenue)
