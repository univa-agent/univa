import os
import sys
import toml
import json

def get_default_config():
    """Get default configuration"""
    # Get project root directory (assuming config.py is in univa/config/)
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        
    return {
        # Auth configuration
        "auth_config_file": os.path.join(project_root, "univa/config/auth_config.json"),
        "auth_enabled": True,
        "admin_access_code": "",  # Will be generated on first init
        
        # Session configuration
        "session_timeout_minutes": 60,
        "max_sessions_per_user": 10,
        
        # Agent configuration paths
        "mcp_servers_config": os.path.join(project_root, "univa/config/mcp_configs.json"),
        "prompt_dir": os.path.join(project_root, "univa/prompts"),
        "mcp_tools_path": os.path.join(project_root, "univa"),  # Default UniVideo path
        
        # Model configuration for Plan Agent
        "plan_model_provider": "openai",
        "plan_model_id": "gpt-5-2025-08-07",
        "plan_model_api_key": "",
        "plan_model_base_url": "",
        "plan_model_extra_params": "",
        
        # Model configuration for Act Agent
        "act_model_provider": "openai",
        "act_model_id": "gpt-5-2025-08-07",
        "act_model_api_key": "",
        "act_model_base_url": "",
        "act_model_extra_params": "",
        
        # Other settings
        "proxy_host": "",
        "proxy_port": ""
    }

def load_config():
    """Load configuration from TOML file or create with defaults if it doesn't exist"""
    app_name = "univa"
    # toml config file - use Preferences dir on macOS
    if sys.platform == "darwin":
        CONFIG_FILE = os.path.expanduser(f"~/Library/Preferences/{app_name}/config.toml")
    else:
        CONFIG_FILE = os.path.expanduser(f"~/.config/{app_name}/config.toml")
    # ensure the directory exists
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)

    # Create default config if file doesn't exist
    if not os.path.exists(CONFIG_FILE):
        config = get_default_config()
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            toml.dump(config, f)
    else:
        # read existing config file
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = toml.load(f)
            # Merge with defaults to ensure all required fields exist
            defaults = get_default_config()
            for key, value in defaults.items():
                if key not in config:
                    config[key] = value

    # Set up data files
    for file_key in ["auth_config_file"]:
        config[file_key] = os.path.expanduser(config[file_key])
        os.makedirs(os.path.dirname(config[file_key]), exist_ok=True)

    # Set up proxy settings if configured
    PROXY_HOST = config.get("proxy_host")
    PROXY_PORT = config.get("proxy_port")
    if PROXY_HOST and PROXY_PORT:
        os.environ["http_proxy"] = f"http://{PROXY_HOST}:{PROXY_PORT}"
        os.environ["https_proxy"] = f"http://{PROXY_HOST}:{PROXY_PORT}"

    # Load model configuration from .env if present (project root .env)
    try:
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        univa_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env_candidates = [
            os.path.join(univa_root, ".env"),
            os.path.join(project_root, ".env"),
        ]
        env_path = next((p for p in env_candidates if os.path.exists(p)), None)
        if env_path:
            env_vars = {}
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        env_vars[k.strip()] = v.strip().strip('"').strip("'")

            # Plan model
            config["plan_model_provider"] = env_vars.get("PLAN_MODEL_PROVIDER", config.get("plan_model_provider"))
            config["plan_model_id"] = env_vars.get("PLAN_MODEL_ID", config.get("plan_model_id"))
            config["plan_model_api_key"] = env_vars.get("PLAN_MODEL_API_KEY", config.get("plan_model_api_key"))
            config["plan_model_base_url"] = env_vars.get("PLAN_MODEL_BASE_URL", config.get("plan_model_base_url"))
            plan_extra = env_vars.get("PLAN_MODEL_EXTRA_PARAMS", config.get("plan_model_extra_params", ""))
            config["plan_model_extra_params"] = plan_extra

            # Act model
            config["act_model_provider"] = env_vars.get("ACT_MODEL_PROVIDER", config.get("act_model_provider"))
            config["act_model_id"] = env_vars.get("ACT_MODEL_ID", config.get("act_model_id"))
            config["act_model_api_key"] = env_vars.get("ACT_MODEL_API_KEY", config.get("act_model_api_key"))
            config["act_model_base_url"] = env_vars.get("ACT_MODEL_BASE_URL", config.get("act_model_base_url"))
            act_extra = env_vars.get("ACT_MODEL_EXTRA_PARAMS", config.get("act_model_extra_params", ""))
            config["act_model_extra_params"] = act_extra
    except Exception:
        pass

    return CONFIG_FILE, config

CONFIG_FILE, config = load_config()

# Initialize auth service
from auth.auth_service import AuthService
auth_service = AuthService(config['auth_config_file'])

