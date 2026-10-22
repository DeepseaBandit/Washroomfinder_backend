import csv
import io
import os
import re
from datetime import datetime, timezone

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
db = MongoClient(os.environ["MONGO_URL"])["toilets"]

CSV_FILE = "toilets.csv"

# Columns, in the order they appear in the sheet:
# ID, Name, Type, Latitude, Longitude, Fee, Hours, Open to public?, Accessible,
# Water, Soap, Separate for Men?, Separate for Women?, Cleanliness(1-5),
# Photo link, Date checked, Reviews, Address, Phone


def clean(v):
    return str(v or "").replace("\xa0", " ").strip()


def yes(v):
    return clean(v).lower() in ("yes", "y", "true", "1")


def to_int(v):
    v = clean(v)
    return int(float(v)) if v else None


def to_fee(v):
    """'Free', '' -> 0.   'Rs 5', '5' -> 5."""
    m = re.search(r"\d+", clean(v))
    return int(m.group()) if m else 0


def to_date(v):
    v = clean(v)
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y", "%d-%b-%Y"):
        try:
            return datetime.strptime(v, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def to_photos(v):
    """Links may be separated by commas, spaces or new lines.
    The app expects them comma separated."""
    return ",".join(x for x in re.split(r"[,\s]+", clean(v)) if x)


def parse_reviews(cell):
    """'5 | Great\\n3 | Okay' -> [{rating: 5.0, text: 'Great'}, ...]
    Reviews are split by a new line or '##'."""
    out = []
    for part in re.split(r"\n|##", str(cell or "").replace("\xa0", " ")):
        part = part.strip()
        if not part:
            continue
        rating, text = None, part
        if "|" in part:
            left, right = part.split("|", 1)
            try:
                rating = max(0.0, min(5.0, float(left.strip())))
                text = right
            except ValueError:
                rating, text = None, part
        out.append({"rating": rating, "text": text.strip()})
    return out


def read_text(path):
    """UTF-8 first, then Windows-1252 (Excel's old CSV format)."""
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise SystemExit("Could not decode the file. Re-save it as CSV UTF-8.")


text = read_text(CSV_FILE)
delimiter = "\t" if "\t" in text.split("\n", 1)[0] else ","

imported, skipped = 0, 0
for row in csv.DictReader(io.StringIO(text, newline=""), delimiter=delimiter):
    try:
        name = clean(row["Name"])
        lat = float(row["Latitude"])
        lng = float(row["Longitude"])
        if not name:
            raise ValueError("empty name")
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise ValueError("latitude/longitude out of range")
        doc = {
            "survey_id": clean(row["ID"]),
            "name": name,
            "type": clean(row["Type"]).lower() or "other",
            "location": {"type": "Point", "coordinates": [lng, lat]},  # lng first
            "fee": to_fee(row["Fee"]),
            "hours": clean(row["Hours"]),
            "open_to_public": clean(row["Open to public?"]).lower() or "yes",
            "accessible": yes(row["Accessible"]),
            "water": yes(row["Water"]),
            "soap": yes(row["Soap"]),
            "men_separate": yes(row["Separate for Men?"]),
            "women_separate": yes(row["Separate for Women?"]),
            "cleanliness": to_int(row["Cleanliness(1-5)"]),
            "photo_url": to_photos(row["Photo link"]),
            "last_checked_at": to_date(row["Date checked"]),
            "reviews": parse_reviews(row["Reviews"]),
            "address": clean(row["Address"]),
            "phone": clean(row["Phone"]),
        }
        # upsert by survey ID, so re-running never creates duplicates
        db.places.update_one(
            {"survey_id": doc["survey_id"]}, {"$set": doc}, upsert=True
        )
        imported += 1
    except Exception as e:
        skipped += 1
        print(f"Skipped row {row.get('ID')}: {e}")

print(f"Imported {imported}, skipped {skipped}")