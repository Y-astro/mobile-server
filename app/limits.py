"""In-flight work admission for dashboard uploads and conversions."""
import threading


class Admission:
    def __init__(self):
        self.slots = {
            '/api/laptop/terminals': threading.BoundedSemaphore(1),
            '/api/agy/scroll': threading.BoundedSemaphore(1),
            '/api/login': threading.BoundedSemaphore(4),
            '/api/clipboard/upload': threading.BoundedSemaphore(2),
            '/api/convert': threading.BoundedSemaphore(1),
        }

    def acquire(self, path):
        slot = self.slots.get(path)
        return slot, slot is None or slot.acquire(blocking=False)


BODY_LIMITS = {
    '/api/laptop/terminals': 1024,
    '/api/agy/scroll': 1024,
    '/api/login': 8*1024,
    '/api/logout': 1024,
    '/api/services/toggle': 4096,
    '/api/vpn/toggle': 4096,
    '/api/clipboard/upload': 25*1024*1024,
    '/api/convert': 32*1024*1024,
}
