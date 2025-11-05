
from __future__ import annotations
import io
import re
import time
import uuid
import json
import threading
from datetime import datetime
from typing import Dict, List, Any

import pandas as pd
import requests
from flask import Flask, request, jsonify, send_file

app = Flask(__name__)

JOBS: Dict[str, Dict[str, Any]] = {}

PRESETS = {
    "basic": ["scene_number", "location", "time_of_day", "characters"],
    "extended": [
        "scene_number",
        "location",
        "time_of_day",
        "characters",
        "extras",
        "props",
    ],
    "full": [
        "scene_number",
        "slug",
        "location",
        "time_of_day",
        "characters",
        "secondary_characters",
        "extras",
        "props",
        "effects",
        "notes",
    ],
}

TIME_OF_DAY_KW = {
    "день": ["ДЕНЬ", "ДНЕМ", "ДНЁМ"],
    "ночь": ["НОЧЬ", "НОЧЬЮ"],
    "утро": ["УТРО", "УТРОМ"],
    "вечер": ["ВЕЧЕР", "ВЕЧЕРОМ"],
    "инт": ["ИНТ.", "ИНТ", "ИНТЕРЬЕР"],
    "экст": ["ЭКСТ.", "ЭКСТ", "ЭКСТЕРЬЕР"],
}

LOCATION_KW = [
    "улица",
    "дом",
    "квартира",
    "подъезд",
    "кабинет",
    "офис",
    "кафе",
    "ресторан",
    "бар",
    "набережная",
    "парк",
    "гараж",
    "больница",
    "школа",
    "коридор",
    "площадь",
    "лес",
    "крыша",
]

EXTRAS_KW = [
    "массовка",
    "толпа",
    "прохожие",
    "официанты",
    "охранники",
    "студенты",
    "болельщики",
]

PROPS_KW = [
    "автомобиль",
    "машина",
    "пистолет",
    "нож",
    "телефон",
    "компьютер",
    "чемодан",
    "животное",
    "велосипед",
    "зонт",
    "гитара",
]

EFFECTS_KW = [
    "взрыв",
    "дым",
    "огонь",
    "дождь",
    "кровь",
    "каскадёр",
    "трюк",
    "спецэффект",
    "грима",
    "грим",
]

# -----------------------------
# Utilities
# -----------------------------

def now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def pick_time_of_day(block_text: str) -> str:
    upper = block_text.upper()
    for label, variants in TIME_OF_DAY_KW.items():
        if any(v in upper for v in variants):
            return label
    # fallback
    return "день" if "СОЛНЦЕ" in upper else "не указано"


def find_keywords(block_text: str, keywords: List[str]) -> List[str]:
    found = []
    low = block_text.lower()
    for kw in keywords:
        if re.search(r"\\b" + re.escape(kw) + r"(\\b|и|ов|ам|ами|ах)", low):
            found.append(kw)
    return sorted(list(set(found)))


def extract_characters(block_text: str) -> Dict[str, List[str]]:
    """
    Very naive: treat ALL-CAPS Cyrillic tokens at line starts as character cues,
    and also collect capitalized names inside dialogue lines.
    Returns dict with main and secondary lists.
    """
    mains: List[str] = []
    secondary: List[str] = []

    lines = [l.strip() for l in block_text.splitlines() if l.strip()]
    for line in lines:
        # Speaker cue like: ИВАН: ...  or МАРИЯ — ...
        m = re.match(r"^([А-ЯЁ][А-ЯЁ\-\s]{1,30})(:|—|-)\s", line)
        if m and line.isupper():
            name = m.group(1).strip().replace("  ", " ")
            if name and name not in mains:
                mains.append(name)
        # Capitalized names in narrative
        for name in re.findall(r"\b[А-ЯЁ][а-яё]+\b", line):
            if name.isupper():
                continue
            if name not in secondary and name not in mains:
                secondary.append(name)

    return {
        "main": mains[:10],
        "secondary": secondary[:15],
    }


def extract_slug(block_text: str) -> str:
    """Try to capture a slug like: СЦЕНА 12. ИНТ./НОЧЬ. КВАРТИРА — КУХНЯ"""
    first_line = next((l.strip() for l in block_text.splitlines() if l.strip()), "")
    return first_line[:180]


