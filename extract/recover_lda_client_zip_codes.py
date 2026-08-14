import re
import requests
from bs4 import BeautifulSoup

# Account for optional ZIP+4 code which may or may not be separated by a hyphen
ADDRESS_PATTERN = re.compile(r"City(\w+?)State(\w{2})Zip([\d-]+)Country(\w+)")
REPLACEMENT_PATTERN = re.compile(r"\s|\u200b")
CLIENT_START_PATTERN = re.compile(r'.*client name.*', re.IGNORECASE)


def get_client_html_file(url: str) -> BeautifulSoup:
    return BeautifulSoup(requests.get(url).content, 'html.parser')


def get_client_zip_codes(soup: BeautifulSoup) -> tuple[tuple[str], tuple[str]] | None:
    client_section_start = soup.find('td', string=CLIENT_START_PATTERN)
    if not client_section_start:
        return None
    while client_section_start.parent != soup.find('body'):
        client_section_start = client_section_start.parent

    first_zip_group = client_section_start.next_sibling.next_sibling
    address_1 = tuple()
    for el in first_zip_group.descendants:
        text = REPLACEMENT_PATTERN.sub("", el.get_text())
        match = ADDRESS_PATTERN.match(text)
        if match:
            address_1 = match.groups()
            break

    second_zip_group = first_zip_group.next_sibling.next_sibling
    address_2 = tuple()
    for el in second_zip_group.descendants:
        text = REPLACEMENT_PATTERN.sub("", el.get_text())
        match = ADDRESS_PATTERN.match(text)
        if match:
            address_2 = match.groups()
            break

    return address_1, address_2