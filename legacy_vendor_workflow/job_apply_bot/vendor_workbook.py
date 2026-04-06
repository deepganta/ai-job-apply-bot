import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List
from zipfile import ZipFile

from .models import Vendor
from .utils import ensure_url, normalize_domain, normalize_text


MAIN_NS = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def _shared_strings(archive: ZipFile) -> List[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []

    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    strings: List[str] = []
    for item in root.findall("main:si", MAIN_NS):
        strings.append("".join(node.text or "" for node in item.iterfind(".//main:t", MAIN_NS)))
    return strings


def _sheet_target(archive: ZipFile, sheet_name: str) -> str:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    rel_map = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels}

    for sheet in workbook.find("main:sheets", MAIN_NS):
        if sheet.attrib.get("name") == sheet_name:
            rel_id = sheet.attrib[REL_NS]
            return f"xl/{rel_map[rel_id]}"

    raise ValueError(f"Workbook is missing sheet {sheet_name!r}")


def load_vendors(workbook_path: Path) -> List[Vendor]:
    vendors: List[Vendor] = []
    seen: Dict[str, Vendor] = {}
    with ZipFile(workbook_path) as archive:
        shared_strings = _shared_strings(archive)
        sheet_xml = ET.fromstring(archive.read(_sheet_target(archive, "White Vendors")))
        rows = sheet_xml.find("main:sheetData", MAIN_NS)
        if rows is None:
            return vendors

        for row in list(rows)[1:]:
            values: Dict[str, str] = {}
            for cell in row.findall("main:c", MAIN_NS):
                reference = cell.attrib.get("r", "")
                column = "".join(ch for ch in reference if ch.isalpha())
                value_node = cell.find("main:v", MAIN_NS)
                if value_node is None:
                    continue
                raw = value_node.text or ""
                if cell.attrib.get("t") == "s":
                    value = shared_strings[int(raw)]
                else:
                    value = raw
                values[column] = value.strip()

            company = values.get("A", "").strip()
            website = ensure_url(values.get("B", "").strip())
            if not company or not website:
                continue

            domain = normalize_domain(website)
            site_key = f"{domain}|{website.rstrip('/').lower()}"
            existing = seen.get(site_key)
            if existing is not None:
                if company != existing.name and company not in existing.aliases:
                    existing.aliases.append(company)
                continue

            vendor = Vendor(
                name=company,
                website=website,
                domain=domain,
            )
            seen[site_key] = vendor
            vendors.append(vendor)

    return vendors


def vendor_name_index(vendors: List[Vendor]) -> Dict[str, Vendor]:
    indexed: Dict[str, Vendor] = {}
    for vendor in vendors:
        indexed[normalize_text(vendor.name)] = vendor
        for alias in vendor.aliases:
            indexed[normalize_text(alias)] = vendor
    return indexed


def infer_vendor_from_host(vendors: List[Vendor], host: str) -> Vendor:
    normalized_host = normalize_domain(host)
    for vendor in vendors:
        if normalized_host == vendor.domain or normalized_host.endswith(f".{vendor.domain}"):
            return vendor
    return Vendor(name="", website="", domain="")
