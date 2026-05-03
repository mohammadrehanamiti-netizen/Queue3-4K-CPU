import json
import os
from config import Config

class SettingsManager:
    """Simple per-user JSON storage for encoding settings."""
    STORAGE = os.path.join(Config.DOWNLOAD_DIR, 'user_settings.json')

    @classmethod
    def _load_all(cls):
        if not os.path.exists(cls.STORAGE):
            return {}
        with open(cls.STORAGE, 'r') as f:
            return json.load(f)

    @classmethod
    def _save_all(cls, data):
        with open(cls.STORAGE, 'w') as f:
            json.dump(data, f, indent=2)

    @classmethod
    def get(cls, user_id):
        """Return dict or {}."""
        return cls._load_all().get(str(user_id), {})

    @classmethod
    def set(cls, user_id, key, value):
        all_data = cls._load_all()
        user_data = all_data.setdefault(str(user_id), {})
        user_data[key] = value
        cls._save_all(all_data)
