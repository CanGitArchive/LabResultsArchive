import csv
import html
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PyQt6.QtCore import Qt, QMarginsF, QRectF
from PyQt6.QtGui import QIcon, QTextDocument, QPageLayout, QPageSize, QPainter, QFont, QColor, QPen
from PyQt6.QtPrintSupport import QPrinter
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QInputDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
    QAbstractItemView,
    QComboBox,
)

import webbrowser
if sys.platform.startswith("win"):
    import winreg

APP_TITLE = "Lab Results Archive"
APP_VERSION = "0.1.0"
APP_ICON_FILE = "icon.ico"
DATA_DIR_NAME = "DATA"
DEFAULT_ARCHIVE_DIR_NAME = "LabResultsArchive"
DEFAULT_RESULTS_FILE = "blood_test_results.json"
DEFAULT_RANGES_FILE = "test_ranges.json"
NO_UNIT_PLACEHOLDER = "-"

KNOWN_MOJIBAKE_REPLACEMENTS = {
    "ﾂｵ": "µ",
    "ﾂｲ": "²",
    "竕･": "≥",
    "窶・": NO_UNIT_PLACEHOLDER,
}


# -----------------------------
# Portable path helpers
# -----------------------------
def get_app_dir() -> Path:
    """
    Portable-data rule:
    - running as .py: data lives next to this .py
    - running as .exe: data lives next to the .exe
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def get_resource_path(filename: str) -> Path:
    """
    Works as .py and as PyInstaller .exe.
    First checks beside the .py/.exe. Then checks PyInstaller's temp bundle path.
    """
    app_side = get_app_dir() / filename
    if app_side.exists():
        return app_side

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bundled = Path(meipass) / filename
        if bundled.exists():
            return bundled

    return app_side


def apply_app_icon(app_or_window) -> None:
    icon_path = get_resource_path(APP_ICON_FILE)
    if icon_path.exists():
        app_or_window.setWindowIcon(QIcon(str(icon_path)))


def open_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def validate_json_file(path: Path) -> None:
    with path.open("r", encoding="utf-8") as f:
        json.load(f)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    validate_json_file(path)


def validate_bundle_json_files(bundle_dir: Path, filenames: List[str]) -> List[Path]:
    json_paths = [bundle_dir / name for name in filenames if name.lower().endswith(".json")]
    for json_path in json_paths:
        validate_json_file(json_path)
    return json_paths


def stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def repair_known_mojibake(text: str) -> str:
    repaired = text
    for broken, replacement in KNOWN_MOJIBAKE_REPLACEMENTS.items():
        repaired = repaired.replace(broken, replacement)
    return repaired


def clean_json_export_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): clean_json_export_value(val) for key, val in value.items()}
    if isinstance(value, list):
        return [clean_json_export_value(item) for item in value]
    if isinstance(value, tuple):
        return [clean_json_export_value(item) for item in value]
    if isinstance(value, str):
        return repair_known_mojibake(value)
    return value


def safe_slug(text: str, fallback: str = "record") -> str:
    s = text.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or fallback


def normalize_unit(unit: Any) -> str:
    text = repair_known_mojibake(stringify(unit))
    return text if text else NO_UNIT_PLACEHOLDER


def normalize_record_for_ai_export(record: Dict[str, Any]) -> Dict[str, Any]:
    exported = clean_json_export_value(dict(record))
    exported["date"] = stringify(exported.get("date"))
    exported["test_name_en"] = stringify(exported.get("test_name_en"))
    exported["value"] = stringify(exported.get("value"))
    exported["unit"] = normalize_unit(exported.get("unit"))
    exported["source_files"] = parse_source_files(exported.get("source_files"))
    return exported


def parse_source_files(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [stringify(v) for v in value if stringify(v)]
    text = stringify(value)
    if not text:
        return []
    parts = re.split(r"[,;\n]+", text)
    return [p.strip() for p in parts if p.strip()]


def source_files_to_text(files: Any) -> str:
    return ", ".join(parse_source_files(files))


def sort_key_for_date(date_text: str) -> Tuple[int, str]:
    try:
        dt = datetime.strptime(date_text, "%Y-%m-%d")
        return (0, dt.strftime("%Y%m%d"))
    except Exception:
        return (1, date_text)


def is_valid_date(date_text: str) -> bool:
    try:
        datetime.strptime(date_text, "%Y-%m-%d")
        return True
    except Exception:
        return False


def cell_text(value: Any, unit: Any, include_unit: bool = True) -> str:
    v = stringify(value)
    u = normalize_unit(unit)
    if not v:
        return ""
    if include_unit and u and u != "-":
        return f"{v} {u}"
    return v


def split_value_unit(text: str, fallback_unit: str = "-") -> Tuple[str, str]:
    """
    For importing wide CSV cells like:
      501 pg/mL
      5.59 10^3/µL
      Negative
    If a fallback unit exists, keep it and treat the whole cell as value unless the cell ends with that unit.
    """
    t = stringify(text)
    fu = normalize_unit(fallback_unit)
    if not t:
        return "", fu

    if fu != "-" and t.endswith(fu):
        return t[: -len(fu)].strip(), fu

    # Simple number + trailing unit fallback.
    m = re.match(r"^([+-]?\d+(?:[.,]\d+)?(?:-\d+(?:[.,]\d+)?)?)\s+(.+)$", t)
    if m and fu == "-":
        return m.group(1).strip(), m.group(2).strip()

    return t, fu


def markdown_escape(text: Any) -> str:
    return stringify(text).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def format_range_number(value: Any) -> str:
    text = stringify(value)
    if not text:
        return ""
    try:
        number = float(text)
        if number.is_integer():
            return str(int(number))
        return f"{number:g}"
    except Exception:
        return text


def format_reference_range(range_entry: Optional[Dict[str, Any]]) -> str:
    """
    Human-readable display text for DATA/LabResultsArchive/test_ranges.json.
    """
    if not isinstance(range_entry, dict):
        return "-"

    range_type = stringify(range_entry.get("range_type"))
    unit = normalize_unit(range_entry.get("unit"))
    unit_text = "" if unit == "-" else f" {unit}"

    low = range_entry.get("low")
    high = range_entry.get("high")

    if range_type == "interval":
        return f"{format_range_number(low)}-{format_range_number(high)}{unit_text}"
    if range_type == "upper_limit":
        return f"< {format_range_number(high)}{unit_text}"
    if range_type == "lower_limit":
        return f"≥ {format_range_number(low)}{unit_text}"
    if range_type == "expected_values":
        values = range_entry.get("expected_values", [])
        if isinstance(values, list) and values:
            return " / ".join(stringify(v) for v in values if stringify(v))
        return "Expected value"
    if range_type == "no_universal_range":
        return "-"

    return "-"


def parse_numeric_for_coloring(value: Any) -> Optional[float]:
    """
    Strict-ish numeric parser for range coloring.

    Returns None for qualitative/free-text values such as Negative, Normal,
    Not reported, 1-2 leukocytes, etc. Those are handled separately only when
    range_type is expected_values.
    """
    text = stringify(value)
    if not text:
        return None

    lowered = text.lower()
    if lowered in {"-", "-", "not reported", "negative", "normal", "none"}:
        return None

    # Remove simple thousands separators, but keep decimal comma support.
    text = text.replace(",", ".").strip()

    # Match a single numeric token only. Avoid guessing for ranges like "1-2".
    m = re.fullmatch(r"[+-]?\d+(?:\.\d+)?", text)
    if not m:
        return None

    try:
        return float(text)
    except Exception:
        return None


def range_status_for_value(value: Any, range_entry: Optional[Dict[str, Any]]) -> Optional[str]:
    """
    Return:
      "low"          -> value is below a lower threshold
      "high"         -> value is above an upper threshold
      "unexpected"   -> qualitative value does not match expected values
      None           -> in range, no range, non-coloring, or not comparable

    The app only colors entries where test_ranges.json has use_for_coloring=true.
    """
    if not isinstance(range_entry, dict):
        return None

    if not bool(range_entry.get("use_for_coloring", False)):
        return None

    range_type = stringify(range_entry.get("range_type"))
    raw_value = stringify(value)

    if not raw_value or raw_value in {"-", "-"}:
        return None

    if range_type == "expected_values":
        expected = range_entry.get("expected_values", [])
        if not isinstance(expected, list) or not expected:
            return None
        expected_norm = {stringify(v).lower() for v in expected if stringify(v)}
        return None if raw_value.lower() in expected_norm else "unexpected"

    number = parse_numeric_for_coloring(raw_value)
    if number is None:
        return None

    low_raw = range_entry.get("low")
    high_raw = range_entry.get("high")
    low = parse_numeric_for_coloring(low_raw)
    high = parse_numeric_for_coloring(high_raw)

    if range_type == "interval":
        if low is not None and number < low:
            return "low"
        if high is not None and number > high:
            return "high"
        return None

    if range_type == "upper_limit":
        if high is not None and number > high:
            return "high"
        return None

    if range_type == "lower_limit":
        if low is not None and number < low:
            return "low"
        return None

    return None


def color_for_range_status(status: Optional[str]) -> Optional[QColor]:
    """
    App/UI coloring.

    Keep these dark-theme friendly. The PDF renderer uses separate light colors,
    because white paper/PDF needs different contrast than the dark app table.
    """
    if status == "low":
        return QColor("#6b5818")   # muted dark amber
    if status in {"high", "unexpected"}:
        return QColor("#6b2d2d")   # muted dark red
    return None


def combined_status_for_records(records: List[Dict[str, Any]], range_entry: Optional[Dict[str, Any]]) -> Optional[str]:
    """
    If a cell contains multiple results, high/unexpected has priority over low.
    """
    statuses = [range_status_for_value(r.get("value"), range_entry) for r in records]
    if any(s in {"high", "unexpected"} for s in statuses):
        return "high"
    if any(s == "low" for s in statuses):
        return "low"
    return None


def load_simple_env(path: Path) -> Dict[str, str]:
    """
    Tiny .env parser so the app does not need python-dotenv.

    Supported:
      KEY=value
      KEY="value"
      KEY='value'

    Lines starting with # are ignored.
    """
    env: Dict[str, str] = {}
    if not path.exists():
        return env

    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        env[key] = value
    return env


# -----------------------------
# Grouping / report helpers
# -----------------------------
def test_category(test_name: str) -> str:
    n = test_name.lower()

    hemogram = [
        "white blood cell", "red blood cell", "hemoglobin", "hematocrit",
        "corpuscular", "red cell distribution", "nucleated red",
        "wbc", "rbc", "hgb", "hct", "mcv", "mch", "mchc", "rdw"
    ]
    differential = [
        "neutrophil", "lymphocyte", "monocyte", "eosinophil", "basophil",
        "granulocyte", "mid cells"
    ]
    platelets = ["platelet", "plateletcrit", "mean platelet", "pdw", "p-lcr"]
    kidney = ["creatinine", "glomerular", "egfr", "urea", "blood urea", "bun", "uric acid"]
    liver = ["alanine", "aspartate", "gamma-glutamyl", "ggt", "bilirubin", "alt", "ast", "sgot", "sgpt"]
    lipids = ["cholesterol", "triglyceride", "hdl", "ldl", "vldl", "non-hdl"]
    iron = ["iron", "ferritin", "tibc", "uibc", "binding capacity"]
    glucose = ["glucose", "insulin", "homa", "a1c", "hemoglobin a1c"]
    vitamins_minerals = [
        "vitamin", "folate", "folic", "magnesium", "calcium", "sodium",
        "potassium", "zinc", "iodine"
    ]
    thyroid = ["thyroid", "tsh", "thyroxine", "triiodothyronine", "free t3", "free t4"]
    inflammation = ["c-reactive", "crp", "sedimentation", "esr", "creatine kinase", "ck"]
    urine = ["urine"]
    pancreas = ["lipase", "amylase"]

    if any(x in n for x in hemogram):
        return "Hemogram / Red Cells"
    if any(x in n for x in differential):
        return "White Cell Differential"
    if any(x in n for x in platelets):
        return "Platelets"
    if any(x in n for x in kidney):
        return "Kidney"
    if any(x in n for x in liver):
        return "Liver"
    if any(x in n for x in lipids):
        return "Lipids"
    if any(x in n for x in iron):
        return "Iron"
    if any(x in n for x in glucose):
        return "Glucose / Insulin"
    if any(x in n for x in thyroid):
        return "Thyroid / Hormones"
    if any(x in n for x in vitamins_minerals):
        return "Vitamins / Minerals"
    if any(x in n for x in inflammation):
        return "Inflammation / Muscle"
    if any(x in n for x in pancreas):
        return "Pancreatic Enzymes"
    if any(x in n for x in urine):
        return "Urine"
    return "Other"


CATEGORY_ORDER = {
    "Hemogram / Red Cells": 100,
    "White Cell Differential": 120,
    "Platelets": 140,
    "Kidney": 200,
    "Liver": 220,
    "Lipids": 240,
    "Iron": 260,
    "Glucose / Insulin": 280,
    "Thyroid / Hormones": 300,
    "Vitamins / Minerals": 320,
    "Inflammation / Muscle": 340,
    "Pancreatic Enzymes": 360,
    "Urine": 800,
    "Other": 900,
}


# -----------------------------
# Data store
# -----------------------------
class BloodTestStore:
    def __init__(self, app_dir: Path):
        self.app_dir = app_dir
        self.data_dir = self.app_dir / DATA_DIR_NAME
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.env_path = self.data_dir / ".env"
        env = load_simple_env(self.env_path)

        archive_dir_name = env.get("BLOOD_TEST_ARCHIVE_DIR", DEFAULT_ARCHIVE_DIR_NAME).strip() or DEFAULT_ARCHIVE_DIR_NAME
        results_file_name = env.get("BLOOD_TEST_RESULTS_FILE", DEFAULT_RESULTS_FILE).strip() or DEFAULT_RESULTS_FILE
        ranges_file_name = env.get("BLOOD_TEST_RANGES_FILE", DEFAULT_RANGES_FILE).strip() or DEFAULT_RANGES_FILE

        self.archive_dir = self.data_dir / archive_dir_name
        self.archive_dir.mkdir(parents=True, exist_ok=True)

        self.results_path = self.archive_dir / results_file_name
        self.ranges_path = self.archive_dir / ranges_file_name
        self.backup_dir = self.archive_dir / "backups"
        self.exports_dir = self.archive_dir / "exports"
        self.original_pdfs_dir = self.archive_dir / "original_pdfs"

        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.exports_dir.mkdir(parents=True, exist_ok=True)
        self.original_pdfs_dir.mkdir(parents=True, exist_ok=True)

        self.data: Dict[str, Any] = {}
        self.records: List[Dict[str, Any]] = []
        self.ranges_data: Dict[str, Any] = {}
        self.ranges_by_test: Dict[str, Dict[str, Any]] = {}
        self.load_or_create()
        self.load_ranges()

    def load_ranges(self) -> None:
        """
        Load DATA/LabResultsArchive/test_ranges.json.

        Missing/invalid range file should never break the app. It only means the
        Reference / Target Range column will show "-".
        """
        self.ranges_data = {}
        self.ranges_by_test = {}

        if not self.ranges_path.exists():
            return

        try:
            loaded = read_json(self.ranges_path)
            if not isinstance(loaded, dict):
                raise ValueError("Top-level test_ranges.json must be an object.")
            ranges = loaded.get("ranges", [])
            if not isinstance(ranges, list):
                raise ValueError("'ranges' must be a list.")

            self.ranges_data = loaded
            for item in ranges:
                if not isinstance(item, dict):
                    continue
                test_name = stringify(item.get("test_name_en"))
                if test_name:
                    self.ranges_by_test[test_name] = item
        except Exception as exc:
            QMessageBox.warning(
                None,
                APP_TITLE,
                f"Could not read test_ranges.json.\\n\\n"
                f"Reference / Target Range column will be blank.\\n\\n"
                f"File:\\n{self.ranges_path}\\n\\n"
                f"Error:\\n{exc}"
            )
            self.ranges_data = {}
            self.ranges_by_test = {}

    def range_for_test(self, test_name: str) -> Optional[Dict[str, Any]]:
        return self.ranges_by_test.get(stringify(test_name))

    def range_text_for_test(self, test_name: str) -> str:
        return format_reference_range(self.range_for_test(test_name))

    def default_data(self) -> Dict[str, Any]:
        return {
            "schema_version": 1,
            "app": {
                "name": APP_TITLE,
                "version_created": APP_VERSION,
                "storage_model": "split_json_v1",
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "last_saved_at": "",
                "notes": "Blood test records archive. Dates are sample/test dates where known.",
            },
            "records": [],
        }

    def load_or_create(self) -> None:
        if not self.results_path.exists():
            self.data = self.default_data()
            self.records = []
            self.save(make_backup=False)
            return

        try:
            loaded = read_json(self.results_path)
            if not isinstance(loaded, dict):
                raise ValueError("Top-level JSON must be an object.")
            records = loaded.get("records", [])
            if not isinstance(records, list):
                raise ValueError("'records' must be a list.")

            self.data = loaded
            self.records = self.normalize_records(records)
            self.data["records"] = self.records
        except Exception as exc:
            broken_backup = self.backup_file(self.results_path, suffix="broken")
            self.data = self.default_data()
            self.records = []
            self.save(make_backup=False)
            QMessageBox.warning(
                None,
                APP_TITLE,
                f"Could not read blood_test_results.json.\n\n"
                f"Error: {exc}\n\n"
                f"A backup of the broken file was made here:\n{broken_backup}\n\n"
                f"A fresh empty archive file was created."
            )

    def normalize_records(self, records: List[Any]) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []

        for raw in records:
            if not isinstance(raw, dict):
                continue

            date_text = stringify(raw.get("date"))
            test_name = stringify(raw.get("test_name_en") or raw.get("test_name") or raw.get("name"))
            value = stringify(raw.get("value") or raw.get("result"))
            unit = normalize_unit(raw.get("unit"))
            source_files = parse_source_files(raw.get("source_files", []))

            if not date_text and not test_name and not value:
                continue

            record = {
                "record_id": stringify(raw.get("record_id")),
                "date": date_text,
                "test_name_en": test_name,
                "value": value,
                "unit": unit,
                "source_files": source_files,
            }

            # Preserve any extra fields the user/app may add later.
            for key, val in raw.items():
                if key not in record:
                    record[key] = val

            normalized.append(record)

        self.assign_missing_record_ids(normalized)
        return sorted(
            normalized,
            key=lambda r: (
                sort_key_for_date(r.get("date", "")),
                CATEGORY_ORDER.get(test_category(r.get("test_name_en", "")), 999),
                r.get("test_name_en", "").lower(),
                r.get("record_id", ""),
            ),
        )

    def assign_missing_record_ids(self, records: List[Dict[str, Any]]) -> None:
        seen: Dict[str, int] = {}

        for record in records:
            current = stringify(record.get("record_id"))
            if current:
                base = current
            else:
                base = f"{record.get('date', 'unknown_date')}__{safe_slug(record.get('test_name_en', ''), 'test')}"
                if not record.get("date"):
                    base = f"unknown_date__{safe_slug(record.get('test_name_en', ''), 'test')}"

            if base in seen:
                seen[base] += 1
                record["record_id"] = f"{base}_{seen[base]}"
            else:
                seen[base] = 1
                record["record_id"] = base

    def backup_file(self, path: Path, suffix: str = "backup") -> Optional[Path]:
        if not path.exists():
            return None
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = self.backup_dir / f"{path.stem}.{suffix}_{stamp}{path.suffix}"
        shutil.copy2(path, backup)
        return backup

    def save(self, make_backup: bool = True) -> None:
        self.records = self.normalize_records(self.records)
        self.data.setdefault("schema_version", 1)
        self.data.setdefault("app", {})
        self.data["app"]["name"] = APP_TITLE
        self.data["app"]["storage_model"] = "split_json_v1"
        self.data["app"]["last_saved_at"] = datetime.now().isoformat(timespec="seconds")
        self.data["records"] = self.records

        if make_backup and self.results_path.exists():
            self.backup_file(self.results_path)

        write_json(self.results_path, self.data)

    def replace_records(self, records: List[Dict[str, Any]]) -> None:
        self.records = self.normalize_records(records)

    def add_records(self, records: List[Dict[str, Any]]) -> None:
        self.records.extend(records)
        self.records = self.normalize_records(self.records)

    def unique_dates(self, records: Optional[List[Dict[str, Any]]] = None) -> List[str]:
        source = records if records is not None else self.records
        return sorted({stringify(r.get("date")) for r in source if stringify(r.get("date"))}, key=sort_key_for_date)

    def unique_test_names(self, records: Optional[List[Dict[str, Any]]] = None) -> List[str]:
        source = records if records is not None else self.records
        names = {stringify(r.get("test_name_en")) for r in source if stringify(r.get("test_name_en"))}
        return sorted(
            names,
            key=lambda s: (CATEGORY_ORDER.get(test_category(s), 999), s.lower())
        )


# -----------------------------
# Import helpers
# -----------------------------
def normalize_imported_record(raw: Dict[str, Any], default_source: str = "") -> Dict[str, Any]:
    return {
        "record_id": stringify(raw.get("record_id")),
        "date": stringify(raw.get("date") or raw.get("Date")),
        "test_name_en": stringify(
            raw.get("test_name_en")
            or raw.get("English test name")
            or raw.get("Test name")
            or raw.get("test_name")
            or raw.get("name")
        ),
        "value": stringify(raw.get("value") or raw.get("Value") or raw.get("result") or raw.get("Result")),
        "unit": normalize_unit(raw.get("unit") or raw.get("Unit")),
        "source_files": parse_source_files(raw.get("source_files") or raw.get("Source files") or default_source),
    }


def records_from_json(path: Path) -> List[Dict[str, Any]]:
    raw = read_json(path)
    if isinstance(raw, dict):
        records = raw.get("records", [])
    elif isinstance(raw, list):
        records = raw
    else:
        raise ValueError("JSON import must be a list of records or an object with a 'records' list.")

    if not isinstance(records, list):
        raise ValueError("'records' must be a list.")

    return [normalize_imported_record(r, default_source=path.name) for r in records if isinstance(r, dict)]


def records_from_csv(path: Path) -> List[Dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        dialect = csv.Sniffer().sniff(sample) if sample.strip() else csv.excel
        reader = csv.DictReader(f, dialect=dialect)
        rows = list(reader)

    if not rows:
        return []

    headers = [h or "" for h in (reader.fieldnames or [])]
    header_lower = {h.lower().strip(): h for h in headers}

    # Raw records CSV.
    if any(k in header_lower for k in ["date", "test_name_en", "english test name", "value", "unit"]):
        records = []
        for row in rows:
            records.append(normalize_imported_record(row, default_source=path.name))
        return records

    # Wide history CSV exported by this app:
    # Test name, Units, 2023-07-25, 2023-12-14, ...
    test_header = None
    for candidate in ["test name", "english test name", "test"]:
        if candidate in header_lower:
            test_header = header_lower[candidate]
            break
    unit_header = None
    for candidate in ["units", "unit"]:
        if candidate in header_lower:
            unit_header = header_lower[candidate]
            break

    date_headers = [h for h in headers if is_valid_date(h.strip())]
    if test_header and date_headers:
        records = []
        for row in rows:
            test_name = stringify(row.get(test_header))
            if not test_name:
                continue
            unit_text = normalize_unit(row.get(unit_header, "-")) if unit_header else "-"
            # If unit cell contains multiple units, keep it as a display unit unless the value cell carries its own.
            for date_h in date_headers:
                raw_cell = stringify(row.get(date_h))
                if not raw_cell:
                    continue

                # Support multiple same-date values separated by semicolon.
                parts = [p.strip() for p in raw_cell.split(";") if p.strip()]
                for part in parts:
                    value, unit = split_value_unit(part, unit_text)
                    records.append({
                        "record_id": "",
                        "date": date_h.strip(),
                        "test_name_en": test_name,
                        "value": value,
                        "unit": unit,
                        "source_files": [path.name],
                    })
        return records

    raise ValueError(
        "CSV format not recognized. Supported formats: raw records CSV with date/test/value/unit columns, "
        "or wide history CSV with Test name/Units/date columns."
    )


# -----------------------------
# Export / report helpers
# -----------------------------
def build_wide_history(records: List[Dict[str, Any]]) -> Tuple[List[str], List[str], Dict[str, Dict[str, List[Dict[str, Any]]]], Dict[str, List[str]]]:
    dates = sorted({stringify(r.get("date")) for r in records if stringify(r.get("date"))}, key=sort_key_for_date)
    test_names = sorted(
        {stringify(r.get("test_name_en")) for r in records if stringify(r.get("test_name_en"))},
        key=lambda s: (CATEGORY_ORDER.get(test_category(s), 999), s.lower())
    )

    lookup: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    units_by_test: Dict[str, List[str]] = {}

    for record in records:
        name = stringify(record.get("test_name_en"))
        date_text = stringify(record.get("date"))
        if not name or not date_text:
            continue

        lookup.setdefault(name, {}).setdefault(date_text, []).append(record)
        unit = normalize_unit(record.get("unit"))
        units_by_test.setdefault(name, [])
        if unit not in units_by_test[name]:
            units_by_test[name].append(unit)

    return dates, test_names, lookup, units_by_test


def filter_records_by_dates(records: List[Dict[str, Any]], dates: List[str]) -> List[Dict[str, Any]]:
    allowed = set(dates)
    return [r for r in records if stringify(r.get("date")) in allowed]


def filter_records_by_date_range(records: List[Dict[str, Any]], start_date: str, end_date: str) -> List[Dict[str, Any]]:
    return [
        r for r in records
        if stringify(r.get("date")) and start_date <= stringify(r.get("date")) <= end_date
    ]


def last_n_dates(records: List[Dict[str, Any]], n: int) -> List[str]:
    dates = sorted({stringify(r.get("date")) for r in records if stringify(r.get("date"))}, key=sort_key_for_date)
    return dates[-n:] if n > 0 else dates


def chunk_dates(dates: List[str], chunk_size: int) -> List[List[str]]:
    chunk_size = max(1, int(chunk_size))
    return [dates[i:i + chunk_size] for i in range(0, len(dates), chunk_size)]


def display_cell_for_report(recs: List[Dict[str, Any]], all_units_for_test: List[str]) -> str:
    if not recs:
        return "-"

    # If there is only one unit used for this test, keep cells clean and show only value.
    include_unit = len([u for u in all_units_for_test if u and u != "-"]) > 1

    values = []
    for r in recs:
        values.append(cell_text(r.get("value"), r.get("unit"), include_unit=include_unit))
    return "; ".join(values)


def export_doctor_csv(path: Path, records: List[Dict[str, Any]], ranges_by_test: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
    dates, test_names, lookup, units_by_test = build_wide_history(records)
    ranges_by_test = ranges_by_test or {}

    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Category", "Test name"] + dates + ["Units", "Reference / Target Range"])
        for name in test_names:
            category = test_category(name)
            units = " / ".join(units_by_test.get(name, []))
            range_text = format_reference_range(ranges_by_test.get(name))
            row = [category, name]
            for date_text in dates:
                row.append(display_cell_for_report(lookup.get(name, {}).get(date_text, []), units_by_test.get(name, [])))
            row.append(units)
            row.append(range_text)
            writer.writerow(row)


def build_doctor_markdown(records: List[Dict[str, Any]], source_path: Path, ranges_by_test: Optional[Dict[str, Dict[str, Any]]] = None) -> str:
    dates, test_names, lookup, units_by_test = build_wide_history(records)
    ranges_by_test = ranges_by_test or {}
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    lines: List[str] = []
    lines.append("# Blood Test History Report\n\n")
    lines.append(f"- **Generated:** {now}\n")
    lines.append(f"- **Source JSON:** `{source_path}`\n")
    lines.append(f"- **Records:** {len(records)}\n")
    lines.append(f"- **Dates:** {', '.join(dates)}\n\n")
    lines.append("Note: Units are shown in the Units column. If a test has multiple units across dates, the value cells include their units.\n\n")

    current_category = ""
    for name in test_names:
        category = test_category(name)
        if category != current_category:
            current_category = category
            lines.append(f"\n## {markdown_escape(category)}\n\n")
            lines.append("| Test name | " + " | ".join(markdown_escape(d) for d in dates) + " | Units | Reference / Target Range |\n")
            lines.append("| --- | " + " | ".join("---:" for _ in dates) + " | --- | --- |\n")

        units = " / ".join(units_by_test.get(name, []))
        row = [markdown_escape(name)]
        for date_text in dates:
            row.append(markdown_escape(display_cell_for_report(lookup.get(name, {}).get(date_text, []), units_by_test.get(name, []))))
        row.append(markdown_escape(units))
        row.append(markdown_escape(format_reference_range(ranges_by_test.get(name))))
        lines.append("| " + " | ".join(row) + " |\n")

    return "".join(lines)


def build_doctor_html(records: List[Dict[str, Any]], source_path: Path, ranges_by_test: Optional[Dict[str, Dict[str, Any]]] = None) -> str:
    dates, test_names, lookup, units_by_test = build_wide_history(records)
    ranges_by_test = ranges_by_test or {}
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    date_count = max(len(dates), 1)

    # V0.3.4:
    # - One global table remains, so every category shares the same grid.
    # - Units moved to the rightmost column.
    # - Every cell is centered both horizontally and vertically.
    # - Every date column receives the exact same width.
    #
    # QTextDocument's HTML renderer is not a full browser engine, so we repeat
    # width attributes on <col>, <th>, and <td> for stronger consistency.
    test_col_width = 22.0
    unit_col_width = 10.0
    range_col_width = 16.0
    date_col_width = max((100.0 - test_col_width - unit_col_width - range_col_width) / date_count, 5.0)

    style = f"""
    body {{
        font-family: Arial, Helvetica, sans-serif;
        font-size: 8.3pt;
        color: #222;
    }}
    h1 {{
        font-size: 18pt;
        margin-bottom: 4px;
    }}
    .meta {{
        font-size: 8pt;
        color: #555;
        margin-bottom: 8px;
    }}
    table.report {{
        border-collapse: collapse;
        width: 100%;
        table-layout: fixed;
    }}
    table.report th,
    table.report td {{
        border: 1px solid #bbbbbb;
        padding: 3px;
        text-align: center;
        vertical-align: middle;
        overflow-wrap: anywhere;
        word-wrap: break-word;
    }}
    table.report th {{
        background: #f2f2f2;
        font-weight: bold;
    }}
    tr.category-row td {{
        background: #e8e8e8;
        font-size: 11pt;
        font-weight: bold;
        border: 1px solid #d0d0d0;
        padding: 5px;
        text-align: left;
        vertical-align: middle;
    }}
    tr.repeat-header th {{
        background: #f2f2f2;
        font-weight: bold;
        text-align: center;
        vertical-align: middle;
    }}
    td.test {{
        text-align: center;
        vertical-align: middle;
        width: {test_col_width:.2f}%;
    }}
    td.units {{
        width: {unit_col_width:.2f}%;
        color: #444;
        text-align: center;
        vertical-align: middle;
    }}
    td.range {{
        width: {range_col_width:.2f}%;
        color: #333;
        text-align: center;
        vertical-align: middle;
    }}
    th.date,
    td.value {{
        width: {date_col_width:.2f}%;
        text-align: center;
        vertical-align: middle;
        white-space: normal;
    }}
    .small-note {{
        font-size: 7.5pt;
        color: #666;
    }}
    """

    def colgroup_html() -> str:
        parts = [f"<col width='{test_col_width:.2f}%'>"]
        for _ in dates:
            parts.append(f"<col width='{date_col_width:.2f}%'>")
        parts.append(f"<col width='{unit_col_width:.2f}%'>")
        parts.append(f"<col width='{range_col_width:.2f}%'>")
        return "<colgroup>" + "".join(parts) + "</colgroup>"

    def header_row_html() -> str:
        row = [
            f"<tr class='repeat-header'>"
            f"<th width='{test_col_width:.2f}%'>Test name</th>"
        ]
        for d in dates:
            row.append(f"<th class='date' width='{date_col_width:.2f}%'>{html.escape(d)}</th>")
        row.append(f"<th width='{unit_col_width:.2f}%'>Units</th>")
        row.append(f"<th width='{range_col_width:.2f}%'>Reference / Target Range</th>")
        row.append("</tr>")
        return "".join(row)

    parts: List[str] = []
    parts.append("<html><head><meta charset='utf-8'><style>")
    parts.append(style)
    parts.append("</style></head><body>")
    parts.append("<h1>Blood Test History Report</h1>")
    parts.append("<div class='meta'>")
    parts.append(f"<b>Generated:</b> {html.escape(now)}<br>")
    parts.append(f"<b>Source JSON:</b> {html.escape(str(source_path))}<br>")
    parts.append(f"<b>Records:</b> {len(records)}<br>")
    parts.append(f"<b>Dates:</b> {html.escape(', '.join(dates))}<br>")
    parts.append("<span class='small-note'>Units are shown in the rightmost Units column. Missing results are shown as '-'. If a test has multiple units across dates, the value cells include their units.</span>")
    parts.append("</div>")

    by_category: Dict[str, List[str]] = defaultdict(list)
    for name in test_names:
        by_category[test_category(name)].append(name)

    total_columns = 3 + len(dates)
    parts.append("<table class='report' width='100%' cellspacing='0' cellpadding='0'>")
    parts.append(colgroup_html())

    for category in sorted(by_category.keys(), key=lambda c: CATEGORY_ORDER.get(c, 999)):
        parts.append(
            f"<tr class='category-row'><td colspan='{total_columns}'>{html.escape(category)}</td></tr>"
        )
        parts.append(header_row_html())

        for name in by_category[category]:
            units = " / ".join(units_by_test.get(name, []))
            parts.append("<tr>")
            parts.append(f"<td class='test' width='{test_col_width:.2f}%' align='center' valign='middle'>{html.escape(name)}</td>")
            for date_text in dates:
                value = display_cell_for_report(lookup.get(name, {}).get(date_text, []), units_by_test.get(name, []))
                parts.append(f"<td class='value' width='{date_col_width:.2f}%' align='center' valign='middle'>{html.escape(value)}</td>")
            parts.append(f"<td class='units' width='{unit_col_width:.2f}%' align='center' valign='middle'>{html.escape(units)}</td>")
            range_text = format_reference_range(ranges_by_test.get(name))
            parts.append(f"<td class='range' width='{range_col_width:.2f}%' align='center' valign='middle'>{html.escape(range_text)}</td>")
            parts.append("</tr>")

    parts.append("</table>")
    parts.append("</body></html>")
    return "".join(parts)

def export_doctor_pdf(
    path: Path,
    records: List[Dict[str, Any]],
    source_path: Path,
    ranges_by_test: Optional[Dict[str, Dict[str, Any]]] = None,
    date_groups: Optional[List[List[str]]] = None,
    report_title_suffix: str = "",
    repeat_all_tests_for_each_group: bool = False,
) -> None:
    """
    V2.0 manual PDF renderer.

    Supports:
      - all dates
      - filtered date ranges / last N dates
      - split-wide export into date chunks in ONE PDF

    The split-wide mode repeats the same test rows for each date chunk, so the
    doctor can read each chunk as a normal table without columns becoming tiny.
    """
    ranges_by_test = ranges_by_test or {}

    all_dates, all_test_names, full_lookup, full_units_by_test = build_wide_history(records)
    if not all_dates:
        raise ValueError("No dated records available for PDF export.")

    if date_groups is None:
        date_groups = [all_dates]
    else:
        date_groups = [[d for d in group if d in all_dates] for group in date_groups]
        date_groups = [group for group in date_groups if group]

    if not date_groups:
        raise ValueError("No dates selected for PDF export.")

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(str(path))
    printer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    printer.setPageOrientation(QPageLayout.Orientation.Landscape)
    # V2.1.1: safer print margins. Some local print services clip near the page edge.
    printer.setPageMargins(QMarginsF(12, 10, 12, 14), QPageLayout.Unit.Millimeter)

    painter = QPainter()
    if not painter.begin(printer):
        raise RuntimeError("Could not start PDF painter.")

    try:
        page_rect = printer.pageRect(QPrinter.Unit.DevicePixel)
        left = float(page_rect.left())
        top = float(page_rect.top())
        width = float(page_rect.width())
        bottom = float(page_rect.bottom())

        scale = max(printer.resolution() / 72.0, 1.0)
        pad = 2.5 * scale

        title_font = QFont("Arial", 16)
        title_font.setBold(True)

        meta_font = QFont("Arial", 7)

        category_font = QFont("Arial", 9)
        category_font.setBold(True)

        header_font = QFont("Arial", 7)
        header_font.setBold(True)

        body_font = QFont("Arial", 7)

        border_pen = QPen(QColor("#b8b8b8"))
        text_pen = QPen(QColor("#222222"))
        meta_pen = QPen(QColor("#555555"))

        header_bg = QColor("#f2f2f2")
        category_bg = QColor("#e8e8e8")
        white_bg = QColor("#ffffff")
        low_bg = QColor("#fff2b3")
        high_bg = QColor("#ffd6d6")

        def pdf_bg_for_status(status: Optional[str]) -> QColor:
            if status == "low":
                return low_bg
            if status in {"high", "unexpected"}:
                return high_bg
            return white_bg

        def text_height(text: str, col_width: float, font: QFont, min_lines: int = 1) -> float:
            painter.setFont(font)
            fm = painter.fontMetrics()
            available_w = max(10, int(col_width - 2 * pad))
            rect = fm.boundingRect(
                0,
                0,
                available_w,
                100000,
                int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap),
                text or "-"
            )
            min_h = (fm.height() * min_lines) + 2 * pad
            return max(float(rect.height()) + 2 * pad, float(min_h))

        def draw_cell(x: float, y: float, w: float, h: float, text: str, font: QFont,
                      bg: Optional[QColor] = None,
                      align: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignCenter) -> None:
            rect = QRectF(x, y, w, h)
            if bg is not None:
                painter.fillRect(rect, bg)
            painter.setPen(border_pen)
            painter.drawRect(rect)
            painter.setPen(text_pen)
            painter.setFont(font)
            text_rect = QRectF(x + pad, y + pad, max(1, w - 2 * pad), max(1, h - 2 * pad))
            painter.drawText(text_rect, int(align | Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextWordWrap), text)

        def draw_full_width_row(y: float, h: float, text: str, font: QFont, bg: QColor,
                                align: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignLeft) -> float:
            rect = QRectF(left, y, width, h)
            painter.fillRect(rect, bg)
            painter.setPen(border_pen)
            painter.drawRect(rect)
            painter.setPen(text_pen)
            painter.setFont(font)
            text_rect = QRectF(left + pad, y + pad, width - 2 * pad, h - 2 * pad)
            painter.drawText(text_rect, int(align | Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextWordWrap), text)
            return y + h

        def draw_report_header(group_index: int, group_count: int, dates_for_group: List[str], first_page_for_group: bool) -> float:
            y = top
            painter.setPen(text_pen)
            painter.setFont(title_font)
            fm = painter.fontMetrics()

            # V2.0.1:
            # Keep the title short. Long export mode/date-group text belongs in
            # the metadata lines, where it can wrap without going off-page.
            title = "Blood Test History Report"
            if not first_page_for_group:
                title += " (continued)"

            painter.drawText(
                QRectF(left, y, width, fm.height() + 2 * pad),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                title
            )
            y += fm.height() + 3 * pad

            painter.setFont(meta_font)
            painter.setPen(meta_pen)
            fm = painter.fontMetrics()

            meta_lines = [
                f"Generated: {generated_at}",
                f"Source JSON: {source_path}",
                f"Records in source: {len(records)}",
            ]

            if report_title_suffix:
                meta_lines.append(f"Export mode: {report_title_suffix}")

            if group_count > 1:
                meta_lines.append(f"Date group {group_index + 1}/{group_count}: {dates_for_group[0]} to {dates_for_group[-1]}")

            meta_lines.extend([
                f"Dates in this table: {', '.join(dates_for_group)}",
                "Missing results are shown as '-'. Light yellow = below range; light red = above range or unexpected qualitative value. Units are shown at right.",
            ])

            for line in meta_lines:
                # Use wrapped text so long paths/mode labels never run off the page.
                rect = QRectF(left, y, width, fm.height() * 3 + pad)
                flags = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextWordWrap)
                bounded = fm.boundingRect(
                    0,
                    0,
                    int(width),
                    int(fm.height() * 4),
                    flags,
                    line
                )
                line_h = max(float(bounded.height()) + 0.5 * pad, float(fm.height() + pad))
                painter.drawText(QRectF(left, y, width, line_h), flags, line)
                y += line_h

            y += 2 * pad
            painter.setPen(text_pen)
            return y

        def new_page(group_index: int, group_count: int, dates_for_group: List[str]) -> float:
            printer.newPage()
            return draw_report_header(group_index, group_count, dates_for_group, first_page_for_group=False)

        def make_layout(dates_for_group: List[str]) -> Tuple[List[float], List[float]]:
            date_count = max(len(dates_for_group), 1)
            test_w = width * 0.22
            units_w = width * 0.10
            range_w = width * 0.16
            date_w = (width - test_w - units_w - range_w) / date_count

            col_widths = [test_w] + [date_w for _ in dates_for_group] + [units_w, range_w]
            col_x = [left]
            for w in col_widths[:-1]:
                col_x.append(col_x[-1] + w)
            return col_x, col_widths

        def draw_table_header(y: float, category: str, dates_for_group: List[str], col_x: List[float], col_widths: List[float]) -> float:
            category_h = text_height(category, width, category_font)
            header_labels = ["Test name"] + dates_for_group + ["Units", "Reference / Target Range"]
            header_h = max(text_height(label, col_widths[i], header_font) for i, label in enumerate(header_labels))

            y = draw_full_width_row(y, category_h, category, category_font, category_bg)

            for i, label in enumerate(header_labels):
                draw_cell(col_x[i], y, col_widths[i], header_h, label, header_font, header_bg)
            return y + header_h

        def row_height_for(name: str, row_values: List[str], units: str, range_text: str, col_widths: List[float]) -> float:
            values = [name] + row_values + [units, range_text]
            return max(text_height(val or "-", col_widths[i], body_font) for i, val in enumerate(values))

        def draw_data_row(y: float, name: str, row_values: List[str], units: str, range_text: str,
                          row_h: float, col_x: List[float], col_widths: List[float],
                          row_statuses: Optional[List[Optional[str]]] = None) -> float:
            row_statuses = row_statuses or []
            values = [name] + row_values + [units, range_text]
            for i, val in enumerate(values):
                bg = white_bg
                if 1 <= i <= len(row_values):
                    bg = pdf_bg_for_status(row_statuses[i - 1] if i - 1 < len(row_statuses) else None)
                draw_cell(col_x[i], y, col_widths[i], row_h, val or "-", body_font, bg)
            return y + row_h

        group_count = len(date_groups)
        first_printer_page = True

        for group_index, dates_for_group in enumerate(date_groups):
            if group_index > 0:
                printer.newPage()
                first_printer_page = False

            col_x, col_widths = make_layout(dates_for_group)
            y = draw_report_header(group_index, group_count, dates_for_group, first_page_for_group=True)

            # Test rows:
            # - normal exports show tests that have at least one result in the selected dates
            # - split-wide mode repeats all tests across every chunk for consistent reading
            if repeat_all_tests_for_each_group:
                test_names_for_group = all_test_names
            else:
                records_for_group = filter_records_by_dates(records, dates_for_group)
                _, test_names_for_group, _, _ = build_wide_history(records_for_group)

            by_category: Dict[str, List[str]] = defaultdict(list)
            for name in test_names_for_group:
                by_category[test_category(name)].append(name)

            for category in sorted(by_category.keys(), key=lambda c: CATEGORY_ORDER.get(c, 999)):
                names = by_category[category]
                if not names:
                    continue

                # Category + header + first data row are kept together.
                first_name = names[0]
                first_values = [
                    display_cell_for_report(full_lookup.get(first_name, {}).get(date_text, []), full_units_by_test.get(first_name, []))
                    for date_text in dates_for_group
                ]
                first_units = " / ".join(full_units_by_test.get(first_name, []))
                first_range = format_reference_range(ranges_by_test.get(first_name))
                first_row_h = row_height_for(first_name, first_values, first_units, first_range, col_widths)

                category_h = text_height(category, width, category_font)
                header_labels = ["Test name"] + dates_for_group + ["Units", "Reference / Target Range"]
                header_h = max(text_height(label, col_widths[i], header_font) for i, label in enumerate(header_labels))

                if y + category_h + header_h + first_row_h > bottom:
                    y = new_page(group_index, group_count, dates_for_group)

                y = draw_table_header(y, category, dates_for_group, col_x, col_widths)

                for name in names:
                    row_values = [
                        display_cell_for_report(full_lookup.get(name, {}).get(date_text, []), full_units_by_test.get(name, []))
                        for date_text in dates_for_group
                    ]
                    range_entry = ranges_by_test.get(name)
                    row_statuses = [
                        combined_status_for_records(full_lookup.get(name, {}).get(date_text, []), range_entry)
                        for date_text in dates_for_group
                    ]
                    units = " / ".join(full_units_by_test.get(name, []))
                    range_text = format_reference_range(range_entry)
                    row_h = row_height_for(name, row_values, units, range_text, col_widths)

                    if y + row_h > bottom:
                        y = new_page(group_index, group_count, dates_for_group)
                        continued = f"{category} (continued)"
                        y = draw_table_header(y, continued, dates_for_group, col_x, col_widths)

                    y = draw_data_row(y, name, row_values, units, range_text, row_h, col_x, col_widths, row_statuses)

            first_printer_page = False

    finally:
        painter.end()



# -----------------------------
# Main UI
# -----------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.app_dir = get_app_dir()
        self.store = BloodTestStore(self.app_dir)
        self.loading = False
        self.dirty = False

        self.setWindowTitle(f"{APP_TITLE} V{APP_VERSION}")
        self.resize(1500, 900)
        apply_app_icon(self)

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)

        top = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search test name, category, date, value, unit, or source file...")
        self.reload_btn = QPushButton("Reload JSON")
        self.save_btn = QPushButton("Save Records")
        self.validate_btn = QPushButton("Validate")
        self.add_row_btn = QPushButton("Add Blank Record")
        self.delete_rows_btn = QPushButton("Delete Selected Rows")
        self.import_json_btn = QPushButton("Import JSON")
        self.import_csv_btn = QPushButton("Import CSV")
        self.open_data_btn = QPushButton("Open DATA Folder")
        self.open_exports_btn = QPushButton("Open Exports")

        top.addWidget(QLabel("Filter:"))
        top.addWidget(self.search_edit, 1)
        top.addWidget(self.reload_btn)
        top.addWidget(self.save_btn)
        top.addWidget(self.validate_btn)
        top.addWidget(self.add_row_btn)
        top.addWidget(self.delete_rows_btn)
        top.addWidget(self.import_json_btn)
        top.addWidget(self.import_csv_btn)
        top.addWidget(self.open_data_btn)
        top.addWidget(self.open_exports_btn)
        layout.addLayout(top)

        export_row = QHBoxLayout()
        self.export_csv_btn = QPushButton("Export Doctor CSV")
        self.export_md_btn = QPushButton("Export Doctor Markdown")
        self.export_date_combo = QComboBox()
        self.export_date_combo.setMinimumWidth(120)
        self.export_date_combo.setToolTip("Select one blood-test date for a compact Markdown export.")
        self.export_date_md_btn = QPushButton("Markdown: Selected Date")
        self.export_each_date_md_btn = QPushButton("Markdown: Each Date")
        self.export_ai_bundle_btn = QPushButton("Export AI Bundle")
        self.export_pdf_all_btn = QPushButton("PDF: All Dates")
        self.export_pdf_last6_btn = QPushButton("PDF: Last 6 Dates")
        self.export_pdf_range_btn = QPushButton("PDF: Custom Range")
        self.export_pdf_split_btn = QPushButton("PDF: Split Chunks")
        export_row.addStretch(1)
        export_row.addWidget(self.export_csv_btn)
        export_row.addWidget(self.export_md_btn)
        export_row.addWidget(QLabel("Date:"))
        export_row.addWidget(self.export_date_combo)
        export_row.addWidget(self.export_date_md_btn)
        export_row.addWidget(self.export_each_date_md_btn)
        export_row.addWidget(self.export_ai_bundle_btn)
        export_row.addWidget(self.export_pdf_all_btn)
        export_row.addWidget(self.export_pdf_last6_btn)
        export_row.addWidget(self.export_pdf_range_btn)
        export_row.addWidget(self.export_pdf_split_btn)
        layout.addLayout(export_row)

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #666666;")
        layout.addWidget(self.status_label)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        self.history_table = QTableWidget()
        self.history_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.history_table.setAlternatingRowColors(True)
        self.history_table.setSortingEnabled(False)

        self.records_table = QTableWidget()
        self.records_table.setAlternatingRowColors(True)
        self.records_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.records_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.records_table.setSortingEnabled(False)

        self.tabs.addTab(self.history_table, "History Table")
        self.tabs.addTab(self.records_table, "Raw Records / Edit")

        self.search_edit.textChanged.connect(self.refresh_tables)
        self.reload_btn.clicked.connect(self.reload_json)
        self.save_btn.clicked.connect(self.save_records_from_table)
        self.validate_btn.clicked.connect(self.validate_archive)
        self.add_row_btn.clicked.connect(self.add_blank_record_row)
        self.delete_rows_btn.clicked.connect(self.delete_selected_record_rows)
        self.import_json_btn.clicked.connect(self.import_json)
        self.import_csv_btn.clicked.connect(self.import_csv)
        self.export_csv_btn.clicked.connect(self.export_doctor_csv_clicked)
        self.export_md_btn.clicked.connect(self.export_doctor_markdown_clicked)
        self.export_date_md_btn.clicked.connect(self.export_selected_date_markdown_clicked)
        self.export_each_date_md_btn.clicked.connect(self.export_each_date_markdown_clicked)
        self.export_ai_bundle_btn.clicked.connect(self.export_ai_bundle_clicked)
        self.export_pdf_all_btn.clicked.connect(self.export_doctor_pdf_all_clicked)
        self.export_pdf_last6_btn.clicked.connect(self.export_doctor_pdf_last6_clicked)
        self.export_pdf_range_btn.clicked.connect(self.export_doctor_pdf_custom_range_clicked)
        self.export_pdf_split_btn.clicked.connect(self.export_doctor_pdf_split_chunks_clicked)
        self.open_data_btn.clicked.connect(lambda: open_folder(self.store.archive_dir))
        self.open_exports_btn.clicked.connect(lambda: open_folder(self.store.exports_dir))
        self.records_table.itemChanged.connect(self.on_record_item_changed)

        self.refresh_tables()
        self.set_dirty(False)

    def set_dirty(self, dirty: bool) -> None:
        self.dirty = dirty
        marker = " *" if self.dirty else ""
        self.setWindowTitle(f"{APP_TITLE} V{APP_VERSION}{marker}")
        self.update_status()

    def update_status(self) -> None:
        dates = self.store.unique_dates()
        dirty_text = " | UNSAVED CHANGES" if self.dirty else ""
        ranges_status = f" | {len(self.store.ranges_by_test)} ranges" if getattr(self.store, "ranges_by_test", None) else " | no ranges loaded"
        self.status_label.setText(
            f"Data file: {self.store.results_path} | "
            f"{len(self.store.records)} records | "
            f"{len(dates)} dates{ranges_status} | coloring on{dirty_text}"
        )

    def query(self) -> str:
        return self.search_edit.text().strip().lower()

    def record_matches_filter(self, record: Dict[str, Any]) -> bool:
        q = self.query()
        if not q:
            return True
        haystack = " ".join([
            stringify(record.get("date")),
            stringify(record.get("test_name_en")),
            test_category(stringify(record.get("test_name_en"))),
            stringify(record.get("value")),
            stringify(record.get("unit")),
            source_files_to_text(record.get("source_files")),
        ]).lower()
        return q in haystack

    def visible_records(self) -> List[Dict[str, Any]]:
        return [r for r in self.store.records if self.record_matches_filter(r)]

    def refresh_tables(self) -> None:
        if self.loading:
            return
        self.loading = True
        try:
            self.refresh_export_date_combo()
            self.populate_history_table()
            self.populate_records_table()
            self.update_status()
        finally:
            self.loading = False

    def refresh_export_date_combo(self) -> None:
        current = self.export_date_combo.currentText()
        dates = self.store.unique_dates()

        self.export_date_combo.blockSignals(True)
        self.export_date_combo.clear()
        self.export_date_combo.addItems(dates)
        if current in dates:
            self.export_date_combo.setCurrentText(current)
        elif dates:
            self.export_date_combo.setCurrentIndex(len(dates) - 1)
        self.export_date_combo.blockSignals(False)

        has_dates = bool(dates)
        self.export_date_combo.setEnabled(has_dates)
        self.export_date_md_btn.setEnabled(has_dates)
        self.export_each_date_md_btn.setEnabled(has_dates)

    def range_tooltip_for_test(self, test_name: str) -> str:
        entry = self.store.range_for_test(test_name)
        if not isinstance(entry, dict):
            return "No reference / target range loaded for this test."

        lines = [
            f"{test_name}",
            f"Reference / Target Range: {format_reference_range(entry)}",
            f"Range type: {stringify(entry.get('range_type')) or '-'}",
            f"Source type: {stringify(entry.get('source_type')) or '-'}",
            f"Confidence: {stringify(entry.get('confidence')) or '-'}",
            f"Use for coloring later: {entry.get('use_for_coloring', False)}",
        ]

        notes = stringify(entry.get("notes"))
        if notes:
            lines.append("")
            lines.append(notes)

        unit_note = stringify(entry.get("unit_note"))
        if unit_note:
            lines.append("")
            lines.append(unit_note)

        return "\n".join(lines)

    def populate_history_table(self) -> None:
        visible_records = self.visible_records()
        dates, test_names, lookup, units_by_test = build_wide_history(visible_records)

        self.history_table.clear()
        self.history_table.setRowCount(len(test_names))
        self.history_table.setColumnCount(4 + len(dates))
        self.history_table.setHorizontalHeaderLabels(["Category", "Test name"] + dates + ["Units", "Reference / Target Range"])

        for row_idx, name in enumerate(test_names):
            category = test_category(name)
            values = [category, name]
            for col_idx, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.history_table.setItem(row_idx, col_idx, item)

            for col_offset, date_text in enumerate(dates, start=2):
                recs = lookup.get(name, {}).get(date_text, [])
                item = QTableWidgetItem(display_cell_for_report(recs, units_by_test.get(name, [])))
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)

                status = combined_status_for_records(recs, self.store.range_for_test(name))
                bg = color_for_range_status(status)
                if bg is not None:
                    item.setBackground(bg)
                    item.setForeground(QColor("#ffffff"))

                if recs:
                    tooltip_lines = []
                    for r in recs:
                        tooltip_lines.append(
                            f"{r.get('date', '')} | {r.get('test_name_en', '')}\n"
                            f"Value: {r.get('value', '')}\n"
                            f"Unit: {r.get('unit', '')}\n"
                            f"Sources: {source_files_to_text(r.get('source_files'))}"
                        )
                    item.setToolTip("\n\n".join(tooltip_lines))
                else:
                    item.setToolTip("No result available for this date.")

                self.history_table.setItem(row_idx, col_offset, item)

            units_col = 2 + len(dates)
            units_item = QTableWidgetItem(" / ".join(units_by_test.get(name, [])))
            units_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            units_item.setFlags(units_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.history_table.setItem(row_idx, units_col, units_item)

            range_col = units_col + 1
            range_text = self.store.range_text_for_test(name)
            range_item = QTableWidgetItem(range_text)
            range_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            range_item.setToolTip(self.range_tooltip_for_test(name))
            range_item.setFlags(range_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.history_table.setItem(row_idx, range_col, range_item)

        header = self.history_table.horizontalHeader()

        # V0.3.5 app-table readability:
        # - Test name can take the extra room.
        # - All date columns get the exact same fixed width.
        # - Long values no longer make one date column huge.
        # - Full values are still available in tooltips when a result exists.
        date_col_width = 112
        units_col_width = 115

        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)  # Category
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)           # Test name

        for col in range(2, 2 + len(dates)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
            self.history_table.setColumnWidth(col, date_col_width)

        units_col = 2 + len(dates)
        header.setSectionResizeMode(units_col, QHeaderView.ResizeMode.Fixed)
        self.history_table.setColumnWidth(units_col, units_col_width)

        range_col = units_col + 1
        header.setSectionResizeMode(range_col, QHeaderView.ResizeMode.Fixed)
        self.history_table.setColumnWidth(range_col, 170)

        self.history_table.verticalHeader().setVisible(False)

    def populate_records_table(self) -> None:
        visible_records = self.visible_records()

        self.records_table.clear()
        self.records_table.setRowCount(len(visible_records))
        self.records_table.setColumnCount(7)
        self.records_table.setHorizontalHeaderLabels([
            "Record ID",
            "Date",
            "Category",
            "English test name",
            "Value",
            "Unit",
            "Source files",
        ])

        for row_idx, record in enumerate(visible_records):
            values = [
                stringify(record.get("record_id")),
                stringify(record.get("date")),
                test_category(stringify(record.get("test_name_en"))),
                stringify(record.get("test_name_en")),
                stringify(record.get("value")),
                normalize_unit(record.get("unit")),
                source_files_to_text(record.get("source_files")),
            ]

            for col_idx, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if col_idx in (0, 2):
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)

                # Color only the Value cell in the raw editor, based on its test's range.
                if col_idx == 4:
                    status = range_status_for_value(record.get("value"), self.store.range_for_test(record.get("test_name_en", "")))
                    bg = color_for_range_status(status)
                    if bg is not None:
                        item.setBackground(bg)
                        item.setForeground(QColor("#ffffff"))

                self.records_table.setItem(row_idx, col_idx, item)

        header = self.records_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self.records_table.verticalHeader().setVisible(False)

    def on_record_item_changed(self, item: QTableWidgetItem) -> None:
        if self.loading:
            return
        self.set_dirty(True)

        # Update category if test name changed.
        if item.column() == 3:
            category_item = self.records_table.item(item.row(), 2)
            if category_item:
                category_item.setText(test_category(item.text()))

    def ask_unsaved(self, action: str = "continue") -> bool:
        if not self.dirty:
            return True

        choice = QMessageBox.question(
            self,
            APP_TITLE,
            f"You have unsaved changes.\n\nSave before you {action}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes,
        )

        if choice == QMessageBox.StandardButton.Cancel:
            return False
        if choice == QMessageBox.StandardButton.Yes:
            return self.save_records_from_table(show_success=False)
        return True

    def reload_json(self) -> None:
        if not self.ask_unsaved("reload the JSON"):
            return
        self.store.load_or_create()
        self.store.load_ranges()
        self.refresh_tables()
        self.set_dirty(False)
        QMessageBox.information(self, APP_TITLE, "Reloaded blood_test_results.json and test_ranges.json.")

    def collect_records_from_records_table(self) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []

        for row in range(self.records_table.rowCount()):
            record_id = stringify(self.item_text(row, 0))
            date_text = stringify(self.item_text(row, 1))
            test_name = stringify(self.item_text(row, 3))
            value = stringify(self.item_text(row, 4))
            unit = normalize_unit(self.item_text(row, 5))
            sources = parse_source_files(self.item_text(row, 6))

            if not date_text and not test_name and not value:
                continue

            records.append({
                "record_id": record_id,
                "date": date_text,
                "test_name_en": test_name,
                "value": value,
                "unit": unit,
                "source_files": sources,
            })

        return records

    def sync_visible_table_into_store(self) -> bool:
        """
        Merge the visible Raw Records table into the in-memory store.
        Important: if search/filter is active, hidden records are preserved.
        """
        try:
            edited_visible = self.collect_records_from_records_table()
            edited_ids = {stringify(r.get("record_id")) for r in edited_visible if stringify(r.get("record_id"))}

            if self.query():
                hidden = [r for r in self.store.records if stringify(r.get("record_id")) not in edited_ids]
                self.store.replace_records(hidden + edited_visible)
            else:
                self.store.replace_records(edited_visible)
            return True
        except Exception as exc:
            QMessageBox.critical(self, APP_TITLE, f"Could not read the edit table:\n\n{exc}")
            return False

    def save_records_from_table(self, show_success: bool = True) -> bool:
        try:
            if not self.sync_visible_table_into_store():
                return False
            self.store.save(make_backup=True)
            self.refresh_tables()
            self.set_dirty(False)
            if show_success:
                QMessageBox.information(
                    self,
                    APP_TITLE,
                    f"Saved {len(self.store.records)} records.\n\n"
                    f"File:\n{self.store.results_path}\n\n"
                    f"A timestamped backup was created in:\n{self.store.backup_dir}"
                )
            return True
        except Exception as exc:
            QMessageBox.critical(self, APP_TITLE, f"Save failed:\n\n{exc}")
            return False

    def item_text(self, row: int, col: int) -> str:
        item = self.records_table.item(row, col)
        if item is None:
            return ""
        return item.text()

    def add_blank_record_row(self) -> None:
        self.tabs.setCurrentWidget(self.records_table)
        row = self.records_table.rowCount()
        self.records_table.insertRow(row)

        today = datetime.now().strftime("%Y-%m-%d")
        defaults = ["", today, "Other", "", "", "-", ""]
        for col, value in enumerate(defaults):
            item = QTableWidgetItem(value)
            if col in (0, 2):
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.records_table.setItem(row, col, item)

        self.records_table.scrollToItem(self.records_table.item(row, 1))
        self.records_table.setCurrentCell(row, 3)
        self.set_dirty(True)

    def delete_selected_record_rows(self) -> None:
        self.tabs.setCurrentWidget(self.records_table)
        rows = sorted({idx.row() for idx in self.records_table.selectedIndexes()}, reverse=True)

        if not rows:
            QMessageBox.information(self, APP_TITLE, "Select one or more rows in the Raw Records / Edit tab first.")
            return

        confirm = QMessageBox.question(
            self,
            APP_TITLE,
            f"Delete {len(rows)} selected row(s)?\n\n"
            f"This is not written to JSON until you press Save Records."
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        ids_to_remove = set()
        for row in rows:
            rid = stringify(self.item_text(row, 0))
            if rid:
                ids_to_remove.add(rid)
            self.records_table.removeRow(row)

        if ids_to_remove:
            self.store.records = [r for r in self.store.records if stringify(r.get("record_id")) not in ids_to_remove]

        self.set_dirty(True)
        self.refresh_tables()

    def import_json(self) -> None:
        if not self.ask_unsaved("import JSON"):
            return

        path_text, _ = QFileDialog.getOpenFileName(
            self,
            "Import JSON records",
            str(self.store.archive_dir),
            "JSON Files (*.json);;All Files (*.*)",
        )
        if not path_text:
            return

        try:
            records = records_from_json(Path(path_text))
            self.import_records(records, Path(path_text).name)
        except Exception as exc:
            QMessageBox.critical(self, APP_TITLE, f"JSON import failed:\n\n{exc}")

    def import_csv(self) -> None:
        if not self.ask_unsaved("import CSV"):
            return

        path_text, _ = QFileDialog.getOpenFileName(
            self,
            "Import CSV records",
            str(self.store.archive_dir),
            "CSV Files (*.csv);;All Files (*.*)",
        )
        if not path_text:
            return

        try:
            records = records_from_csv(Path(path_text))
            self.import_records(records, Path(path_text).name)
        except Exception as exc:
            QMessageBox.critical(self, APP_TITLE, f"CSV import failed:\n\n{exc}")

    def import_records(self, records: List[Dict[str, Any]], source_label: str) -> None:
        if not records:
            QMessageBox.information(self, APP_TITLE, "No records were found to import.")
            return

        choice = QMessageBox.question(
            self,
            APP_TITLE,
            f"Imported {len(records)} records from:\n{source_label}\n\n"
            f"Choose Yes to APPEND them to the current archive.\n"
            f"Choose No to REPLACE the current archive with imported records.\n"
            f"Choose Cancel to stop.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes,
        )

        if choice == QMessageBox.StandardButton.Cancel:
            return
        if choice == QMessageBox.StandardButton.Yes:
            self.store.add_records(records)
        else:
            self.store.replace_records(records)

        self.refresh_tables()
        self.set_dirty(True)
        QMessageBox.information(
            self,
            APP_TITLE,
            f"Import loaded into memory.\n\nPress Save Records to write it to:\n{self.store.results_path}"
        )

    def validation_messages(self) -> Tuple[List[str], List[str]]:
        if not self.sync_visible_table_into_store():
            return ["Could not sync current edit table before validation."], []

        errors: List[str] = []
        warnings: List[str] = []

        seen_key: Dict[Tuple[str, str, str], int] = {}
        units_by_test: Dict[str, set] = defaultdict(set)

        for idx, r in enumerate(self.store.records, start=1):
            date_text = stringify(r.get("date"))
            name = stringify(r.get("test_name_en"))
            value = stringify(r.get("value"))
            unit = normalize_unit(r.get("unit"))

            if not date_text:
                errors.append(f"Record {idx}: missing date.")
            elif not is_valid_date(date_text):
                errors.append(f"Record {idx}: invalid date format '{date_text}'. Expected YYYY-MM-DD.")

            if not name:
                errors.append(f"Record {idx}: missing English test name.")

            if not value:
                warnings.append(f"Record {idx}: missing/empty value for {date_text} / {name or '(missing test name)'}.")
            if not unit:
                warnings.append(f"Record {idx}: missing unit for {date_text} / {name or '(missing test name)'}. Use, if no unit.")

            key = (date_text, name, unit)
            seen_key[key] = seen_key.get(key, 0) + 1
            if name:
                units_by_test[name].add(unit)

        for (date_text, name, unit), count in sorted(seen_key.items()):
            if date_text and name and count > 1:
                warnings.append(f"Possible duplicate: {date_text} / {name} / {unit} appears {count} times.")

        for name, units in sorted(units_by_test.items()):
            clean_units = sorted(u for u in units if u)
            if len(clean_units) > 1:
                warnings.append(f"Multiple units for '{name}': {', '.join(clean_units)}.")

        for name in self.store.unique_test_names():
            if name not in self.store.ranges_by_test:
                warnings.append(f"No reference / target range entry found for '{name}'.")

        return errors, warnings

    def validate_archive(self) -> None:
        errors, warnings = self.validation_messages()

        if not errors and not warnings:
            QMessageBox.information(self, APP_TITLE, "Validation passed. No errors or warnings found.")
            return

        text_parts = []
        if errors:
            text_parts.append("ERRORS:\n" + "\n".join(f"- {e}" for e in errors[:80]))
            if len(errors) > 80:
                text_parts.append(f"... {len(errors) - 80} more errors.")
        if warnings:
            text_parts.append("WARNINGS:\n" + "\n".join(f"- {w}" for w in warnings[:120]))
            if len(warnings) > 120:
                text_parts.append(f"... {len(warnings) - 120} more warnings.")

        QMessageBox.warning(self, APP_TITLE, "\n\n".join(text_parts))

    def ensure_export_ready(self) -> bool:
        # Include unsaved edits in exports by syncing visible table into store.
        return self.sync_visible_table_into_store()

    def default_export_path(self, suffix: str, extension: str) -> Path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return self.store.exports_dir / f"blood_test_{suffix}_{stamp}.{extension}"

    def export_doctor_csv_clicked(self) -> None:
        if not self.ensure_export_ready():
            return

        default_path = self.default_export_path("doctor_report", "csv")
        path_text, _ = QFileDialog.getSaveFileName(
            self,
            "Export doctor-readable CSV",
            str(default_path),
            "CSV Files (*.csv)",
        )
        if not path_text:
            return

        try:
            export_doctor_csv(Path(path_text), self.store.records, self.store.ranges_by_test)
            QMessageBox.information(self, APP_TITLE, f"Exported CSV:\n{path_text}")
        except Exception as exc:
            QMessageBox.critical(self, APP_TITLE, f"CSV export failed:\n\n{exc}")

    def export_doctor_markdown_clicked(self) -> None:
        if not self.ensure_export_ready():
            return

        default_path = self.default_export_path("doctor_report", "md")
        path_text, _ = QFileDialog.getSaveFileName(
            self,
            "Export doctor-readable Markdown",
            str(default_path),
            "Markdown Files (*.md);;Text Files (*.txt)",
        )
        if not path_text:
            return

        try:
            md = build_doctor_markdown(self.store.records, self.store.results_path, self.store.ranges_by_test)
            Path(path_text).write_text(md, encoding="utf-8")
            QMessageBox.information(self, APP_TITLE, f"Exported Markdown:\n{path_text}")
        except Exception as exc:
            QMessageBox.critical(self, APP_TITLE, f"Markdown export failed:\n\n{exc}")

    def export_selected_date_markdown_clicked(self) -> None:
        if not self.ensure_export_ready():
            return

        self.refresh_export_date_combo()
        selected_date = stringify(self.export_date_combo.currentText())
        if not selected_date:
            QMessageBox.information(self, APP_TITLE, "No test date is available to export.")
            return

        records = filter_records_by_dates(self.store.records, [selected_date])
        if not records:
            QMessageBox.information(self, APP_TITLE, f"No records found for {selected_date}.")
            return

        default_path = self.default_export_path(f"doctor_report_{selected_date}", "md")
        path_text, _ = QFileDialog.getSaveFileName(
            self,
            f"Export Markdown - {selected_date}",
            str(default_path),
            "Markdown Files (*.md);;Text Files (*.txt)",
        )
        if not path_text:
            return

        try:
            md = build_doctor_markdown(records, self.store.results_path, self.store.ranges_by_test)
            Path(path_text).write_text(md, encoding="utf-8")
            QMessageBox.information(
                self,
                APP_TITLE,
                f"Exported Markdown for {selected_date}:\n{path_text}\n\n"
                f"Records included: {len(records)}"
            )
        except Exception as exc:
            QMessageBox.critical(self, APP_TITLE, f"Selected-date Markdown export failed:\n\n{exc}")

    def export_each_date_markdown_clicked(self) -> None:
        if not self.ensure_export_ready():
            return

        dates = self.store.unique_dates()
        if not dates:
            QMessageBox.information(self, APP_TITLE, "No test dates are available to export.")
            return

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        bundle_dir = self.store.exports_dir / f"MARKDOWN_BY_DATE_{stamp}"
        bundle_dir.mkdir(parents=True, exist_ok=True)

        try:
            written_files: List[Path] = []
            for date_text in dates:
                records = filter_records_by_dates(self.store.records, [date_text])
                if not records:
                    continue

                date_filename = re.sub(r"[^A-Za-z0-9._-]+", "_", date_text).strip("._-")
                if not date_filename:
                    date_filename = safe_slug(date_text, "unknown_date")

                path = bundle_dir / f"blood_test_{date_filename}.md"
                md = build_doctor_markdown(records, self.store.results_path, self.store.ranges_by_test)
                path.write_text(md, encoding="utf-8")
                written_files.append(path)

            if not written_files:
                QMessageBox.information(self, APP_TITLE, "No dated records were found to export.")
                return

            QMessageBox.information(
                self,
                APP_TITLE,
                f"Exported one Markdown file per date:\n{bundle_dir}\n\n"
                f"Files created: {len(written_files)}"
            )
            open_folder(bundle_dir)

        except Exception as exc:
            QMessageBox.critical(self, APP_TITLE, f"Per-date Markdown export failed:\n\n{exc}")

    def export_ai_bundle_clicked(self) -> None:
        """
        Export a machine-readable bundle for AI analysis.

        This is intentionally different from the doctor Markdown/PDF:
        - exact JSON records are included
        - exact range JSON is included
        - flagged values are precomputed
        - wide CSV is included for spreadsheet-like parsing
        - a short AI_CONTEXT.md tells the AI how to read the bundle
        """
        if not self.ensure_export_ready():
            return

        if not self.store.records:
            QMessageBox.information(self, APP_TITLE, "No records available for AI export.")
            return

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        bundle_dir = self.store.exports_dir / f"AI_ANALYSIS_EXPORT_{stamp}"
        bundle_dir.mkdir(parents=True, exist_ok=True)

        try:
            dates, test_names, lookup, units_by_test = build_wide_history(self.store.records)
            exported_records = [
                normalize_record_for_ai_export(record)
                for record in self.store.records
            ]

            # 1) Exact normalized records, with current in-memory edits.
            records_payload = {
                "schema_version": 1,
                "export_type": "ai_analysis_normalized_records",
                "exported_at": datetime.now().isoformat(timespec="seconds"),
                "source_results_path": str(self.store.results_path),
                "records_count": len(exported_records),
                "dates": dates,
                "records": exported_records,
            }
            write_json(bundle_dir / "blood_test_results.normalized.json", records_payload)

            # 2) Exact ranges used for reference/coloring.
            ranges_payload = self.store.ranges_data if isinstance(self.store.ranges_data, dict) and self.store.ranges_data else {
                "schema_version": 1,
                "ranges": list(self.store.ranges_by_test.values()),
            }
            ranges_payload = clean_json_export_value(ranges_payload)
            write_json(bundle_dir / "test_ranges.json", ranges_payload)

            # 3) Wide history CSV.
            export_doctor_csv(bundle_dir / "wide_history_with_ranges.csv", self.store.records, self.store.ranges_by_test)

            # 4) Flagged values CSV + JSON.
            flagged_rows = []
            for record in self.store.records:
                test_name = stringify(record.get("test_name_en"))
                range_entry = self.store.range_for_test(test_name)
                status = range_status_for_value(record.get("value"), range_entry)

                if status:
                    flagged_rows.append(clean_json_export_value({
                        "date": stringify(record.get("date")),
                        "category": test_category(test_name),
                        "test_name_en": test_name,
                        "value": stringify(record.get("value")),
                        "unit": normalize_unit(record.get("unit")),
                        "flag": status,
                        "reference_target_range": format_reference_range(range_entry),
                        "range_type": stringify(range_entry.get("range_type")) if isinstance(range_entry, dict) else "",
                        "source_type": stringify(range_entry.get("source_type")) if isinstance(range_entry, dict) else "",
                        "confidence": stringify(range_entry.get("confidence")) if isinstance(range_entry, dict) else "",
                        "notes": stringify(range_entry.get("notes")) if isinstance(range_entry, dict) else "",
                        "source_files": source_files_to_text(record.get("source_files")),
                    }))

            flagged_json = {
                "schema_version": 1,
                "export_type": "ai_analysis_flagged_values",
                "exported_at": datetime.now().isoformat(timespec="seconds"),
                "flag_policy": {
                    "low": "below range",
                    "high": "above range",
                    "unexpected": "qualitative result outside expected values",
                    "only_if_use_for_coloring": True,
                },
                "flagged_count": len(flagged_rows),
                "flagged_values": flagged_rows,
            }
            write_json(bundle_dir / "flagged_values.json", flagged_json)

            with (bundle_dir / "flagged_values.csv").open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=[
                        "date", "category", "test_name_en", "value", "unit", "flag",
                        "reference_target_range", "range_type", "source_type",
                        "confidence", "notes", "source_files"
                    ],
                )
                writer.writeheader()
                writer.writerows(flagged_rows)

            # 5) Source file map.
            source_map: Dict[str, List[str]] = defaultdict(list)
            for record in self.store.records:
                date_text = stringify(record.get("date"))
                for source_file in parse_source_files(record.get("source_files")):
                    if source_file and source_file not in source_map[date_text]:
                        source_map[date_text].append(source_file)

            source_payload = {
                "schema_version": 1,
                "export_type": "ai_analysis_source_file_map",
                "exported_at": datetime.now().isoformat(timespec="seconds"),
                "source_files_by_date": dict(sorted(source_map.items())),
            }
            source_payload = clean_json_export_value(source_payload)
            write_json(bundle_dir / "source_files_by_date.json", source_payload)

            # 6) Compact AI instructions/context.
            ai_context = f"""# Blood Test AI Analysis Bundle

