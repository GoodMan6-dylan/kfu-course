#!/usr/bin/env python3
"""Rebuild KFU sections from the six public Banner pages.

The server timer calls this script after updating its Git checkout. The script
only writes data.js; Git commit/push and repeated-failure alerts belong to the
server runner. Exit codes: 0 = all pages valid, 2 = one or more pages failed
(their previous sections were retained), 3 = safety abort (nothing written).
"""

from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from http.client import IncompleteRead
import json
import logging
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen


# Change this one value when the next term begins.
TERM_CODE = "144810"

BANNER_HOST = "ssb-ar.kfu.edu.sa"
BANNER_PATH = "/PROD_ar/ws"
PAGE_DELAY_SECONDS = 2
FETCH_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_DROP_PERCENT = 30
RIYADH_TZ = timezone(timedelta(hours=3))


@dataclass(frozen=True)
class Page:
    name: str
    college_param: str
    sex_param: str
    college_code: str
    expected_college_header: str

    @property
    def url(self) -> str:
        query = urlencode(
            {
                "p_trm_code": TERM_CODE,
                "p_col_code": self.college_param,
                "p_sex_code": self.sex_param,
            }
        )
        return f"https://{BANNER_HOST}{BANNER_PATH}?{query}"

    @property
    def gender_word(self) -> str:
        return "طالبات" if self.sex_param == "12" else "طلاب"


PAGES = (
    Page("eng_m", "22", "11", "22", "الهندسة"),
    Page("sci_m", "08", "11", "08", "العلوم"),
    Page("eng-lang_m", "00", "11", "17", "No College Designated"),
    Page("eng_f", "22", "12", "22F", "الهندسة"),
    Page("sci_f", "08", "12", "08F", "العلوم"),
    Page("eng-lang_f", "00", "12", "17F", "No College Designated"),
)

COLLEGES = {
    "22": "الهندسة",
    "08": "العلوم",
    "17": "مركز اللغات",
    "22F": "الهندسة (طالبات)",
    "08F": "العلوم (طالبات)",
    "17F": "مركز اللغات (طالبات)",
}

# Confirmed against the six saved Banner pages. The prefix identifies a
# department when one Banner heading contains multiple course prefixes.
KNOWN_HEADERS = {
    "22": {
        "هندسة - عامة": {"2200"},
        "الهندسة الميكانيكية": {"2201"},
        "الهندسة الكهربائية": {"2202"},
        "الهندسة المدنية والبيئة": {"2203"},
        "الهندسة الكيميائية": {"2204"},
        "هندسة الطبية الحيوية": {"2206"},
        "الهندسة عام": {"2220"},
    },
    "08": {
        "الفزياء *علوم": {"0814"},
        "الكيمياء *علوم": {"0815", "0825"},
        "الاحياء *علوم": {"0816", "0826"},
        "الرياضيات *علوم": {"0817"},
        "الفيزياء*علوم": {"0824"},
        "رياضيات * علوم": {"0827"},
        "الرياضيات والإحصاء": {"0837", "0847"},
        "الفيزياء": {"0844"},
        "الكيمياء": {"0845"},
        "علوم الحياة": {"0846"},
    },
    "17": {
        "مركز اللغات الأجنبية": {"1700", "1701", "1722", "1723"},
    },
}

STATUS_MAP = {
    "متاحه": "available",
    "متاحة": "available",
    "ممتلئة": "full",
    "ممتلئه": "full",
    "غير متاحه": "unavailable",
    "غير متاحة": "unavailable",
}
DAY_MAP = {"ح": "sun", "ن": "mon", "ث": "tue", "ر": "wed", "خ": "thu"}
TIME_RE = re.compile(r"^(\d{2})(\d{2})\s*-\s*(\d{2})(\d{2})$")
COURSE_RE = re.compile(r"^([0-9]{4})-[0-9A-Za-z]+$")
CRN_RE = re.compile(r"^[0-9]{5,}$")


class PageError(Exception):
    """A page cannot safely replace its previous sections."""


def clean(value: str) -> str:
    return " ".join(value.split())


