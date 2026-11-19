# utils/scoreCalculation.py
"""
Master source of truth for all scoring calculations.

Every consumer — the dashboard, score exports, and future scorecard/report
generation — must derive standings from calculate_team_standings() so all
views of the data agree. Do not reimplement this logic elsewhere.
"""

import json

from flask import current_app
from sqlalchemy import func

from models import db, Team, Score
from routes.redConfig import calculate_red_team_scores
from routes.orangeConfig import calculate_orange_team_scores
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
    """
    totals = get_raw_red_ir_totals(team_id)
    red = totals['red']
    ir = totals['ir']

    if ir == 0 and red == 0:
        return None, 'no_data'
    if ir > 0 and red >= 0:
        # Edge case: nothing conceded, but the team earned IR points anyway
        return None, 'no_red'
    if ir <= 0 and red < 0:
        return None, 'no_ir'

    # Normal case: points conceded (negative red) and points recovered (positive IR)
    return (abs(ir) / abs(red)) * 100, 'normal'


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

        # Red override (fall back to raw sum if team absent from calculation)
        if team.team_number in red_lookup:
            red_calc = red_lookup[team.team_number]
            score_map['red'] = (
                red_calc['final_sum'] if red_calc['final_sum'] is not None
                else red_calc['red_score']
            )
        else:
            score_map['red'] = sum(
                s.points for s in approved_scores if s.category.lower() == 'red'
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