This folder is optimized for AI analysis, not for human printing.

## Preferred reading order for the AI

1. `blood_test_results.normalized.json`, exact normalized longitudinal records.
2. `test_ranges.json`, reference/target ranges, reliability notes, and coloring policy.
3. `flagged_values.json` or `flagged_values.csv`, precomputed low/high/unexpected values.
4. `wide_history_with_ranges.csv`, spreadsheet-style view if a wide table is useful.
5. `source_files_by_date.json`, original PDF/source filenames grouped by date.

## Important interpretation rules

- Dates are intended as blood draw/test dates when known, not necessarily PDF result-publication dates.
- Decimal dots are normalized.
- Some equivalent units were normalized, for example B12 ng/L to pg/mL and vitamin D µg/L to ng/mL.
- Missing results are represented as `-` in wide exports.
- The `Reference / Target Range` values are visual screening aids, not diagnoses.
- `use_for_coloring=false` in `test_ranges.json` means the value is too lab-dependent, timing-dependent, qualitative, or unreliable for automatic coloring.
- Light yellow in the app/PDF means below the configured range.
- Light red means above the configured range or unexpected qualitative value.
- For clinical interpretation, prioritize trends, repeated abnormalities, symptoms, medications/supplements, fasting status, hydration, illness, and the doctor's judgment.

