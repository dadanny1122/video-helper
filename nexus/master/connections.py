"""追蹤「哪個裝置現在有哪些 TLS 連線」；撤銷／暫停時能立刻關掉（額外要求 2）"""
import socket
import threading
import time


class ConnectionRegistry:
    def __init__(self):
        self.lock = threading.Lock()
        self.by_device = {}           # device_id → {id(sock): (sock, since)}

    def add(self, device_id: str, sock):
        with self.lock:
            self.by_device.setdefault(device_id, {})[id(sock)] = (sock, time.time())

    def remove(self, device_id: str, sock):
        with self.lock:
            d = self.by_device.get(device_id)
            if d:
                d.pop(id(sock), None)
                if not d:
                    self.by_device.pop(device_id, None)

    def close_all(self, device_id: str) -> int:
        with self.lock:
            socks = [s for s, _ in self.by_device.pop(device_id, {}).values()]
        for s in socks:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass
        return len(socks)

    def count(self, device_id: str | None = None) -> int:
        with self.lock:
            if device_id:
                return len(self.by_device.get(device_id, {}))
            return sum(len(d) for d in self.by_device.values())
