from .website_crawler import normalize_url


def semantic_verification(url: str | None) -> bool:
    return normalize_url(url) is not None


def iterate_through_patterns(str_to_search: str, *patterns: object) -> bool:
    return any(pattern.match(str_to_search) for pattern in patterns)
