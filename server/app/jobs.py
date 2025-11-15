import time
import uuid
import requests
import threading
from datetime import datetime, timezone
from typing import Dict

from .logger import log
from .config import Config

JOBS: Dict[str, dict] = {}
LOCK = threading.Lock()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def create_job(text: str, preset: str, custom_columns, chat_id, msg_id):
    job_id = uuid.uuid4().hex
    with LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "status": "queued",
            "input": {
                "text": text,
                "preset": preset,
                "custom_columns": custom_columns,
                "tg_chat_id": chat_id,
                "tg_message_id": msg_id,
            },
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
    log.info(f"Создана задача {job_id}")
    return job_id


def process_job(job_id: str):
    log.info(f"Запуск фоновой задачи {job_id}")

    with LOCK:
        job = JOBS[job_id]

    try:
        # запрос во внешнее приложение
        resp = requests.post(
            f"{Config.PROCESSING_APP_URL}/v1/jobs",
            json=job["input"],
        )
        external_id = resp.json()["job_id"]
        log.info(f"Задача {job_id} → внешняя {external_id}")

        while True:
            time.sleep(5)
            status = requests.get(
                f"{Config.PROCESSING_APP_URL}/v1/jobs/{external_id}"
            ).json()

            if status["status"] == "done":
                export = requests.get(
                    f"{Config.PROCESSING_APP_URL}/v1/jobs/{external_id}/export?format=xlsx"
                )

                with LOCK:
                    job["status"] = "done"
                    job["result"] = export.content
                    job["updated_at"] = now_iso()

                log.info(f"Задача {job_id} завершена")
                break

            if status["status"] == "error":
                raise RuntimeError(status.get("error"))

    except Exception as e:
        log.error(f"Ошибка в задаче {job_id}: {e}")
        with LOCK:
            job["status"] = "error"
            job["error"] = str(e)
            job["updated_at"] = now_iso()


def start_result_monitor(bot):
    def monitor():
        while True:
            time.sleep(5)
            with LOCK:
                finished = [
                    j for j in JOBS.values()
                    if j["status"] == "done" and not j.get("sent")
                ]

            for job in finished:
                chat_id = job["input"]["tg_chat_id"]
                bot.send_document(chat_id, ("result.xlsx", job["result"]))
                bot.send_message(chat_id, "Готово ✔️")

                with LOCK:
                    job["sent"] = True

                log.info(f"Результат отправлен: {job['id']}")

    threading.Thread(target=monitor, daemon=True).start()
