import asyncio
import json
import os
import re
from datetime import datetime
from playwright.async_api import async_playwright
import ddddocr
import gspread
from google.oauth2.service_account import Credentials

SHEET_NAME = "Tender Tracker"

def get_sheet():
    """Authenticates with Google Sheets using the JSON key from environment variables."""
    creds_json = os.environ.get("GCP_CREDENTIALS")
    if not creds_json:
        raise ValueError("Error: GCP_CREDENTIALS environment secret not found.")
    
    creds_dict = json.loads(creds_json)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(credentials)
    
    sh = gc.open(SHEET_NAME).sheet1
    if len(sh.get_all_values()) == 0:
        sh.append_row([
            "Date Found", "State / Source", "Tender ID", 
            "Department / Org", "Work Description / Scope", 
            "Closing Date", "Tender Link"
        ])
    return sh

def save_to_sheet(sheet, tenders):
    """Appends unique tenders to Google Sheet without duplicating existing IDs."""
    if not tenders:
        print("\n--> No new matching tenders found to append today.")
        return

    existing_records = sheet.get_all_values()
    existing_ids = set()
    if len(existing_records) > 1:
        existing_ids = {row[2].strip() for row in existing_records[1:] if len(row) > 2}

    new_rows = []
    today = datetime.now().strftime("%Y-%m-%d")

    for item in tenders:
        t_id = item["tender_id"].strip()
        if t_id not in existing_ids:
            existing_ids.add(t_id)
            new_rows.append([
                today,
                item["source"],
                t_id,
                item["department"],
                item["title"],
                item["closing_date"],
                item["link"]
            ])

    if new_rows:
        sheet.append_rows(new_rows)
        print(f"\nSUCCESS: Added {len(new_rows)} new opportunities to Google Sheet!")
    else:
        print("\nAll matching opportunities are already logged in the sheet.")

# Target State and Central E-Procurement Gateways
PORTALS = {
    "Maharashtra (MahaTenders / CIDCO / MMRDA)": "https://mahatenders.gov.in/nicgep/app",
    "Goa eProcure (GSIDC / PWD / Transport)": "https://eprocure.goa.gov.in/nicgep/app",
    "Madhya Pradesh (MP Tenders / Metro / MPRDC)": "https://mptenders.gov.in/nicgep/app",
    "CPPP Central Portal (NHAI / MoRTH / MoHUA)": "https://eprocure.gov.in/eprocure/app"
}

# Search terms to query against the portal search bars
SEARCH_TERMS = [
    "consultan",
    "pmc",
    "mobility",
    "transport",
    "electric bus",
    "charging",
    "parking",
    "advisory",
    "empanelment",
    "feasibility"
]

# Comprehensive domain regex covering transport, advisory, infrastructure, and urban mobility
FILTER_REGEX = re.compile(
    r"("
    # EV & Modern Bus Transit
    r"electric\s+bus|e-?bus|ebuses|charging\s+infra|evse|fast\s+charger|battery\s+swapp|"
    r"gross\s+cost\s+contract|\bgcc\b|depot\s+charging|fleet\s+electrif|"
    # Urban Transit Assets & Amenities
    r"bus\s+shelter|bus\s+queue\s+shelter|\bbqs\b|ac\s+bus\s+shelter|bus\s+terminal|"
    r"foot\s*over\s*bridge|\bfob\b|skywalk|pedestrian\s+underpass|subway|"
    r"multi.?modal|mmlp|intermodal|transit.oriented|\btod\b|"
    # Parking & Traffic Engineering
    r"multi.?level\s+car\s+parking|\bmlcp\b|parking\s+management|on-?street\s+parking|"
    r"fastag\s+parking|congestion|traffic\s+engineering|traffic\s+survey|traffic\s+study|"
    r"junction\s+improvement|signaliz|adaptive\s+traffic|\batcs\b|\bits\b|blackspot|"
    # Urban Mobility & Active Transport
    r"urban\s+mobility|comprehensive\s+mobility|\bcmp\b|non.?motorized|\bnmt\b|"
    r"cycle\s+track|complete\s+streets|street\s+design|pedestrianiz|"
    # Highways, Metro & Corridors
    r"metro\s+rail|metro\s+neo|metro\s+lite|brts|ropeway|cable\s+car|"
    r"highways?|expressway|ring\s+road|flyover|elevated\s+corridor|bypass|tunnels?|"
    # Institutional Strengthening & Program Management
    r"technical\s+assistance|capacity\s+strengthen|capacity\s+build|institutional\s+strengthen|"
    r"project\s+management\s+unit|\bpmu\b|project\s+implementation\s+unit|\bpiu\b|"
    r"\bpmc\b|project\s+management\s+consult|project\s+monitoring|independent\s*engineer|"
    r"authority['\s]*s?\s*engineer|proof\s+consultant|technical\s+audit|"
    # Financial, Transaction Advisory & Delivery Models
    r"transaction\s+advis|financial\s+advis|revenue\s+model|tariff\s+study|"
    r"\bppp\b|dbfot|\bham\b|hybrid\s+annuity|viability\s+gap|\bvgf\b|concession\s+agreement|"
    # Procurement Types & Project Reports
    r"empanelment|expression\s+of\s+interest|\beoi\b|\brfp\b|\brfq\b|pre-qualification|"
    r"detailed\s+project\s+report|\bdpr\b|feasibility\s+study|detailed\s+design|smart\s+city"
    r")",
    re.IGNORECASE
)