## Export summary

- Exported at: {datetime.now().isoformat(timespec="seconds")}
- Records: {len(self.store.records)}
- Dates: {", ".join(dates)}
- Unique tests: {len(test_names)}
- Flagged values: {len(flagged_rows)}
- Results path in app: `{self.store.results_path}`
- Ranges path in app: `{self.store.ranges_path}`
"""
            (bundle_dir / "AI_CONTEXT.md").write_text(ai_context, encoding="utf-8")

            # 7) Manifest.
            manifest = {
                "schema_version": 1,
                "export_type": "blood_test_ai_analysis_bundle",
                "exported_at": datetime.now().isoformat(timespec="seconds"),
                "app_title": APP_TITLE,
                "app_version": APP_VERSION,
                "records_count": len(self.store.records),
                "unique_dates_count": len(dates),
                "unique_tests_count": len(test_names),
                "flagged_values_count": len(flagged_rows),
                "files": [
                    "AI_CONTEXT.md",
                    "blood_test_results.normalized.json",
                    "test_ranges.json",
                    "flagged_values.json",
                    "flagged_values.csv",
                    "wide_history_with_ranges.csv",
                    "source_files_by_date.json",
                    "manifest.json",
                ],
            }
            write_json(bundle_dir / "manifest.json", manifest)
            validated_json_paths = validate_bundle_json_files(bundle_dir, manifest["files"])

            QMessageBox.information(
                self,
                APP_TITLE,
                f"Exported AI analysis bundle:\n{bundle_dir}\n\n"
                f"Files created: {len(manifest['files'])}\n"
                f"JSON files validated: {len(validated_json_paths)}\n"
                f"Flagged values: {len(flagged_rows)}"
            )
            open_folder(bundle_dir)

        except Exception as exc:
            QMessageBox.critical(self, APP_TITLE, f"AI bundle export failed:\n\n{exc}")

    def export_pdf_with_options(
        self,
        suffix: str,
        dialog_title: str,
        records: List[Dict[str, Any]],
        report_title_suffix: str,
        date_groups: Optional[List[List[str]]] = None,
        repeat_all_tests_for_each_group: bool = False,
    ) -> None:
        if not records:
            QMessageBox.information(self, APP_TITLE, "No records available for this PDF export.")
            return

        default_path = self.default_export_path(suffix, "pdf")
        path_text, _ = QFileDialog.getSaveFileName(
            self,
            dialog_title,
            str(default_path),
            "PDF Files (*.pdf)",
        )
        if not path_text:
            return

        try:
            export_doctor_pdf(
                Path(path_text),
                records,
                self.store.results_path,
                self.store.ranges_by_test,
                date_groups=date_groups,
                report_title_suffix=report_title_suffix,
                repeat_all_tests_for_each_group=repeat_all_tests_for_each_group,
            )
            QMessageBox.information(self, APP_TITLE, f"Exported PDF:\n{path_text}")
        except Exception as exc:
            try:
                html_path = Path(path_text).with_suffix(".html")
                # HTML fallback does not support split-chunk rendering, but still gives a readable report.
                html_path.write_text(
                    build_doctor_html(records, self.store.results_path, self.store.ranges_by_test),
                    encoding="utf-8"
                )
                QMessageBox.warning(
                    self,
                    APP_TITLE,
                    f"PDF export failed:\n\n{exc}\n\n"
                    f"Saved HTML fallback instead:\n{html_path}"
                )
            except Exception as html_exc:
                QMessageBox.critical(
                    self,
                    APP_TITLE,
                    f"PDF export failed:\n\n{exc}\n\n"
                    f"HTML fallback also failed:\n\n{html_exc}"
                )

    def export_doctor_pdf_all_clicked(self) -> None:
        if not self.ensure_export_ready():
            return
        self.export_pdf_with_options(
            suffix="doctor_report_all_dates",
            dialog_title="Export PDF - All Dates",
            records=self.store.records,
            report_title_suffix="All Dates",
        )

    def export_doctor_pdf_last6_clicked(self) -> None:
        if not self.ensure_export_ready():
            return

        dates = last_n_dates(self.store.records, 6)
        if not dates:
            QMessageBox.information(self, APP_TITLE, "No dates found.")
            return

        records = filter_records_by_dates(self.store.records, dates)
        self.export_pdf_with_options(
            suffix="doctor_report_last_6_dates",
            dialog_title="Export PDF - Last 6 Dates",
            records=records,
            report_title_suffix=f"Last 6 Dates: {dates[0]} to {dates[-1]}",
        )

    def export_doctor_pdf_custom_range_clicked(self) -> None:
        if not self.ensure_export_ready():
            return

        all_dates = sorted({stringify(r.get("date")) for r in self.store.records if stringify(r.get("date"))}, key=sort_key_for_date)
        if not all_dates:
            QMessageBox.information(self, APP_TITLE, "No dates found.")
            return

        default_text = f"{all_dates[0]} to {all_dates[-1]}"
        text, ok = QInputDialog.getText(
            self,
            "Custom PDF date range",
            "Enter date range as:\nYYYY-MM-DD to YYYY-MM-DD",
            text=default_text,
        )
        if not ok:
            return

        parts = re.split(r"\s*(?:to|,|;|\.\.|–|-{2,})\s*", text.strip())
        # If the simple splitter accidentally split YYYY-MM-DD, fall back to regex date extraction.
        dates_found = re.findall(r"\d{4}-\d{2}-\d{2}", text)

        if len(dates_found) >= 2:
            start_date, end_date = dates_found[0], dates_found[1]
        elif len(parts) >= 2:
            start_date, end_date = parts[0].strip(), parts[1].strip()
        else:
            QMessageBox.warning(self, APP_TITLE, "Could not understand the date range. Use: YYYY-MM-DD to YYYY-MM-DD")
            return

        if not is_valid_date(start_date) or not is_valid_date(end_date):
            QMessageBox.warning(self, APP_TITLE, "Invalid date format. Use YYYY-MM-DD.")
            return

        if start_date > end_date:
            start_date, end_date = end_date, start_date

        records = filter_records_by_date_range(self.store.records, start_date, end_date)
        if not records:
            QMessageBox.information(self, APP_TITLE, f"No records found between {start_date} and {end_date}.")
            return

        self.export_pdf_with_options(
            suffix=f"doctor_report_{start_date}_to_{end_date}",
            dialog_title="Export PDF - Custom Date Range",
            records=records,
            report_title_suffix=f"Custom Range: {start_date} to {end_date}",
        )

    def export_doctor_pdf_split_chunks_clicked(self) -> None:
        if not self.ensure_export_ready():
            return

        all_dates = sorted({stringify(r.get("date")) for r in self.store.records if stringify(r.get("date"))}, key=sort_key_for_date)
        if not all_dates:
            QMessageBox.information(self, APP_TITLE, "No dates found.")
            return

        chunk_size, ok = QInputDialog.getInt(
            self,
            "Split PDF into date chunks",
            "How many date columns per table group?",
            value=6,
            min=1,
            max=12,
            step=1,
        )
        if not ok:
            return

        groups = chunk_dates(all_dates, chunk_size)
        self.export_pdf_with_options(
            suffix=f"doctor_report_split_{chunk_size}_dates_per_group",
            dialog_title="Export PDF - Split Date Chunks",
            records=self.store.records,
            report_title_suffix=f"Split Date Chunks ({chunk_size} dates per group)",
            date_groups=groups,
            repeat_all_tests_for_each_group=True,
        )


    def closeEvent(self, event) -> None:
        if self.ask_unsaved("close the app"):
            event.accept()
        else:
            event.ignore()


# -----------------------------
# Tray launcher (system-tray controller for the PWA + GUI)
# -----------------------------
PWA_LAN_URL = "http://localhost:20000"
RUN_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_REG_VALUE_NAME = "LabResultsArchive"


def _pwa_browser_url() -> str:
    """Return the URL the tray's 'Open PWA in browser' menu item opens.
    Prefers APP_PUBLIC_URL from the environment (set this in
    DATA/LabResultsArchive/.env when you have a Cloudflare tunnel pointed
    at the local server); falls back to the LAN URL for default LAN-only use."""
    return os.environ.get("APP_PUBLIC_URL", "").strip() or PWA_LAN_URL


def _ensure_std_streams() -> None:
    """A frozen windowed (--noconsole) build has sys.stdout/sys.stderr set to
    None; any print() then raises and crashes the app (the bundled PWA code
    prints freely). Route them to a log file when that happens."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        log_path = get_app_dir() / DATA_DIR_NAME / "LabResultsArchive" / "Jobs" / "lab_results_archive.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(log_path, "a", encoding="utf-8", errors="replace")
    except Exception:
        import io
        handle = io.StringIO()
    if sys.stdout is None:
        sys.stdout = handle
    if sys.stderr is None:
        sys.stderr = handle


