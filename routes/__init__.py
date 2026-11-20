# routes/__init__.py
from routes.decorators import login_required, role_required, api_key_required
from .team_management import team_bp
from .extAccounts import ext_accounts_bp
from .scores import scores_bp
from .quotient import quotient_bp
from .userAPIKeys import user_api_keys_bp
from .api import api_bp
from .redConfig import red_config_bp
from .orangeConfig import orange_config_bp
from .redAttack import red_attack_bp
from .dropFlag import drop_flag_bp
from .scorecard import scorecard_bp
from .incidentResponse import ir_bp


__all__ = ['login_required', 'role_required', 'team_bp', 'ext_accounts_bp', 'scores_bp', 'quotient_bp', 'user_api_keys_bp', 'api_bp', 'red_config_bp', 'orange_config_bp', 'red_attack_bp', 'drop_flag_bp', 'scorecard_bp', 'ir_bp']