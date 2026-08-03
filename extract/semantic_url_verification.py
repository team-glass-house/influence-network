"""This file outlines the iterative method for verifying the semantic form of a website supplied by the 
filers in the IRS 990 returns.
"""
import re
from urllib.parse import urlsplit

import requests

# Some 'popular' domains influenced by contents of our dataset
POPULAR_TOP_LEVEL_DOMAINS = {
    "gov", "edu", "com", "net", "org", "us", "ca", "cc", "tv"
}

top_level_domains = requests.get(
    "https://data.iana.org/TLD/tlds-alpha-by-domain.txt"
).text.split('\n')

IANA_VERSION = top_level_domains[0].strip()


UNPOPULAR_TOP_LEVEL_DOMAINS = {
    tld.lower().strip() for tld
    in top_level_domains[1:]
    if tld.lower().strip() != "" and tld.lower().strip() not in POPULAR_TOP_LEVEL_DOMAINS
}

STRICT_MATCH_POPULAR = re.compile(fr"(www\.)([\w-]+\.)+?({'|'.join(POPULAR_TOP_LEVEL_DOMAINS)})")
SEMI_STRICT_MATCH_POPULAR = re.compile(fr"(www\.)?([\w-]+\.)+?({'|'.join(POPULAR_TOP_LEVEL_DOMAINS)})")

STRICT_MATCH_UNPOPULAR = re.compile(fr"(www\.)([\w-]+\.)+?({'|'.join(UNPOPULAR_TOP_LEVEL_DOMAINS)})")
SEMI_STRICT_MATCH_UNPOPULAR = re.compile(fr"(www\.)?([\w-]+\.)+?({'|'.join(UNPOPULAR_TOP_LEVEL_DOMAINS)})")

SPECIAL_CHARACTERS = re.compile(r'[\(\)"_]')

NULL_ENTRIES = (
    "na", "n\\a", "n/a", "none",
    "notapplicable", "notavailable"
)

def semantic_verification(url: str) -> bool:
    if url == "":
        return False
    url = SPECIAL_CHARACTERS.sub("", url).lower().strip()
    if "@" in url:
        # Emails are passed through sometimes
        return False
    
    if url[:3] == "see":
        # E.g., 'See Schedule O", "See supplmental disclosure"
        return False
    
    for null_entry in NULL_ENTRIES:
        if null_entry == url.replace(" ", ""):
            return False

    split_url = urlsplit(url)
    # Highest level of verification
    if split_url.scheme != "" and split_url.netloc != "":
        return True
    # print(url)
    verification_result = iterate_through_patterns(
        url,
        STRICT_MATCH_POPULAR, STRICT_MATCH_UNPOPULAR,
        SEMI_STRICT_MATCH_POPULAR, SEMI_STRICT_MATCH_UNPOPULAR
    )

    return verification_result

def iterate_through_patterns(str_to_search: str, *patterns: re.Pattern[str]) -> bool:
    for pattern in patterns:
        if pattern.match(str_to_search):
            return True
    return False