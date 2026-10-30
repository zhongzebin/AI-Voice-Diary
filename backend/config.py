"""配置管理 - 读取/保存API密钥和数据路径"""
import json
import os
from pathlib import Path
from typing import Optional

from . import platform_utils as plat


def get_config_dir() -> Path:
    """配置目录(可用 AI_DIARY_CONFIG_DIR 覆盖, 测试用)"""
    override = os.environ.get("AI_DIARY_CONFIG_DIR")
    config_dir = Path(override) if override else plat.config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def get_config_file() -> Path:
    return get_config_dir() / "config.json"


def load_config() -> dict:
    config_file = get_config_file()
    if not config_file.exists():
        return {}
    try:
        with open(config_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(config: dict) -> None:
    config_file = get_config_file()
    tmp = config_file.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    os.replace(tmp, config_file)


def get_data_path() -> Optional[str]:
    return load_config().get("data_path")


def get_default_data_path() -> str:
    return str(Path.home() / "Documents" / plat.APP_NAME)


def get_api_keys() -> tuple[Optional[str], Optional[str]]:
    """(dashscope_key, deepseek_key)"""
    config = load_config()
    dashscope_key = plat.load_secret(config.get("dashscope_key_enc", ""))
    deepseek_key = plat.load_secret(config.get("deepseek_key_enc", ""))
    return dashscope_key or None, deepseek_key or None


def update_config(data_path: str, dashscope_key: Optional[str] = None, deepseek_key: Optional[str] = None) -> None:
    """保存配置; key 为空时保留原值"""
    config = load_config()
    config["data_path"] = data_path
    if dashscope_key and dashscope_key.strip():
        config["dashscope_key_enc"] = plat.store_secret("dashscope_key", dashscope_key.strip())
    if deepseek_key and deepseek_key.strip():
        config["deepseek_key_enc"] = plat.store_secret("deepseek_key", deepseek_key.strip())
    save_config(config)


def is_configured() -> bool:
    config = load_config()
    return bool(config.get("data_path") and config.get("dashscope_key_enc") and config.get("deepseek_key_enc"))
