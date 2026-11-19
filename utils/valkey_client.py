# utils/valkey_client.py
from flask import current_app
import valkey


def get_valkey_client(db=0):
    """Get a Valkey connection from the app config."""
    valkey_host = current_app.config.get('VALKEY_HOST', 'valkey')
    valkey_port = current_app.config.get('VALKEY_PORT', 6379)
    valkey_password = current_app.config.get('VALKEY_PASSWORD') or os.environ.get('VALKEY_PASSWORD')

    return valkey.Valkey(
        host=valkey_host,
        port=valkey_port,
        db=db,
        password=valkey_password,
        decode_responses=True
    )