# utils/scoreCalculation.py
"""
Master source of truth for all scoring calculations.

Every consumer — the dashboard, score exports, and future scorecard/report
generation — must derive standings from calculate_team_standings() so all
views of the data agree. Do not reimplement this logic elsewhere.
"""

import json
from collections import Counter

from flask import current_app
from sqlalchemy import func

from models import db, Team, Score
from utils.valkey_client import get_valkey_client

# Canonical category keys used in standings output
SCORE_CATEGORIES = ('blue', 'red', 'white', 'orange', 'green')


def _get_red_scoring_config():
    """Fetch the red team scoring config from Valkey. Returns None on failure."""
    try:
        vk = get_valkey_client()
        raw_scoring = vk.get("vantage:config:scoring:red")
        return json.loads(raw_scoring) if raw_scoring else None
    except Exception as e:
        current_app.logger.error(f"Failed to fetch red scoring config: {e}")
        return None


def _build_red_lookup():
    """Run the red team calculation and build a team_number -> result lookup."""
    red_normalization_stats = {}
    try:
        red_calculations, red_normalization_stats = calculate_red_team_scores(
            _get_red_scoring_config()
        )
        lookup = {calc['team_number']: calc for calc in red_calculations}
    except Exception as e:
        current_app.logger.error(f"Failed to calculate red team scores: {e}")
        lookup = {}
    return lookup, red_normalization_stats


def get_red_calculation_lookup():
    """
    Public accessor for the red team calculation, keyed by team_number.

    Consumers needing red standings or placements (e.g., the scorecard
    feedback page/PDF) must use this rather than reimplementing the math,
    so views of red performance can never drift apart.

    Returns (lookup, red_normalization_stats) where lookup maps
    team_number -> red calculation dict from calculate_red_team_scores().
    """
    return _build_red_lookup()


def get_raw_red_ir_totals(team_id):
    """
    Raw (unweighted) red and incident-response point totals for one team.

    Used for the team-facing '% of red points recovered' feedback metric:
    the percentage is intentionally computed from raw sums so it stays
    independent of any weighting or normalization changes.

    Returns a dict: {'red': int, 'ir': int}
      - 'red': sum of approved scores with category 'red'
      - 'ir':  sum of approved scores with category 'incident-response'
    """
    results = db.session.query(
        Score.category,
        func.sum(Score.points).label('total_points')
    ).filter(
        Score.team_id == team_id,
        Score.category.in_(('red', 'incident-response')),
        Score.is_approved == True
    ).group_by(Score.category).all()

    totals = {'red': 0, 'ir': 0}
    for category, total in results:
        cat = (category or '').lower()
        if cat == 'red':
            totals['red'] += (total or 0)
        elif cat == 'incident-response':
            totals['ir'] += (total or 0)
    return totals


def get_ir_recovery_percent(team_id):
    """
    Percentage of red-team points recovered through incident response.

    Assumes red scores are negative (points conceded) and IR scores are
    positive (points earned back). Computed from RAW sums:
        abs(ir) / abs(red) * 100
    so it is unaffected by attack/incident-response weighting.

    Returns:
        (percent or None, situation)
        situation describes the edge cases for display:
          - 'normal':  percentage is meaningful
          - 'no_red':  nothing conceded but IR points were earned — e.g. the
                       team submitted a quality IR for an attack attempt
                       that was blocked; reward this, do not show a %
          - 'no_ir':   points conceded but nothing recovered
          - 'no_data': no red or IR activity at all
          - 'penalty': team lost additional points on their IR writeup
    """
    totals = get_raw_red_ir_totals(team_id)
    red = totals['red']
    ir = totals['ir']

    if ir == 0 and red == 0:
        return None, 'no_data'
    if ir > 0 and red >= 0:
        # Edge case: nothing conceded, but the team earned IR points anyway
        return None, 'no_red'
    if ir < 0 and red < 0:
        # Team LOST additional points on their IR write-ups. Signed percent
        # expresses the loss magnitude relative to points conceded (e.g.
        # -25.0 means the IR penalties deepened their hole by 25% of what
        # red took). Negative signals "worse than doing nothing".
        return (ir / abs(red)) * 100, 'penalty'
    if ir < 0:
        # Negative IR with nothing conceded — no denominator; still a loss.
        return None, 'penalty'
    if ir <= 0 and red < 0:
        return None, 'no_ir'

    # Normal case: points conceded (negative red) and points recovered (positive IR)
    return (abs(ir) / abs(red)) * 100, 'normal'

