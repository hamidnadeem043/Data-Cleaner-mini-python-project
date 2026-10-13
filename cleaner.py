import argparse
import csv
import json
import logging
import re
import statistics
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("cleaner")

FIELDS = ["id", "name", "email", "age", "city", "salary", "join_date"]
REQUIRED = {"name", "email"}

MISSING_VALUES = {"", "n/a", "na", "null", "none", "-", "nan"}

EMAIL_PATTERN = re.compile(r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$")

DATE_FORMATS = [
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%b %d, %Y",
    "%d %b %Y",
]


def setup_logging(log_file="cleaner.log"):
    """Log to both the console and a file."""
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    file_handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger.addHandler(console)


def is_missing(value):
    return value is None or value.strip().lower() in MISSING_VALUES


def clean_name(value):
    """Trim, collapse repeated spaces, Title Case."""
    if is_missing(value):
        return None
    return " ".join(value.split()).title()


def clean_email(value):
    """Trim, lowercase, and make sure it looks like an email."""
    if is_missing(value):
        return None
    email = value.strip().lower()
    return email if EMAIL_PATTERN.match(email) else None


def clean_age(value):
    """Convert to int; ages outside 1-100 are treated as invalid."""
    if is_missing(value):
        return None
    try:
        age = int(float(value.strip()))
    except ValueError:
        return None
    return age if 0 < age <= 100 else None


def clean_city(value):
    if is_missing(value):
        return None
    return " ".join(value.split()).title()


def clean_salary(value):
    """Strip currency symbols and commas: '$50,000' -> 50000."""
    if is_missing(value):
        return None
    digits = re.sub(r"[^\d.]", "", value)
    try:
        salary = float(digits)
    except ValueError:
        return None
    return int(salary) if salary.is_integer() else round(salary, 2)


def clean_date(value):
    """Try several formats and return an ISO date (YYYY-MM-DD)."""
    if is_missing(value):
        return None
    text = " ".join(value.split())
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


CLEANERS = {
    "name": clean_name,
    "email": clean_email,
    "age": clean_age,
    "city": clean_city,
    "salary": clean_salary,
    "join_date": clean_date,
}


def load_csv(path):
    """Read the CSV into a list of dicts, repairing rows with a wrong column count."""
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        try:
            header = [h.strip().lower() for h in next(reader)]
        except StopIteration:
            raise ValueError("The CSV file is empty.")

        missing_cols = REQUIRED - set(header)
        if missing_cols:
            raise ValueError(f"Missing required column(s): {', '.join(sorted(missing_cols))}")

        for line_no, values in enumerate(reader, start=2):
            if len(values) > len(header):
                logger.warning(
                    "Line %d: %d fields instead of %d (unquoted comma?); merging extras into the last column",
                    line_no, len(values), len(header),
                )
                last = len(header) - 1
                values = values[:last] + [",".join(values[last:])]
            elif len(values) < len(header):
                values = values + [""] * (len(header) - len(values))

            row = dict(zip(header, values))
            row["_line"] = line_no
            rows.append(row)
    return rows


def clean_row(row, stats):
    """Clean one row. Returns None if the row has no name and no email."""
    line = row["_line"]
    cleaned = {"id": (row.get("id") or "").strip()}

    for column, cleaner in CLEANERS.items():
        raw = row.get(column, "")
        result = cleaner(raw)
        cleaned[column] = result

        if result is None and not is_missing(raw):
            logger.warning("Line %d: invalid %s %r -> treated as missing", line, column, raw.strip())
            stats[f"invalid_{column}"] += 1
        elif isinstance(result, str) and result != raw:
            stats["values_standardised"] += 1

    if cleaned["name"] is None and cleaned["email"] is None:
        logger.info("Line %d: no name and no email -> row dropped", line)
        return None

    cleaned["_line"] = line
    return cleaned


def remove_duplicates(rows, stats):
    """Keep the first row for each email (or name, if there is no email)."""
    seen = set()
    unique = []
    for row in rows:
        key = row["email"] or (row["name"] or "").lower()
        if key in seen:
            logger.info("Line %d: duplicate of %s -> row dropped", row["_line"], key)
            stats["duplicates_removed"] += 1
            continue
        seen.add(key)
        unique.append(row)
    return unique


def fill_missing(rows, stats):
    """Fill missing numbers with the median and missing cities with 'Unknown'."""
    for column in ("age", "salary"):
        values = [r[column] for r in rows if r[column] is not None]
        if not values:
            continue
        median = statistics.median(values)
        median = int(median) if float(median).is_integer() else round(median, 2)
        for row in rows:
            if row[column] is None:
                logger.info("Line %d: missing %s -> filled with median %s", row["_line"], column, median)
                row[column] = median
                stats[f"filled_{column}"] += 1

    for row in rows:
        if row["city"] is None:
            logger.info("Line %d: missing city -> 'Unknown'", row["_line"])
            row["city"] = "Unknown"
            stats["filled_city"] += 1
    return rows


def save_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ("" if row[k] is None else row[k]) for k in FIELDS})


def save_report(stats, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(dict(stats), f, indent=2)


def clean_file(input_path, output_path):
    """Run the whole pipeline and return the stats."""
    stats = Counter()

    raw_rows = load_csv(input_path)
    stats["rows_read"] = len(raw_rows)
    logger.info("Loaded %d rows from %s", len(raw_rows), input_path)

    cleaned = []
    for row in raw_rows:
        result = clean_row(row, stats)
        if result is None:
            stats["empty_rows_removed"] += 1
        else:
            cleaned.append(result)

    cleaned = remove_duplicates(cleaned, stats)
    cleaned = fill_missing(cleaned, stats)

    stats["rows_written"] = len(cleaned)
    save_csv(cleaned, output_path)
    return stats


def main():
    parser = argparse.ArgumentParser(description="Clean a messy CSV of people records.")
    parser.add_argument("input", nargs="?", default="messy_data.csv",
                        help="path to the messy CSV file (default: messy_data.csv)")
    parser.add_argument("-o", "--output", help="path for the cleaned CSV (default: <input>_cleaned.csv)")
    args = parser.parse_args()

    setup_logging()

    input_path = Path(args.input)
    output_path = Path(args.output) if args.output else input_path.with_name(f"{input_path.stem}_cleaned.csv")
    report_path = output_path.with_name(f"{input_path.stem}_report.json")

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        stats = clean_file(input_path, output_path)
        save_report(stats, report_path)
    except FileNotFoundError:
        logger.error("File not found: %s", input_path)
        return 1
    except PermissionError:
        logger.error("Permission denied while reading or writing files.")
        return 1
    except (ValueError, UnicodeDecodeError) as err:
        logger.error("Could not process the file: %s", err)
        return 1

    logger.info("Done: %d rows read, %d rows written", stats["rows_read"], stats["rows_written"])
    logger.info("Cleaned data: %s", output_path)
    logger.info("Report: %s", report_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
