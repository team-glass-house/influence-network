# IRS Form 990 XML schema notes

Working notes on quirks and ambiguities in the IRS 990 e-file XML schema,
gathered while building `irs990.py`. Carried over from the
`cpleman-nonprofit-data` branch's schema research; kept as reference for
anyone extending the parser to new schedules or tax years.

* Orgs can file 990s and 990-Ts in the same year (e.g., EIN 2517121 | most recent [990-T pdf](https://apps.irs.gov/pub/epostcard/cor/272517121_202506_990T_2026042224116911.pdf))

* *H(a)* Is this a group retrun for subordinates?
    * No --> `<GroupReturnForAffiliatesInd>false</GroupReturnForAffiliatesInd>`
        * *OR* --> `<GroupReturnForAffiliatesInd>0</GroupReturnForAffiliatesInd>`
    * File/Example used for this:
        * Ex 1:
            * File: 202641139349301904_public.xml
            * EIN: 237216506
            * Link to 2024 [PDF](https://apps.irs.gov/pub/epostcard/cor/237216506_202412_990O_2026032524042773.pdf)
        * Ex 2:
            * File: 202641139349301924_public.xml
            * EIN: 452499732
            * Link to 2024 [PDF](https://apps.irs.gov/pub/epostcard/cor/237216506_202412_990O_2026032524042773.pdf)


* *I* Tax-exempt status:
    * 501(c)(3) --> `<Organization501c3Ind>X</Organization501c3Ind>`
    * 501 (c) (NUM) (insert no.) --> `<Organization501cInd organization501cTypeTxt="NUM">X</Organization501cInd>`
    * File/Example used for this:
        * Ex 1:
            * File: 202641139349301904_public.xml
            * EIN: 237216506
            * Link to 2024 [PDF](https://apps.irs.gov/pub/epostcard/cor/237216506_202412_990O_2026032524042773.pdf)
        * Ex 2:
            * File: 202641139349301924_public.xml
            * EIN: 452499732
            * Link to 2024 [PDF](https://apps.irs.gov/pub/epostcard/cor/237216506_202412_990O_2026032524042773.pdf)

* *J* Website:
    * NO RESPONSE --> NO ELEMENT
    * TEXT PROVIDED --> `<WebsiteAddressTxt>WEB ADDRESS</WebsiteAddressTxt>`
    * File/Example used for this:
        * Ex 1:
            * File: 202641139349301904_public.xml
            * EIN: 237216506
            * Link to 2024 [PDF](https://apps.irs.gov/pub/epostcard/cor/237216506_202412_990O_2026032524042773.pdf)
        * Ex 2:
            * File: 202641139349301924_public.xml
            * EIN: 452499732
            * Link to 2024 [PDF](https://apps.irs.gov/pub/epostcard/cor/237216506_202412_990O_2026032524042773.pdf)

* *K* Form of organization:
    * Corporation --> `<TypeOfOrganizationCorpInd>X</TypeOfOrganizationCorpInd>`
    * Other --> `<TypeOfOrganizationOtherInd>X</TypeOfOrganizationOtherInd>`
        * *AND* --> `<OtherOrganizationDsc>INSERTED DESC</OtherOrganizationDsc>`
    * File/Example used for this:
        * Ex 1:
            * File: 202641139349301904_public.xml
            * EIN: 237216506
            * Link to 2024 [PDF](https://apps.irs.gov/pub/epostcard/cor/237216506_202412_990O_2026032524042773.pdf)
        * Ex 2:
            * File: 202641139349301924_public.xml
            * EIN: 452499732
            * Link to 2024 [PDF](https://apps.irs.gov/pub/epostcard/cor/237216506_202412_990O_2026032524042773.pdf)

### Part 1

* Abbrevations (in xml):
    * CY --> Current Year
    * PY --> Previous Year
    * BOY --> Beginning of Current Year
    * EOY --> End of Current Year
    * Ind --> Indicator(?)

* Mission or activities:
    * Seems to be dependent on response to *K*:
        * OTHER --> `<OtherOrganizationDsc>Fraternal Organization</OtherOrganizationDsc>`
    * However, they still have the same element other orgs have that don't indicate other:
        * `<ActivityOrMissionDesc>Fraternal Organization</ActivityOrMissionDesc>`
    * There is a 2nd/3rd `<MissionDesc>` field/element in Part III

#### Expenses
* *16b* Total fundraising expenses:
    * Doesn't seem to have prior year option

### Part VI - Governnance, Management, and Disclosure

#### Section A. Governing B.ody and Management
* 1a/1b - What does it mean for a voting member to be independent?

### Part X - Balance Sheet
#### Assets
* 3 - What are "Pledges and grants receivable, net"?

#### Liabilities
* 18 - What are "Grants payable"?
* 19 - What is "Deferred revenue"?

#### Net Assets or Fund Balances
* What is "FASB ASC 958"?
* When did Q27-29 change?
* What is the significance of the change?
    * Do they mean the same thing?
    * Are they a 1:1 change?

* Note: Questions 27-28/29 change at some point:
    * Q27: Unrestricted net assets --> Net assets without donor restrictions
    * Q28: Temporarily restricted net assets --> Net assets with donor restrictions
    * Q28: Permanently restricted net assets --> *There is no equivalent*
    * **It's not clear if this is a 1:1 change. It's more likely that all three questions were replaced with two**

## Schedule A - Public Charity Status and Public Support

### Part I - Reason for Public Charity Status
* What type of organizations are described in 509(a)(1)?
* What type of organizations are described in 509(a)(2)?
* What type of organizations are described in 509(a)(3)?

### Part II - Support Schedule for Organizations Described in Sections 170(b)(1)(A)(iv) and 170(b)(1)(A)(vi)
* What are "unusual grant[s]"?

## Schedule C - Political Campaign and Lobbying Activities

### Part I-A - Complete if the organization is exempt under section 501(c) or is a section 527 organization

### Part IV - Supplemental Information
* Could be used to determine which lobbying group(s) and org might be part of (see [2018 National Jewish Health filing](https://apps.irs.gov/pub/epostcard/cor/742044647_201906_990_2021012117618272.pdf)).
