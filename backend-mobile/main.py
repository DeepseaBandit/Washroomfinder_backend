import os
import re
import asyncio
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel

load_dotenv()
db = AsyncIOMotorClient(os.environ["MONGO_URL"])["toilets"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.places.create_index([("location", "2dsphere")])
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


class Review(BaseModel):
    rating: float | None = None
    text: str = ""


class PlaceIn(BaseModel):
    name: str
    lat: float
    lng: float
    type: str = "other"
    fee: int = 0
    hours: str = ""
    open_to_public: str = "yes"
    accessible: bool = False
    water: bool = False
    soap: bool = False
    men_separate: bool = False
    women_separate: bool = False
    cleanliness: float | None = None
    photo_url: str = ""
    address: str = ""
    phone: str = ""
    reviews: list[Review] = []
    notes: str = ""


REVIEW_RE = re.compile(
    r"(?:^|(?<=\|)|(?<=\n))\s*(\d(?:\.\d+)?)\s*/\s*5\s*(?:\[([^\]]*)\])?\s*:\s*"
)


def parse_reviews(cell):
    cell = str(cell or "").replace("\xa0", " ").strip()
    if not cell:
        return []

    matches = list(REVIEW_RE.finditer(cell))
    out = []

    if matches:
        for i, m in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(cell)
            text = re.sub(r"\s*\|\s*$", "", cell[m.end():end].strip()).strip()
            out.append({
                "rating": max(0.0, min(5.0, float(m.group(1)))),
                "date": (m.group(2) or "").strip() or None,
                "text": text,
            })
        out.sort(key=lambda r: r["date"] or "", reverse=True)  # newest first
        return out

    # Fallback: older "5 | text" format, one per line or split by ##
    for part in re.split(r"\r?\n|##", cell):
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
                pass
        out.append({"rating": rating, "date": None, "text": text.strip()})
    return out

@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/places")
async def add_place(p: PlaceIn):
    doc = p.model_dump(exclude={"lat", "lng"})
    doc["location"] = {"type": "Point", "coordinates": [p.lng, p.lat]}
    doc["last_checked_at"] = datetime.now(timezone.utc)
    res = await db.places.insert_one(doc)
    return {"id": str(res.inserted_id)}


@app.get("/places/nearby")
async def nearby(lat: float, lng: float, radius: int = 3000, limit: int = 1000):
    cursor = db.places.find({"location": {"$near": {
        "$geometry": {"type": "Point", "coordinates": [lng, lat]},
        "$maxDistance": radius}}}).limit(limit)
    out = []
    async for d in cursor:
        d["id"] = str(d.pop("_id"))
        d.setdefault("reviews", [])
        out.append(d)
    return out


ROUTE_BASE = "https://routing.openstreetmap.de"
UA = {"User-Agent": "WashroomFinder/0.1 (snehodeep.das@gmail.com)"}
_lock = asyncio.Lock()
_last_call = 0.0
_cache: dict = {}  # key -> (timestamp, result)


async def _osrm(profile: str, a_lat, a_lng, b_lat, b_lng):
    global _last_call
    key = (profile, round(a_lat, 4), round(a_lng, 4), round(b_lat, 4), round(b_lng, 4))
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < 600:  # reuse results for 10 minutes
        return hit[1]

    url = f"{ROUTE_BASE}/routed-{profile}/route/v1/driving/{a_lng},{a_lat};{b_lng},{b_lat}"
    async with _lock:  # the public server allows about 1 request per second
        wait = 1.05 - (time.monotonic() - _last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        async with httpx.AsyncClient(timeout=15, headers=UA) as client:
            r = await client.get(url, params={"overview": "full", "geometries": "geojson"})
        _last_call = time.monotonic()

    if r.status_code != 200:
        raise HTTPException(502, f"Routing service error ({r.status_code})")
    data = r.json()
    if data.get("code") != "Ok" or not data.get("routes"):
        raise HTTPException(404, f"No {profile} route found")

    route = data["routes"][0]
    result = {
        "distance_km": round(route["distance"] / 1000, 2),
        "duration_min": max(1, round(route["duration"] / 60)),
        "geometry": [[lat, lng] for lng, lat in route["geometry"]["coordinates"]],
    }
    _cache[key] = (time.time(), result)
    return result


@app.get("/route")
async def route(from_lat: float, from_lng: float, to_lat: float, to_lng: float):
    foot = await _osrm("foot", from_lat, from_lng, to_lat, to_lng)
    car = await _osrm("car", from_lat, from_lng, to_lat, to_lng)
    return {"foot": foot, "car": car}


@app.get("/route/{profile}")
async def route_leg(profile: str, from_lat: float, from_lng: float, to_lat: float, to_lng: float):
    if profile not in ("foot", "car"):
        raise HTTPException(404, "Unknown profile")
    return await _osrm(profile, from_lat, from_lng, to_lat, to_lng)