async def solve_captcha(page, selector, ocr):
    """Takes screenshot of the captcha element and decodes text using local neural net."""
    captcha_el = await page.wait_for_selector(selector, timeout=10000)
    image_bytes = await captcha_el.screenshot()
    solved = ocr.classification(image_bytes)
    return solved.strip().replace(" ", "")

async def query_portal(portal_label, base_url, term, ocr, context):
    page = await context.new_page()
    results = []

    try:
        search_url = f"{base_url}?page=WebActiveTenders&service=page"
        await page.goto(search_url, wait_until="domcontentloaded", timeout=40000)
        await asyncio.sleep(1)

        # Enter search term
        keyword_input = await page.query_selector("input[name='Keyword'], #Keyword, input[type='text']")
        if keyword_input:
            await keyword_input.fill(term)

        # Solve Captcha
        captcha_img = await page.query_selector("#captchaImage")
        if captcha_img:
            for attempt in range(4):
                code = await solve_captcha(page, "#captchaImage", ocr)
                captcha_box = await page.query_selector("#captchaText")
                if captcha_box:
                    await captcha_box.fill(code)

                submit_btn = await page.query_selector("#Submit")
                if submit_btn:
                    await submit_btn.click()

                await page.wait_for_load_state("domcontentloaded")
                await asyncio.sleep(2)

                err = await page.query_selector("text='Invalid Captcha'")
                if not err and not await page.query_selector("#captchaText"):
                    break

                refresh = await page.query_selector("#Image1")
                if refresh:
                    await refresh.click()
                    await asyncio.sleep(1)

        # Parse matching results
        rows = await page.query_selector_all("#table tr, table.list_table tr")
        print(f"[{portal_label}] Query '{term}' -> {max(0, len(rows) - 1)} items.")

        for row in rows:
            text = (await row.inner_text()).strip()
            if not text or "Tender Title" in text or "S.No" in text:
                continue

            if FILTER_REGEX.search(text):
                cells = await row.query_selector_all("td")
                if len(cells) < 3:
                    continue

                link_el = await row.query_selector("a")
                href = await link_el.get_attribute("href") if link_el else ""
                raw_title = (await link_el.inner_text()).strip() if link_el else text[:120]

                # Date in GePNIC
                closing_date = (await cells[2].inner_text()).strip() if len(cells) > 2 else "Check Link"
                
                # Department/Agency
                department = "State Agency / ULB"
                if len(cells) >= 5:
                    department = (await cells[1].inner_text()).strip()

                # Tender ID extraction
                raw_id = (await cells[len(cells) - 1].inner_text()).strip() if len(cells) > 3 else ""
                id_match = re.search(r"(\d{4}_[A-Z0-9]+_\d+|\b\d{6,}\b)", raw_id + " " + text)
                tender_id = id_match.group(0) if id_match else re.sub(r"\W+", "_", raw_title[:30])

                clean_title = re.sub(r"\s+", " ", raw_title).strip()
                full_link = f"{base_url}{href}" if href.startswith("?") else href

                results.append({
                    "source": portal_label.split(" (")[0],
                    "tender_id": tender_id,
                    "department": department,
                    "title": clean_title,
                    "closing_date": closing_date,
                    "link": full_link or base_url
                })

    except Exception as e:
        print(f"[{portal_label}] Query '{term}' skipped: {e}")
    finally:
        await page.close()

    return results

async def main():
    sheet = get_sheet()
    ocr = ddddocr.DdddOcr(show_ad=False)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            locale="en-IN",
            timezone_id="Asia/Kolkata"
        )

        all_tenders = []
        for label, url in PORTALS.items():
            print(f"\nScanning Portal: {label}")
            for term in SEARCH_TERMS:
                matches = await query_portal(label, url, term, ocr, context)
                all_tenders.extend(matches)

        await browser.close()

    save_to_sheet(sheet, all_tenders)

if __name__ == "__main__":
    asyncio.run(main())
