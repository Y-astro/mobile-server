import json
import os
import threading


class NotesStore:
    def __init__(self, path, *, lock=None):
        self.path = os.path.expanduser(str(path))
        self.lock = lock if lock is not None else threading.Lock()

    def get(self, username):
        if not username:
            return ""
        with self.lock:
            if not os.path.exists(self.path):
                return ""
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return str(data.get(username, ""))
            except Exception as e:
                print(f"Error reading notes for {username}: {e}")
                return ""

    def set(self, username, content):
        if not username:
            return False
        with self.lock:
            dir_name = os.path.dirname(self.path)
            if dir_name:
                os.makedirs(dir_name, exist_ok=True)
            data = {}
            if os.path.exists(self.path):
                try:
                    with open(self.path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    data = {}
            data[username] = str(content)
            tmp_file = self.path + ".tmp"
            try:
                with open(tmp_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.chmod(tmp_file, 0o600)
                os.replace(tmp_file, self.path)
                return True
            except Exception as e:
                print(f"Error saving notes for {username}: {e}")
                if os.path.exists(tmp_file):
                    try:
                        os.remove(tmp_file)
                    except Exception:
                        pass
                return False
