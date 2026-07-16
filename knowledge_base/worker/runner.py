import threading

from ..config import settings
from ..indexing import IndexingService
from ..repositories import get_repository


class KnowledgeWorker:
    def __init__(self):
        self.repository = get_repository()
        self.service = IndexingService(self.repository)
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.repository.recover_incomplete_jobs()
        self.stop_event.clear()
        self.thread = threading.Thread(target=self.run, name="jinni-knowledge-worker", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3)

    def run(self):
        while not self.stop_event.is_set():
            job = self.repository.claim_job()
            if not job:
                self.stop_event.wait(settings.worker_poll_seconds)
                continue
            try:
                self.service.process_job(job)
            except Exception as exc:
                if job.get("version_id"):
                    self.repository.fail_version(job["version_id"], str(exc))
                self.repository.update_job(job["id"], "failed", error=str(exc))