def autostart_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    py = Path(sys.executable)
    pyw = py.with_name("pythonw.exe")
    launcher = pyw if pyw.exists() else py
    return f'"{launcher}" "{Path(__file__).resolve()}"'


def autostart_is_enabled() -> bool:
    if not sys.platform.startswith("win"):
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_REG_PATH) as key:
            winreg.QueryValueEx(key, RUN_REG_VALUE_NAME)
        return True
    except OSError:
        return False


def autostart_enable() -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_REG_PATH, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, RUN_REG_VALUE_NAME, 0, winreg.REG_SZ, autostart_command())


def autostart_disable() -> None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_REG_PATH, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, RUN_REG_VALUE_NAME)
    except OSError:
        pass


class LabResultsArchiveTray(QSystemTrayIcon):
    def __init__(self, app: QApplication) -> None:
        super().__init__()
        self.app = app
        self.window: Optional[MainWindow] = None
        self.pwa_server = None              # werkzeug server hosting the PWA
        self.pwa_thread: Optional[threading.Thread] = None

        icon_path = get_resource_path(APP_ICON_FILE)
        if icon_path.exists():
            self.setIcon(QIcon(str(icon_path)))
        self.setToolTip(f"{APP_TITLE} V{APP_VERSION}")

        menu = QMenu()
        self.action_open = menu.addAction("Open GUI")
        self.action_open.triggered.connect(self.open_gui)
        self.action_browser = menu.addAction("Open phone PWA in browser")
        self.action_browser.triggered.connect(lambda: webbrowser.open(_pwa_browser_url()))
        menu.addSeparator()
        self.action_pwa_start = menu.addAction("Start PWA")
        self.action_pwa_start.triggered.connect(self.start_pwa)
        self.action_pwa_stop = menu.addAction("Stop PWA")
        self.action_pwa_stop.triggered.connect(self.stop_pwa)
        menu.addSeparator()
        self.action_autostart = menu.addAction("Run at Windows startup")
        self.action_autostart.setCheckable(True)
        self.action_autostart.setChecked(autostart_is_enabled())
        self.action_autostart.toggled.connect(self.toggle_autostart)
        menu.addSeparator()
        self.action_quit = menu.addAction("Exit")
        self.action_quit.triggered.connect(self.quit_app)
        self.setContextMenu(menu)
        self._menu = menu  # keep a ref so it doesn't get GC'd

        self.activated.connect(self.on_activated)
        self.refresh_pwa_state()

    def on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.open_gui()

    def open_gui(self) -> None:
        if self.window is None:
            self.window = MainWindow()
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def start_pwa(self) -> None:
        """Host the Flask PWA in-process on a background thread. The PWA code
        lives in app.py and is imported here - no subprocess, no second
        console window, and one .exe bundles everything."""
        if self.pwa_thread is not None and self.pwa_thread.is_alive():
            return
        try:
            import app as pwa
            self.pwa_server = pwa.start_pwa_server()
        except Exception as exc:
            self.pwa_server = None
            self.showMessage(
                APP_TITLE,
                f"Failed to start PWA:\n{exc}",
                QSystemTrayIcon.MessageIcon.Warning,
            )
            self.refresh_pwa_state()
            return
        self.pwa_thread = threading.Thread(
            target=self.pwa_server.serve_forever, name="pwa-server", daemon=True
        )
        self.pwa_thread.start()
        self.refresh_pwa_state()

    def stop_pwa(self) -> None:
        if self.pwa_server is not None:
            try:
                import app as pwa
                pwa.stop_pwa_server(self.pwa_server)
            except Exception:
                pass
        if self.pwa_thread is not None:
            self.pwa_thread.join(timeout=5)
        self.pwa_server = None
        self.pwa_thread = None
        self.refresh_pwa_state()

    def refresh_pwa_state(self) -> None:
        running = bool(self.pwa_thread is not None and self.pwa_thread.is_alive())
        self.action_pwa_start.setEnabled(not running)
        self.action_pwa_stop.setEnabled(running)

    def toggle_autostart(self, checked: bool) -> None:
        try:
            if checked:
                autostart_enable()
            else:
                autostart_disable()
        except OSError as exc:
            self.showMessage(
                APP_TITLE,
                f"Could not update startup setting:\n{exc}",
                QSystemTrayIcon.MessageIcon.Warning,
            )
            self.action_autostart.blockSignals(True)
            self.action_autostart.setChecked(autostart_is_enabled())
            self.action_autostart.blockSignals(False)

    def quit_app(self) -> None:
        if self.window is not None and self.window.isVisible():
            if not self.window.close():
                return
        self.stop_pwa()
        self.hide()
        self.app.quit()


def main() -> None:
    _ensure_std_streams()
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    apply_app_icon(app)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        window = MainWindow()
        window.show()
        sys.exit(app.exec())
        return

    tray = LabResultsArchiveTray(app)
    tray.show()
    app.aboutToQuit.connect(tray.stop_pwa)
    tray.start_pwa()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