class BannerTables(HTMLParser):
    """Capture table cells in document order without depending on bs4/lxml."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[tuple[str, list[str]]] = []
        self._stack: list[dict] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            attributes = dict(attrs)
            self._stack.append({"class": attributes.get("class") or "", "cells": [], "cell": None})
        elif tag in {"td", "th"} and self._stack:
            self._stack[-1]["cell"] = []
        elif tag == "br" and self._stack and self._stack[-1]["cell"] is not None:
            self._stack[-1]["cell"].append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._stack:
            table = self._stack[-1]
            if table["cell"] is not None:
                table["cells"].append(clean("".join(table["cell"])))
                table["cell"] = None
        elif tag == "table" and self._stack:
            table = self._stack.pop()
            self.tables.append((table["class"], table["cells"]))

    def handle_data(self, data: str) -> None:
        if self._stack and self._stack[-1]["cell"] is not None:
            self._stack[-1]["cell"].append(data)


def validate_final_url(final_url: str, page: Page) -> None:
    final = urlsplit(final_url)
    if final.scheme != "https" or final.hostname != BANNER_HOST or final.path != BANNER_PATH:
        raise PageError(f"redirected outside Banner to {final_url}")
    actual = parse_qs(final.query, keep_blank_values=True)
    expected = {
        "p_trm_code": [TERM_CODE],
        "p_col_code": [page.college_param],
        "p_sex_code": [page.sex_param],
    }
    if any(actual.get(key) != value for key, value in expected.items()):
        raise PageError(f"unexpected Banner query in final URL {final_url}")


def fetch_page(page: Page) -> str:
    request = Request(
        page.url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; KFUCourseUpdater/1.0)"},
    )
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            with urlopen(request, timeout=35) as response:
                status = response.status
                final_url = response.geturl()
                content = response.read(MAX_RESPONSE_BYTES + 1)
            break
        except (HTTPError, URLError, TimeoutError, OSError, IncompleteRead) as error:
            if attempt == FETCH_ATTEMPTS:
                raise PageError(
                    f"request failed after {FETCH_ATTEMPTS} attempts: "
                    f"{type(error).__name__}: {error}"
                ) from error
            delay = RETRY_BACKOFF_SECONDS * attempt
            logging.warning(
                "%s: transient request/read error on attempt %d/%d (%s: %s); retrying in %ds",
                page.name, attempt, FETCH_ATTEMPTS, type(error).__name__, error, delay,
            )
            time.sleep(delay)
    if status != 200:
        raise PageError(f"HTTP {status}")
    validate_final_url(final_url, page)
    if len(content) > MAX_RESPONSE_BYTES:
        raise PageError("response exceeds 16 MiB")
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise PageError(f"response is not valid UTF-8: {error}") from error


def base_college_code(page: Page) -> str:
    return "17" if page.college_param == "00" else page.college_param


def department_code(page: Page, department_heading: str, course_prefix: str) -> str:
    base = base_college_code(page)
    if not re.fullmatch(r"[0-9]{4}", course_prefix) or not course_prefix.startswith(base):
        raise PageError(f"course prefix {course_prefix!r} does not belong to {page.name}")
    known_for_college = KNOWN_HEADERS[base]
    known_headings_for_prefix = {
        heading for heading, prefixes in known_for_college.items() if course_prefix in prefixes
    }
    if known_headings_for_prefix and department_heading not in known_headings_for_prefix:
        raise PageError(
            f"prefix {course_prefix} is under unexpected heading {department_heading!r}"
        )
    if not department_heading:
        raise PageError(f"empty department heading in {page.name}")
    if page.sex_param == "12":
        return f"{base}F{course_prefix[2:]}"
    return course_prefix


def parse_meeting(days_value: str, type_value: str, time_value: str) -> dict | None:
    days_raw = clean(days_value)
    activity = clean(type_value).replace("ا ختياري", "اختياري")
    time_raw = clean(time_value)
    if not days_raw and time_raw in {"", "-"}:
        return None
    if not days_raw or time_raw in {"", "-"}:
        raise PageError(f"incomplete meeting days={days_raw!r} time={time_raw!r}")
    day_codes = days_raw.split(" ")
    if any(day not in DAY_MAP for day in day_codes) or len(day_codes) != len(set(day_codes)):
        raise PageError(f"invalid meeting days {days_raw!r}")
    match = TIME_RE.fullmatch(time_raw)
    if not match:
        raise PageError(f"invalid meeting time {time_raw!r}")
    hour_start, minute_start, hour_end, minute_end = map(int, match.groups())
    start = hour_start * 60 + minute_start
    end = hour_end * 60 + minute_end
    if hour_start > 23 or hour_end > 23 or minute_start > 59 or minute_end > 59 or start >= end:
        raise PageError(f"invalid meeting time {time_raw!r}")
    return {
        "days": [DAY_MAP[day] for day in day_codes],
        "days_raw": " ".join(day_codes),
        "time": [start, end],
        "time_raw": f"{match[1]}{match[2]}-{match[3]}{match[4]}",
        "type": activity,
    }


def parse_banner(page: Page, html: str, existing_departments: dict) -> tuple[list[dict], dict]:
    # A generic 200 response, a homepage, or a wrong-term page must never
    # turn into a large deletion of valid sections.
    if "الجدول الدراسي" not in html or page.gender_word not in html or TERM_CODE[:4] not in html:
        raise PageError("Banner term/gender schedule heading is missing")
    parser = BannerTables()
    parser.feed(html)
    parser.close()
    current_college = None
    current_department = None
    group_has_columns = False
    group_rows = 0
    grouped: dict[str, dict] = {}
    department_additions: dict[str, dict] = {}
    headings_by_dept: dict[str, str] = {}
    data_rows = 0
    column_headers = 0
    for classes, cells in parser.tables:
        college_cells = [cell for cell in cells if re.match(r"^الكلية\s*:", cell)]
        department_cells = [cell for cell in cells if re.match(r"^القسم\s*:", cell)]
        if college_cells or department_cells:
            if current_department is not None and group_rows == 0:
                raise PageError(f"department {current_department!r} has no section rows")
            if len(college_cells) != 1 or len(department_cells) != 1:
                raise PageError("incomplete college/department heading")
            current_college = clean(college_cells[0].split(":", 1)[1])
            current_department = clean(department_cells[0].split(":", 1)[1])
            group_has_columns = False
            group_rows = 0
            if current_college != page.expected_college_header:
                raise PageError(f"unexpected college heading {current_college!r} in {page.name}")
            if not current_department:
                raise PageError("empty department heading")
            continue
        if "normaltxt" not in classes.split():
            continue
        if len(cells) >= 2 and cells[0] == "رقم المقرر" and cells[1].upper() == "CRN":
            if len(cells) != 13:
                raise PageError(f"course column heading has {len(cells)} cells")
            column_headers += 1
            group_has_columns = True
            continue
        if len(cells) != 13:
            raise PageError(f"section table has {len(cells)} cells, expected 13")
        if not current_college or not current_department or not group_has_columns:
            raise PageError("section row arrived without validated Banner headings")
        course, crn, section, raw_status, name, credits, days, activity, clock, instructor, prereq = cells[:11]
        match = COURSE_RE.fullmatch(course)
        if not match or not CRN_RE.fullmatch(crn) or not section or not name:
            raise PageError(f"invalid course/CRN/section/name in row {data_rows + 1}")
        status = STATUS_MAP.get(raw_status)
        if status is None:
            raise PageError(f"unknown status {raw_status!r} for CRN {crn}")
        prefix = match[1]
        dept = department_code(page, current_department, prefix)
        if dept in headings_by_dept and headings_by_dept[dept] != current_department:
            raise PageError(f"department {dept} appears under multiple Banner headings")
        headings_by_dept[dept] = current_department
        existing_definition = existing_departments.get(dept)
        if existing_definition and existing_definition.get("college") != page.college_code:
            raise PageError(f"department {dept} has conflicting college definition")
        if dept not in department_additions:
            department_additions[dept] = existing_definition or {
                "college": page.college_code,
                "name": f"{current_department} ({prefix})",
            }
        meeting = parse_meeting(days, activity, clock)
        activity = clean(activity).replace("ا ختياري", "اختياري")
        data_rows += 1
        group_rows += 1
        if crn not in grouped:
            grouped[crn] = {
                "course": course,
                "crn": crn,
                "section": section,
                "status": status,
                "name": name,
                "credits": credits,
                "type": activity,
                "instructor": "",
                "prereq": prereq,
                "college": page.college_code,
                "dept": dept,
                "meetings": [],
                "_instructors": set(),
                "_meeting_keys": set(),
            }
        section_obj = grouped[crn]
        for key, value in (
            ("course", course), ("section", section), ("status", status),
            ("name", name), ("credits", credits), ("prereq", prereq),
            ("college", page.college_code), ("dept", dept),
        ):
            if section_obj[key] != value:
                raise PageError(f"conflicting {key} for repeated CRN {crn}")
        if instructor:
            section_obj["_instructors"].add(instructor)
        if meeting:
            key = (tuple(meeting["days"]), tuple(meeting["time"]), meeting["type"])
            if key not in section_obj["_meeting_keys"]:
                section_obj["_meeting_keys"].add(key)
                section_obj["meetings"].append(meeting)
    if current_department is not None and group_rows == 0:
        raise PageError(f"department {current_department!r} has no section rows")
    if data_rows == 0 or column_headers == 0 or not grouped:
        raise PageError("no valid section tables in Banner response")
    result = []
    for section_obj in grouped.values():
        section_obj["instructor"] = " / ".join(sorted(section_obj.pop("_instructors")))
        section_obj.pop("_meeting_keys")
        section_obj["meetings"].sort(
            key=lambda meeting: (meeting["time"][0], meeting["time"][1], tuple(meeting["days"]), meeting["type"])
        )
        result.append(section_obj)
    logging.info("%s: %d rows, %d distinct CRNs", page.name, data_rows, len(result))
    return result, department_additions


def section_sort_key(section: dict) -> tuple:
    return (
        section["college"], section["dept"], section["course"],
        section["section"], section["crn"],
    )


def retain_previous_page(old_sections: list[dict], college_code: str) -> list[dict]:
    """Keep every old CRN on a failed page, merging legacy duplicate entries."""
    retained: dict[str, dict] = {}
    meeting_keys: dict[str, set[str]] = {}
    for previous in old_sections:
        if previous.get("college") != college_code:
            continue
        crn = str(previous["crn"])
        if crn not in retained:
            retained[crn] = copy.deepcopy(previous)
            retained[crn]["meetings"] = []
            meeting_keys[crn] = set()
        else:
            # This was the effective entry in the old UI's CRN lookup.
            for field, value in previous.items():
                if field != "meetings":
                    retained[crn][field] = copy.deepcopy(value)
        for meeting in previous.get("meetings", []):
            key = json.dumps(meeting, ensure_ascii=False, sort_keys=True)
            if key not in meeting_keys[crn]:
                meeting_keys[crn].add(key)
                retained[crn]["meetings"].append(copy.deepcopy(meeting))
    return list(retained.values())


def old_status_category(status: object) -> str:
    if status in (1, "available", "متاحه", "متاحة"):
        return "available"
    if status in (0, "full", "unavailable", "ممتلئة", "غير متاحه", "غير متاحة"):
        return "closed" if status == 0 else str(status)
    return str(status)


def meaningful_status_change(old: object, new: object) -> bool:
    """Count real open/closed changes; legacy 0 cannot distinguish full/unavailable."""
    prior = old_status_category(old)
    if prior == "closed":
        return new == "available"
    return prior != new


def read_data(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8").strip()
    match = re.fullmatch(r"const\s+KFU_DATA\s*=\s*(\{.*\})\s*;?", raw, re.DOTALL)
    if not match:
        raise ValueError(f"{path} is not in const KFU_DATA = {{...}}; form")
    data = json.loads(match[1])
    if not isinstance(data, dict) or not isinstance(data.get("sections"), list):
        raise ValueError("data.js has no sections array")
    if not isinstance(data.get("departments"), dict):
        raise ValueError("data.js has no departments object")
    return data


def write_data(path: Path, data: dict) -> None:
    body = "const KFU_DATA = " + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + ";\n"
    fd, temp_name = tempfile.mkstemp(prefix=".data-js-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temp_name, path.stat().st_mode & 0o777)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def term_label() -> str:
    label = {"10": "الفصل الأول", "20": "الفصل الثاني", "30": "الفصل الصيفي"}.get(
        TERM_CODE[-2:], f"الفصل {TERM_CODE[-2:]}"
    )
    return f"{label} {TERM_CODE[:4]}"


def update(repo_root: Path, dry_run: bool = False) -> int:
    path = repo_root / "data.js"
    current = read_data(path)
    old_sections = current["sections"]
    old_by_crn = {str(section["crn"]): section for section in old_sections}
    valid_pages: dict[str, list[dict]] = {}
    failed_pages: dict[str, str] = {}
    department_additions: dict[str, dict] = {}
    for index, page in enumerate(PAGES):
        if index:
            time.sleep(PAGE_DELAY_SECONDS)
        try:
            html = fetch_page(page)
            sections, new_departments = parse_banner(page, html, current["departments"])
            previous_count = len({section["crn"] for section in old_sections if section.get("college") == page.college_code})
            # A mostly empty but syntactically valid response is treated as a
            # failed page, so a truncated page cannot erase one small college.
            if previous_count and len(sections) * 100 < previous_count * (100 - MAX_DROP_PERCENT):
                raise PageError(
                    f"page CRNs dropped from {previous_count} to {len(sections)} "
                    f"(more than {MAX_DROP_PERCENT}%)"
                )
            valid_pages[page.name] = sections
            department_additions.update(new_departments)
        except PageError as error:
            failed_pages[page.name] = str(error)
            logging.error("%s: %s; retaining its previous sections", page.name, error)
        except Exception as error:
            # A parser defect or an unanticipated Banner change must be as
            # safe as an ordinary fetch failure for the affected page.
            failed_pages[page.name] = f"{type(error).__name__}: {error}"
            logging.exception("%s: unexpected page error; retaining its previous sections", page.name)
    if not valid_pages:
        logging.error("all six Banner pages failed; data.js left untouched")
        return 2

    next_sections: list[dict] = []
    for page in PAGES:
        if page.name in valid_pages:
            next_sections.extend(valid_pages[page.name])
        else:
            next_sections.extend(retain_previous_page(old_sections, page.college_code))
    next_sections.sort(key=section_sort_key)
    # CRNs should be unique across all six sources. A collision may mean
    # Banner changed its assignments or a page was mismatched; abort safely.
    if len({section["crn"] for section in next_sections}) != len(next_sections):
        logging.error("duplicate CRNs across pages or retained data; no write")
        return 3
    if len(next_sections) * 100 < len(old_sections) * (100 - MAX_DROP_PERCENT):
        logging.error(
            "ALERT: total sections dropped from %d to %d (> %d%%); no write or commit",
            len(old_sections), len(next_sections), MAX_DROP_PERCENT,
        )
        return 3

    next_data = copy.deepcopy(current)
    next_data["colleges"] = COLLEGES
    used_depts = {section["dept"] for section in next_sections}
    available_depts = {**current["departments"], **department_additions}
    missing_depts = used_depts - available_depts.keys()
    if missing_depts:
        logging.error("missing department definitions %s; no write", sorted(missing_depts))
        return 3
    next_data["departments"] = {
        code: available_depts[code] for code in sorted(used_depts)
    }
    next_data["sections"] = next_sections
    next_data["meta"].update(
        term=term_label(),
        campus="الأحساء",
        gender="طلاب وطالبات",
        note="يشمل طلاب وطالبات - الهندسة والعلوم ومركز اللغات",
    )

    next_by_crn = {str(section["crn"]): section for section in next_sections}
    added = len(next_by_crn.keys() - old_by_crn.keys())
    removed = len(old_by_crn.keys() - next_by_crn.keys())
    status_changed = sum(
        meaningful_status_change(old_by_crn[crn].get("status"), next_by_crn[crn].get("status"))
        for crn in old_by_crn.keys() & next_by_crn.keys()
    )
    compare_current = copy.deepcopy(current)
    compare_next = copy.deepcopy(next_data)
    for item in (compare_current, compare_next):
        item.setdefault("meta", {}).pop("status_updated", None)
        item["meta"].pop("updated", None)
    changed = compare_current != compare_next
    logging.info(
        "summary: previous=%d current=%d added=%d removed=%d status_changed=%d valid_pages=%d failed_pages=%d changed=%s",
        len(old_sections), len(next_sections), added, removed, status_changed,
        len(valid_pages), len(failed_pages), changed,
    )
    if changed:
        now = datetime.now(RIYADH_TZ)
        next_data["meta"]["status_updated"] = now.isoformat(timespec="seconds")
        next_data["meta"]["updated"] = now.date().isoformat()
        if dry_run:
            logging.info("dry run: data.js not written")
        else:
            write_data(path, next_data)
            logging.info("wrote %s", path)
    if failed_pages:
        logging.warning("partial Banner refresh; failed pages: %s", ", ".join(failed_pages))
        return 2
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--once", action="store_true", help="run one update (default behavior)")
    parser.add_argument("--dry-run", action="store_true", help="fetch and report without writing data.js")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        return update(args.repo_root.resolve(), dry_run=args.dry_run)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        logging.error("fatal data or filesystem error: %s", error)
        return 3


if __name__ == "__main__":
    sys.exit(main())