def get_ir_feedback_data():
    """
    Per-team incident-response feedback data for the scorecard PDFs.

    Field statistics (average IR count) use ONLY active teams, consistent
    with the module-wide convention.

    Returns:
        (per_team, field_avg_ir_count)
        per_team: {team_number: {
            'ir_count': int,      # approved IR scores for the team
            'ir_entries': [       # ordered by submission time
                {'identifier': str or '',     # Score.reference_id
                 'feedback': str or None},    # Score.description
                ...
            ],
            'recovery_percent': float or None,
            'recovery_situation': 'normal'|'no_red'|'no_ir'|'no_data'
        }}
        field_avg_ir_count: float (mean approved IR count across active teams)

    Each approved IR score becomes one entry — duplicates included, since
    red may submit multiple feedback rounds against the same identifier.
    Active teams with zero IR activity still get an entry (empty list), so
    the field average reflects the whole active field.
    """
    active_teams = Team.query.filter_by(is_active=True).order_by(Team.team_number).all()
    if not active_teams:
        return {}, 0.0

    team_id_to_number = {t.id: t.team_number for t in active_teams}

    # Approved IR scores for active teams, oldest first so the feedback
    # list reads chronologically.
    ir_scores = Score.query.filter(
        Score.category == 'incident-response',
        Score.is_approved == True,
        Score.team_id.in_(team_id_to_number.keys())
    ).order_by(Score.timestamp.asc()).all()

    counts_by_team_id = {t.id: 0 for t in active_teams}
    entries_by_team_id = {t.id: [] for t in active_teams}
    for score in ir_scores:
        counts_by_team_id[score.team_id] = counts_by_team_id.get(score.team_id, 0) + 1
        # description holds the red team's feedback when they provide it
        feedback = (score.description or '').strip() or None
        entries_by_team_id[score.team_id].append({
            'identifier': score.reference_id or '',
            'feedback': feedback,
            'points': score.points,
        })

    field_avg = sum(counts_by_team_id.values()) / len(active_teams)

    per_team = {}
    for team in active_teams:
        recovery_percent, situation = get_ir_recovery_percent(team.id)
        per_team[team.team_number] = {
            'ir_count': counts_by_team_id[team.id],
            'ir_entries': entries_by_team_id[team.id],
            'recovery_percent': recovery_percent,
            'recovery_situation': situation,
        }

    # Field recovery-rate benchmark: MEDIAN of each team's % points regained.
    # Inclusion rules:
    #   - 'normal':  team's percent counts as-is
    #   - 'penalty': negative percent counts (bad write-ups drag it down)
    #   - 'no_ir':   counts as 0.0 — nothing recovered IS a recovery rate of zero
    #   - 'no_red' / 'no_data': EXCLUDED — no denominator, rate is undefined
    defined_pcts = []
    for data in per_team.values():
        situation = data['recovery_situation']
        if situation in ('no_red', 'no_data'):
            continue
        if situation == 'no_ir':
            defined_pcts.append(0.0)
        elif data['recovery_percent'] is not None:
            defined_pcts.append(data['recovery_percent'])
        # ('penalty' with red==0 yields percent=None — undefined, excluded)

    field_recovery_benchmark = None
    if defined_pcts:
        defined_pcts.sort()
        n = len(defined_pcts)
        mid = n // 2
        if n % 2 == 0:
            field_recovery_benchmark = round((defined_pcts[mid - 1] + defined_pcts[mid]) / 2, 1)
        else:
            field_recovery_benchmark = round(defined_pcts[mid], 1)
        # To switch to MEAN instead: field_recovery_benchmark = round(sum(defined_pcts) / len(defined_pcts), 1)

    # Ceiling benchmark: highest DEFINED recovery rate in the active field.
    # Teams with undefined rates ('no_red'/'no_data') are excluded, same
    # inclusion rules as the median — a 'no_red' team legitimately has no
    # recovery rate and should never crown the leaderboard.
    field_top_recovery = round(max(defined_pcts), 1) if defined_pcts else None

    return per_team, field_avg, field_recovery_benchmark, field_top_recovery