def analyze_text_stub(text: str, preset: str, custom_columns: Dict[str, List[str]] | None = None) -> Dict[str, Any]:
    """
    Stub for the model: segment by scene headers and extract simple features by keywords.
    custom_columns: {"транспорт": ["машина", "автобус"], ...}
    """
    # Scene segmentation by headers like "СЦЕНА 1" or "Сцена 12:"
    pattern = re.compile(r"(?im)^(сцена)\s*(\d+)\s*([\.:\-])?")
    matches = list(pattern.finditer(text))

    scenes: List[Dict[str, Any]] = []
    for idx, m in enumerate(matches):
        start = m.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        block = text[start:end].strip()

        scene_number = int(m.group(2))
        slug = extract_slug(block)
        tod = pick_time_of_day(block)
        locs = find_keywords(block, LOCATION_KW)
        chars = extract_characters(block)
        extras = find_keywords(block, EXTRAS_KW)
        props = find_keywords(block, PROPS_KW)
        effects = find_keywords(block, EFFECTS_KW)

        scene_row: Dict[str, Any] = {
            "scene_number": scene_number,
            "slug": slug,
            "location": ", ".join(locs) if locs else "не распознано",
            "time_of_day": tod,
            "characters": ", ".join(chars["main"]) if chars["main"] else "—",
            "secondary_characters": ", ".join(chars["secondary"]) if chars["secondary"] else "—",
            "extras": ", ".join(extras) if extras else "—",
            "props": ", ".join(props) if props else "—",
            "effects": ", ".join(effects) if effects else "—",
            "notes": "(stub) автоматический разбор по ключевым словам",
        }

        # Apply custom columns
        if custom_columns:
            for col, kws in custom_columns.items():
                scene_row[col] = ", ".join(find_keywords(block, kws)) or "—"

        scenes.append(scene_row)

    if not scenes:
        block = text.strip()
        chars = extract_characters(block)
        scenes.append({
            "scene_number": 1,
            "slug": extract_slug(block),
            "location": ", ".join(find_keywords(block, LOCATION_KW)) or "не распознано",
            "time_of_day": pick_time_of_day(block),
            "characters": ", ".join(chars["main"]) or "—",
            "secondary_characters": ", ".join(chars["secondary"]) or "—",
            "extras": ", ".join(find_keywords(block, EXTRAS_KW)) or "—",
            "props": ", ".join(find_keywords(block, PROPS_KW)) or "—",
            "effects": ", ".join(find_keywords(block, EFFECTS_KW)) or "—",
            "notes": "(stub) один блок без заголовков",
        })

    preset = (preset or "full").lower()
    columns = PRESETS.get(preset, PRESETS["full"]).copy()
    if custom_columns:
        for col in custom_columns.keys():
            if col not in columns:
                columns.append(col)

    return {
        "columns": columns,
        "scenes": sorted(scenes, key=lambda r: r.get("scene_number", 0)),
    }

def _deliver_webhook(callback_url: str, payload: Dict[str, Any]) -> None:
    try:
        requests.post(callback_url, json=payload, timeout=8)
    except Exception as e:
        payload.setdefault("delivery_error", str(e))


def process_job(job_id: str) -> None:
    job = JOBS.get(job_id)
    if not job:
        return
    job["status"] = "processing"
    job["updated_at"] = now_iso()

    time.sleep(0.5)

    try:
        data = job["input"]
        text = data["text"]
        preset = data.get("preset", "full")
        custom_columns = data.get("custom_columns")

        result = analyze_text_stub(text, preset, custom_columns)
        job["result"] = result
        job["status"] = "done"
        job["updated_at"] = now_iso()

        if data.get("callback_url"):
            payload = {
                "job_id": job_id,
                "status": job["status"],
                "columns": result["columns"],
                "scenes": result["scenes"],
            }
            threading.Thread(target=_deliver_webhook, args=(data["callback_url"], payload), daemon=True).start()

    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        job["updated_at"] = now_iso()


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "ts": now_iso()})


@app.route("/v1/jobs", methods=["POST"])
def create_job():
    try:
        payload = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "invalid_json"}), 400

    text = (payload or {}).get("text", "").strip()
    if not text:
        return jsonify({"error": "field 'text' is required"}), 400

    preset = (payload.get("preset") or "full").lower()
    if preset not in PRESETS:
        return jsonify({"error": "invalid preset", "allowed": list(PRESETS.keys())}), 400

    custom_columns = payload.get("custom_columns")
    if custom_columns is not None and not isinstance(custom_columns, dict):
        return jsonify({"error": "custom_columns must be an object of {column: [keywords]}"}), 400

    job_id = uuid.uuid4().hex
    JOBS[job_id] = {
        "id": job_id,
        "status": "queued",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "input": {
            "text": text,
            "preset": preset,
            "custom_columns": custom_columns,
            "callback_url": payload.get("callback_url"),
        },
    }

    t = threading.Thread(target=process_job, args=(job_id,), daemon=True)
    t.start()

    return jsonify({"job_id": job_id, "status": "queued"}), 202


@app.route("/v1/jobs/<job_id>", methods=["GET"])
def get_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "not_found"}), 404

    response = {
        "job_id": job["id"],
        "status": job["status"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
    }
    if job["status"] == "done":
        response.update({
            "columns": job["result"]["columns"],
            "scenes": job["result"]["scenes"],
        })
    if job.get("error"):
        response["error"] = job["error"]

    return jsonify(response), 200


@app.route("/v1/jobs/<job_id>/export", methods=["GET"])
def export_job(job_id: str):
    job = JOBS.get(job_id)
    if not job or job.get("status") != "done":
        return jsonify({"error": "not_found_or_not_ready"}), 404

    fmt = (request.args.get("format") or "csv").lower()
    if fmt not in ("csv", "xlsx"):
        return jsonify({"error": "unsupported_format", "allowed": ["csv", "xlsx"]}), 400

    columns = job["result"]["columns"]
    rows = job["result"]["scenes"]

    norm_rows = []
    for r in rows:
        norm = {col: r.get(col, "—") for col in columns}
        norm_rows.append(norm)
    df = pd.DataFrame(norm_rows, columns=columns)

    if fmt == "csv":
        buf = io.StringIO()
        df.to_csv(buf, index=False)
        data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
        return send_file(
            data,
            mimetype="text/csv; charset=utf-8",
            as_attachment=True,
            download_name=f"preproduction_{job_id}.csv",
        )

    xbuf = io.BytesIO()
    with pd.ExcelWriter(xbuf, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Scenes", index=False)
    xbuf.seek(0)
    return send_file(
        xbuf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=f"preproduction_{job_id}.xlsx",
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)

