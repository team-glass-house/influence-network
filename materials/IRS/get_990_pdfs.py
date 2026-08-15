"""Iteratively save IRS-990 files, and its variants, across a select range of years.
"""
import argparse
import requests
from datetime import date
from pathlib import Path

def main():
    max_year = 2025
    min_year = 2012
    pdf_types = {
        'f': 'Form',
        'i': 'Instructions'
    }
    return_types = [
        '990',
        '990EZ'
    ]

    access_documentation = ''
    for pdf_type, pdf_type_desc in pdf_types.items():
        access_documentation += f'{pdf_type_desc}\n==========\n'
        for return_type in return_types:
            access_documentation += f'{return_type}\n----------\n'
            for year in range(min_year, max_year + 1):
                directory = Path('materials') / '990 PDFs' /  f'{return_type} {pdf_type_desc}'

                if not directory.is_dir():
                    directory.mkdir()

                url = f"https://www.irs.gov/pub/irs-prior/{pdf_type}{return_type.lower()}--{year}.pdf"
                _990_pdf_content = requests.get(url).content

                file_name = f"{pdf_type}{return_type}-{year}.pdf"
                with open(directory / file_name, mode="wb") as pdf:
                    pdf.write(_990_pdf_content)
                access_documentation += f"{year}: Accessed the {return_type} {pdf_type_desc} PDF for {year} on {date.today()} at {url}.\n"
            access_documentation += f"These files were stored in {repr(str(directory))}\n"
            access_documentation += f'\n\n'
        with open(Path('materials') / '990 PDFs' / '_Access Documentation.txt', mode='w') as f:
            f.write(access_documentation)

if __name__ == "__main__":
    exit(main())