def calculate_red_team_scores(scoring_data):
    """
    Calculates red/IR team scores based on the stored configuration.

    Only ACTIVE teams participate: inactive teams are excluded from the
    per-team calculations, the min/max normalization range, and the
    high/low/average/median/mode statistics. A team deactivated mid-event
    can therefore no longer skew every other team's normalized red score.

    Args:
        scoring_data: Dictionary containing scoring configuration
                      (weights, ranges, etc.)

    Returns:
        tuple: (list of team dicts sorted by final score desc,
                dict of normalization stats including average, median,
                mode, highScore, lowScore)
    """
    if not scoring_data:
        return [], {}

    # Extract config values
    enable_ranges = scoring_data.get('enable_ranges', False)
    attack_weight = scoring_data.get('attack_weight', 1.0) if scoring_data.get('attack_weight') is not None else 1.0
    ir_weight = scoring_data.get('incident_response_weight', 1.0) if scoring_data.get('incident_response_weight') is not None else 1.0
    range_min = scoring_data.get('range', {}).get('min', -100000) if scoring_data.get('range') else -100000
    range_max = scoring_data.get('range', {}).get('max', 100000) if scoring_data.get('range') else 100000

    # --- ACTIVE TEAMS ONLY -------------------------------------------------
    # Scores belonging to inactive teams are skipped entirely so they never
    # enter team_calculations, the normalization range, or the stats.
    active_teams = {t.id: t for t in Team.query.filter_by(is_active=True).all()}
    if not active_teams:
        return [], {}

    # Fetch approved scores
    scores = Score.query.filter_by(is_approved=True).all()

    # Group by team and category
    team_scores = {}
    for score in scores:
        if score.team_id not in active_teams:
            continue

        if score.team_id not in team_scores:
            team_scores[score.team_id] = {
                'red': 0,
                'incident-response': 0,
                'team_obj': active_teams[score.team_id]
            }

        cat = score.category.lower()
        if cat == 'red':
            team_scores[score.team_id]['red'] += score.points
        elif cat == 'incident-response':
            team_scores[score.team_id]['incident-response'] += score.points

    if not team_scores:
        return [], {}

    # Build team calculations (identical math to before — the field of
    # teams is simply the active field now)
    team_calculations = []
    for team_id, data in team_scores.items():
        if not data['team_obj']:
            continue

        # Apply weights
        weighted_red = data['red'] * attack_weight if attack_weight != 0 else None
        weighted_ir = data['incident-response'] * ir_weight if ir_weight != 0 else None

        # Calculate raw final sum (weighted sum)
        raw_sum_parts = []
        if weighted_red is not None:
            raw_sum_parts.append(weighted_red)
        if weighted_ir is not None:
            raw_sum_parts.append(weighted_ir)

        if raw_sum_parts:
            raw_sum = sum(raw_sum_parts)
        else:
            # Both weights are 0, fall back to unweighted sum
            raw_sum = data['red'] + data['incident-response']

        team_calculations.append({
            'team_number': data['team_obj'].team_number,
            'team_name': data['team_obj'].team_name,
            'red_score': data['red'],
            'ir_score': data['incident-response'],
            'weighted_red': weighted_red,
            'weighted_ir': weighted_ir,
            'raw_sum': raw_sum,
            'normalized_score': None,
            'normalization_weight': None,
            'final_sum': raw_sum
        })

    normalization_stats = {}

    # Apply normalization if ranges are enabled and we have >1 team
    if enable_ranges and len(team_calculations) > 1:
        raw_sums = [t['raw_sum'] for t in team_calculations]
        min_score = min(raw_sums)
        max_score = max(raw_sums)

        normalization_stats = {
            'min': min_score,
            'max': max_score,
            'enabled': True
        }

        for team in team_calculations:
            if max_score == min_score:
                team['normalized_score'] = None
                team['normalization_weight'] = None
            else:
                # Normalize based on the weighted raw_sum
                norm_weight = (team['raw_sum'] - min_score) / (max_score - min_score)
                team['normalization_weight'] = round(norm_weight, 4)
                normalized = (norm_weight * (range_max - range_min)) + range_min
                team['normalized_score'] = round(normalized)

            if team['normalized_score'] is not None:
                team['final_sum'] = team['normalized_score']

    # Sort by Final Sum Descending
    sorted_teams = sorted(team_calculations, key=lambda x: x['final_sum'], reverse=True)

    # Calculate statistics if we have teams (active teams only)
    if sorted_teams:
        final_scores = [t['final_sum'] for t in sorted_teams]

        high_score = max(final_scores)
        low_score = min(final_scores)

        avg_final_score = round(sum(final_scores) / len(final_scores), 2)

        sorted_scores = sorted(final_scores)
        n = len(sorted_scores)
        if n % 2 == 0:
            median_score = round((sorted_scores[n//2 - 1] + sorted_scores[n//2]) / 2, 2)
        else:
            median_score = round(sorted_scores[n//2], 2)

        # Mode (most frequent value)
        score_counts = Counter(final_scores)
        max_count = max(score_counts.values())
        modes = [score for score, count in score_counts.items() if count == max_count]

        if max_count == 1:
            mode_score = None
        elif len(modes) == 1:
            mode_score = modes[0]
        else:
            # Multiple modes - return the smallest one for consistency
            mode_score = min(modes)

        normalization_stats['highScore'] = high_score
        normalization_stats['lowScore'] = low_score
        normalization_stats['average'] = avg_final_score
        normalization_stats['median'] = median_score
        normalization_stats['mode'] = mode_score
    else:
        normalization_stats['highScore'] = 0
        normalization_stats['lowScore'] = 0
        normalization_stats['average'] = 0
        normalization_stats['median'] = 0
        normalization_stats['mode'] = None

    return sorted_teams, normalization_stats


def get_orange_scoring_config():
    """
    Retrieves the orange scoring configuration from Valkey.
    Falls back to defaults if not set.

    Moved from routes/orangeConfig.py so the orange calculator is fully
    self-contained in this module.
    """
    try:
        vk = get_valkey_client()
        config_raw = vk.get("vantage:config:scoring:orange")

        if config_raw:
            return json.loads(config_raw)
    except Exception as e:
        current_app.logger.warning(f"Failed to read orange scoring config: {e}")

    # Default Configuration
    return {
        'normalize_enabled': False,
        'min_score': 0,
        'max_score': 100,
        'formula': 'raw_score'
    }


def calculate_orange_team_scores():
    """
    Calculates orange team scores based on the 'orange' category in the
    Score table.

    Only ACTIVE teams participate: inactive teams are excluded from the
    per-team scores, the raw min/max used for normalization, and the
    aggregate stats.

    Logic:
    1. Fetch approved orange scores for active teams.
    2. Group by team_id and sum raw scores.
    3. Determine the actual min and max raw scores across active teams.
    4. If normalization is enabled:
       - weight: (team_raw - min_team_raw) / (max_team_raw - min_team_raw)
       - map to configured range: (weight * (config_max - config_min)) + config_min
    5. If normalization is disabled, final score = raw sum.
    """
    config = get_orange_scoring_config()

    # --- ACTIVE TEAMS ONLY -------------------------------------------------
    active_teams = {t.id: t for t in Team.query.filter_by(is_active=True).all()}

    # 1. Fetch approved orange scores
    scores = Score.query.filter_by(category='orange', is_approved=True).all()

    # 2. Group by team and sum raw scores, skipping inactive teams
    team_totals = {}
    for score in scores:
        if score.team_id not in active_teams:
            continue

        if score.team_id not in team_totals:
            team_totals[score.team_id] = {
                'raw_sum': 0,
                'task_count': 0,
                'team_obj': active_teams[score.team_id]
            }
        team_totals[score.team_id]['raw_sum'] += score.points
        team_totals[score.team_id]['task_count'] += 1

    if not team_totals:
        return [], {
            'total_tasks': 0,
            'avg_score': 0,
            'median_score': 0,
            'min_team_score': 0,
            'max_team_score': 0,
            'teams_with_scores': 0,
            'config': config
        }

    # 3. Determine Min/Max Raw Scores across active teams
    raw_scores_list = [data['raw_sum'] for data in team_totals.values()]
    actual_min_raw = min(raw_scores_list)
    actual_max_raw = max(raw_scores_list)

    # 4. Calculate Final Scores
    team_calculations = []

    min_val = float(config.get('min_score', 0))
    max_val = float(config.get('max_score', 100))
    do_normalize = config.get('normalize_enabled', False)

    range_config = max_val - min_val

    for team_id, data in team_totals.items():
        if not data['team_obj']:
            continue

        raw_score = data['raw_sum']
        final_score = raw_score

        if do_normalize:
            # Avoid division by zero if all teams have the exact same score
            if actual_max_raw == actual_min_raw:
                normalized_weight = 1.0
            else:
                # Normalization Weight: (Raw - Min) / (Max - Min)
                normalized_weight = (raw_score - actual_min_raw) / (actual_max_raw - actual_min_raw)

            # Map weight to configured range: (weight * range) + min
            final_score = (normalized_weight * range_config) + min_val

            # Clamp just in case of floating point errors
            final_score = max(min_val, min(max_val, final_score))

        team_calculations.append({
            'team_number': data['team_obj'].team_number,
            'team_name': data['team_obj'].team_name,
            'raw_sum': raw_score,
            'task_count': data['task_count'],
            'normalized_score': round(final_score, 2) if do_normalize else None,
            'final_score': round(final_score, 2)
        })

    # 5. Sort by Final Score Descending
    sorted_teams = sorted(team_calculations, key=lambda x: x['final_score'], reverse=True)

    # 6. Calculate Stats (active teams only)
    total_tasks = sum(t['task_count'] for t in team_calculations)
    final_scores = [t['final_score'] for t in team_calculations]

    if final_scores:
        avg_final = sum(final_scores) / len(final_scores)
        sorted_fs = sorted(final_scores)
        n = len(sorted_fs)
        if n % 2 == 0:
            median_final = (sorted_fs[n//2 - 1] + sorted_fs[n//2]) / 2
        else:
            median_final = sorted_fs[n//2]
    else:
        avg_final = 0
        median_final = 0

    stats = {
        'total_tasks': total_tasks,
        'avg_score': round(avg_final, 2),
        'median_score': round(median_final, 2),
        'min_team_score': actual_min_raw,
        'max_team_score': actual_max_raw,
        'teams_with_scores': len(team_calculations),
        'config': config
    }

    return sorted_teams, stats


def _build_orange_lookup():
    """Run the orange team calculation and build a team_number -> result lookup."""
    orange_stats = {}
    try:
        orange_calculations, orange_stats = calculate_orange_team_scores()
        lookup = {calc['team_number']: calc for calc in orange_calculations}
    except Exception as e:
        current_app.logger.error(f"Failed to calculate orange team scores: {e}")
        lookup = {}
    return lookup, orange_stats

def _resolved_red_total(red_calc, raw_red_sum):
    """
    The canonical red value for standings-style views: the red calculator's
    final_sum (weighted, then normalized when ranges are enabled), falling
    back to the calculator's raw red_score, and to the raw approved red sum
    when the team has no calculation (config missing, or team has no
    red/IR scores).
    """
    if red_calc is not None:
        return (
            red_calc['final_sum'] if red_calc['final_sum'] is not None
            else red_calc['red_score']
        )
    return raw_red_sum


def calculate_team_standings():
    """
    Compute standings for all active teams, matching what is displayed
    on the competition dashboard.

    Red and orange category scores are overridden by their respective
    calculators (with raw-sum fallbacks); blue, white, and green are sums
    of approved raw scores.

    Returns:
        tuple: (teams_data, calculation_stats)

        teams_data is a list of dicts (sorted by total desc, rank assigned):
            rank, team_number, team_name, school_name,
            blue, red, white, orange, green, total,
            red_normalized, orange_normalized

        calculation_stats is a dict:
            red_normalization_stats, orange_normalization_stats
    """
    red_lookup, red_normalization_stats = _build_red_lookup()
    orange_lookup, orange_normalization_stats = _build_orange_lookup()

    teams = Team.query.filter_by(is_active=True).order_by(Team.team_number).all()

    teams_data = []
    for team in teams:
        # Only approved scores contribute to calculated standings
        approved_scores = Score.query.filter_by(team_id=team.id, is_approved=True).all()

        score_map = {cat: 0 for cat in SCORE_CATEGORIES}
        for score in approved_scores:
            cat = score.category.lower()
            # red and orange are overridden below by their calculators
            if cat in score_map and cat not in ('red', 'orange'):
                score_map[cat] += score.points

        # Red override (canonical resolution shared with red placement)
        raw_red_sum = sum(
            s.points for s in approved_scores if s.category.lower() == 'red'
        )
        score_map['red'] = _resolved_red_total(
            red_lookup.get(team.team_number), raw_red_sum
        )

        # Orange override (fall back to raw sum if team absent from calculation)
        if team.team_number in orange_lookup:
            orange_calc = orange_lookup[team.team_number]
            score_map['orange'] = (
                orange_calc['final_score'] if orange_calc['final_score'] is not None
                else orange_calc['raw_sum']
            )
        else:
            score_map['orange'] = sum(
                s.points for s in approved_scores if s.category.lower() == 'orange'
            )

        teams_data.append({
            'rank': 0,
            'team_number': team.team_number,
            'team_name': team.team_name,
            'school_name': team.school_name,
            'blue': score_map['blue'],
            'red': score_map['red'],
            'white': score_map['white'],
            'orange': score_map['orange'],
            'green': score_map['green'],
            'total': sum(score_map.values()),
            'red_normalized': red_lookup.get(team.team_number, {}).get('normalized_score') is not None,
            'orange_normalized': orange_lookup.get(team.team_number, {}).get('normalized_score') is not None,
        })

    # Sort by total score descending and assign ranks
    teams_data.sort(key=lambda x: x['total'], reverse=True)
    for i, team in enumerate(teams_data):
        team['rank'] = i + 1

    calculation_stats = {
        'red_normalization_stats': red_normalization_stats,
        'orange_normalization_stats': orange_normalization_stats,
    }

    return teams_data, calculation_stats


def calculate_overall_stats(teams_data):
    """Compute min/max/avg/median/count over a teams_data list."""
    overall_stats = {'min': 0, 'max': 0, 'avg': 0, 'median': 0, 'count': len(teams_data)}

    if not teams_data:
        return overall_stats

    total_scores = [t['total'] for t in teams_data]
    overall_stats['min'] = min(total_scores)
    overall_stats['max'] = max(total_scores)
    overall_stats['avg'] = sum(total_scores) / len(total_scores)

    sorted_scores = sorted(total_scores)
    n = len(sorted_scores)
    mid = n // 2
    if n % 2 == 0:
        overall_stats['median'] = (sorted_scores[mid - 1] + sorted_scores[mid]) / 2
    else:
        overall_stats['median'] = sorted_scores[mid]

    return overall_stats

def _rank_team_totals(totals_by_team_number):
    """
    Competition ranking over {team_number: total}: higher total = better
    rank, ties share a rank, next distinct total takes the next position.

    Handles negative totals correctly (a less-negative red total outranks a
    more-negative one) and float totals (normalized red scores).
    """
    team_list = sorted(totals_by_team_number.items(), key=lambda kv: (-kv[1], kv[0]))

    placements = {}
    current_rank = 1
    prev_total = None

    for i, (team_number, total) in enumerate(team_list):
        if prev_total is not None and total < prev_total:
            current_rank = i + 1

        placements[team_number] = {'rank': current_rank, 'total': total}
        prev_total = total

    return placements


def calculate_blue_placement():
    """
    Blue (services) placement for all ACTIVE teams, ranked by the sum of
    approved 'blue' category scores (higher is better).

    Returns ({team_number: {'rank': int, 'total': int}}, num_active_teams)
    """
    active_teams = Team.query.filter_by(is_active=True).all()
    if not active_teams:
        return {}, 0

    totals_rows = db.session.query(
        Score.team_id,
        func.sum(Score.points).label('total_points')
    ).filter(
        Score.category == 'blue',
        Score.is_approved == True
    ).group_by(Score.team_id).all()

    total_by_team_id = {row[0]: (row[1] or 0) for row in totals_rows}

    totals = {
        team.team_number: total_by_team_id.get(team.id, 0)
        for team in active_teams
    }

    return _rank_team_totals(totals), len(active_teams)


def calculate_red_placement():
    """
    Red placement for all ACTIVE teams, ranked by the SAME red values the
    dashboard standings show: the red calculator's weighted/normalized
    final_sum (raw-sum fallback when a team has no calculation). This keeps
    the scorecard feedback PDFs and the dashboard permanently in agreement
    about red ordering, even when attack/IR weights or normalization
    ranges are configured.

    Returns ({team_number: {'rank': int, 'total': int}}, num_active_teams)
    """
    active_teams = Team.query.filter_by(is_active=True).order_by(Team.team_number).all()
    if not active_teams:
        return {}, 0

    # Raw approved red sums, in one grouped query (fallback + no-calc teams)
    totals_rows = db.session.query(
        Score.team_id,
        func.sum(Score.points).label('total_points')
    ).filter(
        Score.category == 'red',
        Score.is_approved == True
    ).group_by(Score.team_id).all()
    raw_red_by_team_id = {row[0]: (row[1] or 0) for row in totals_rows}

    # Red calculator results (dict: team_number -> calc), same source as
    # the dashboard standings use.
    red_lookup, _ = get_red_calculation_lookup()

    totals = {}
    for team in active_teams:
        raw_red_sum = raw_red_by_team_id.get(team.id, 0)
        totals[team.team_number] = _resolved_red_total(
            red_lookup.get(team.team_number), raw_red_sum
        )

    return _rank_team_totals(totals), len(active_teams)