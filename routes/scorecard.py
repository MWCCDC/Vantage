# routes/scorecard.py
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app, Response
from routes.decorators import login_required, role_required
from utils.valkey_client import get_valkey_client
from utils.smtp_client import send_email
from models import db, Team, Score
import json
import io
import zipfile
from utils.scoreCalculation import (
          calculate_blue_placement, calculate_red_placement,
          get_ir_feedback_data
      )

# PDF generation
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
)

# Create the blueprint
scorecard_bp = Blueprint('scorecard', __name__, url_prefix='/scorecard')

# Colors matching the web UI
COLOR_GREEN = colors.HexColor('#28a745')
COLOR_RED = colors.HexColor('#dc3545')
COLOR_YELLOW = colors.HexColor('#ffc107')
COLOR_TEXT = colors.HexColor('#212529')
COLOR_MUTED = colors.HexColor('#6c757d')
COLOR_BAR_BG = colors.HexColor('#e9ecef')

# Percentage text color for PDF: darkened yellow for legibility on white paper
COLOR_AMBER_TEXT = colors.HexColor('#b8860b')

def get_engine_config():
    """Retrieve engine list from Valkey."""
    vk = get_valkey_client(db=0)
    raw = vk.get("quotient:engines")

    if raw:
        try:
            engines = json.loads(raw)
            if isinstance(engines, list) and len(engines) > 0:
                return engines
        except json.JSONDecodeError:
            current_app.logger.error("Failed to parse quotient:engines from Valkey.")

    return []

def get_active_team_numbers():
    """
    Return the set of team_numbers marked active in Vantage.

    Quotient may contain extra teams used for testing or as deliberately
    broken baselines — those must never influence scoring comparisons.
    Every comparison, average, benchmark, and export on this page uses
    ONLY teams active in Vantage.
    """
    rows = db.session.query(Team.team_number).filter(Team.is_active == True).all()
    return {row[0] for row in rows}

def parse_valkey_blob(raw):
    """Parse a Valkey-stored blob."""
    parsed = json.loads(raw)
    if isinstance(parsed, dict) and 'data' in parsed:
        inner = json.loads(parsed['data'])
        synced_at = parsed.get('synced_at', 'Unknown')
        return inner, synced_at
    if isinstance(parsed, dict):
        return parsed, 'Unknown'
    raise ValueError("Unexpected Valkey blob structure")

def build_display_names(all_teams_data):
    """
    Build human-friendly display names keyed by team_number (== Quotient team_id).

    Format: "Team <n> — <School Name>" if the school name exists in the DB,
    otherwise just "Team <n>". The Quotient-provided team_name ("team06")
    is not displayed.
    """
    team_numbers = {data['team_id'] for data in all_teams_data.values()}
    if team_numbers:
        db_teams = Team.query.filter(Team.team_number.in_(team_numbers)).all()
        school_by_number = {t.team_number: (t.school_name or '').strip() for t in db_teams}
    else:
        school_by_number = {}

    for data in all_teams_data.values():
        n = data['team_id']
        school = school_by_number.get(n, '')
        data['display_name'] = f"Team {n} — {school}" if school else f"Team {n}"

    return all_teams_data

def fetch_all_team_scores(active_team_numbers):
    """
    Fetch raw scores from all configured engines and merge by team.

    ONLY teams whose team_id matches an ACTIVE team number in Vantage are
    kept — Quotient test teams and non-scoring baseline teams are dropped
    before any benchmarking so they can't skew comparisons.

    Engines are expected to score DIFFERENT services on DIFFERENT networks
    (e.g., Engine 1 has no access to Network 2). Therefore service sets
    across engines should be disjoint, and all values are kept RAW —
    no averaging. If a service name unexpectedly appears on multiple
    engines, raw values are summed and a warning is logged.
    """
    engines = get_engine_config()
    if not engines:
        return None, "No engines configured."

    vk = get_valkey_client(db=0)
    all_teams_data = {}

    for engine in engines:
        engine_id = engine['id']
        scores_key = f"quotient:engine{engine_id}:scores"

        scores_raw = vk.get(scores_key)
        if not scores_raw:
            # Fallback to legacy key
            scores_raw = vk.get("quotient:scores")

        if not scores_raw:
            current_app.logger.warning(f"No score data found for Engine {engine_id}")
            continue

        try:
            scores_data, synced_at = parse_valkey_blob(scores_raw)

            if isinstance(scores_data, list):
                scores_list = scores_data
            elif isinstance(scores_data, dict):
                scores_list = scores_data.get('scores', scores_data.get('Scores', []))
            else:
                current_app.logger.warning(f"Unexpected score format for Engine {engine_id}")
                continue

            for entry in scores_list:
                if not isinstance(entry, dict):
                    continue

                team_id = entry.get('team_id')
                if team_id is None:
                    continue

                # Normalize numeric strings so IDs compare against team_number
                if isinstance(team_id, str) and team_id.isdigit():
                    team_id = int(team_id)

                # Active-team filter: drop Quotient test/non-scoring teams
                if team_id not in active_team_numbers:
                    current_app.logger.debug(
                        f"Skipping Quotient team {team_id} (not active in Vantage).")
                    continue

                # Initialize team if not exists
                if team_id not in all_teams_data:
                    all_teams_data[team_id] = {
                        'team_id': team_id,
                        'team_name': entry.get('team_name', f'Team {team_id}'),  # kept for matching only
                        'services': {},       # {service_name: raw values + source engines}
                        'engines': {}        # {engine_id: team totals for that engine}
                    }

                team_entry = all_teams_data[team_id]

                # Store engine-specific team totals
                team_entry['engines'][engine_id] = {
                    'gross_points': entry.get('gross_points', 0),
                    'total_sla_violations': entry.get('total_sla_violations', 0),
                    'total_sla_penalty': entry.get('total_sla_penalty', 0),
                    'total_points': entry.get('total_points', 0)
                }

                # Merge raw service values (expected: disjoint service sets)
                for svc in (entry.get('services') or []):
                    if not isinstance(svc, dict):
                        continue

                    service_name = svc.get('service_name', 'Unknown')
                    raw_points = svc.get('service_points', 0)
                    raw_violations = svc.get('sla_violations', 0)
                    raw_penalty = svc.get('sla_penalty', 0)

                    if service_name in team_entry['services']:
                        # Unexpected overlap: same service scored by multiple engines.
                        # Sum raw values so nothing is lost, and flag it loudly.
                        current_app.logger.warning(
                            f"Service '{service_name}' for team {team_id} appears on "
                            f"multiple engines (found again on Engine {engine_id}). "
                            f"Raw values summed — verify engine network segmentation."
                        )
                        svc_agg = team_entry['services'][service_name]
                        svc_agg['service_points'] += raw_points
                        svc_agg['sla_violations'] += raw_violations
                        svc_agg['sla_penalty'] += raw_penalty
                        svc_agg['source_engines'].append(engine_id)
                    else:
                        team_entry['services'][service_name] = {
                            'service_points': raw_points,
                            'sla_violations': raw_violations,
                            'sla_penalty': raw_penalty,
                            'source_engines': [engine_id]
                        }

        except Exception as e:
            current_app.logger.error(f"Failed to process scores for Engine {engine_id}: {str(e)}")

    return all_teams_data, None

def fetch_service_uptime(active_team_numbers):
    """
    Fetch service uptime data from Valkey for all engines and merge by team.

    ONLY teams active in Vantage are kept, so uptime is only ever displayed
    (and consumed) for scoring teams.

    Quotient stores uptime as a float FRACTION (e.g., 0.9298 = 92.98%),
    so it is normalized to a percentage here. All downstream consumers
    (web template, PDF) treat it as 0-100.

    Returns {team_number: {service_name: {'uptime': pct, ...}}}
    """
    engines = get_engine_config()
    vk = get_valkey_client(db=0)

    if not engines:
        return {}

    all_uptime_data = {}

    for engine in engines:
        engine_id = engine['id']
        uptime_key = f"quotient:engine{engine_id}:servicesUptime"

        uptime_raw = vk.get(uptime_key)
        if not uptime_raw:
            current_app.logger.warning(f"No uptime data found for Engine {engine_id}")
            continue

        try:
            uptime_data, _ = parse_valkey_blob(uptime_raw)

            if isinstance(uptime_data, dict):
                for team_num_str, team_services in uptime_data.items():
                    try:
                        team_num = int(team_num_str)
                    except (TypeError, ValueError):
                        continue

                    # Active-team filter
                    if team_num not in active_team_numbers:
                        continue

                    if team_num not in all_uptime_data:
                        all_uptime_data[team_num] = {}

                    for svc_name, svc_data in team_services.items():
                        if not isinstance(svc_data, dict):
                            continue

                        if svc_name in all_uptime_data[team_num]:
                            current_app.logger.warning(
                                f"Service '{svc_name}' for team {team_num} appears on "
                                f"multiple engines (found again on Engine {engine_id}). "
                                f"Uptime from first engine retained."
                            )
                        else:
                            uptime = svc_data.get('uptime')
                            # Normalize: Quotient gives us a fraction (0.9298 -> 92.98%).
                            # Guard against values already delivered as a percentage.
                            if uptime is not None and uptime <= 1.0:
                                uptime = uptime * 100

                            all_uptime_data[team_num][svc_name] = {
                                'uptime': uptime,
                                'sla_count': svc_data.get('sla_count', 0),
                                'last_10_rounds': svc_data.get('last_10_rounds', [])
                            }
        except Exception as e:
            current_app.logger.error(f"Failed to process uptime for Engine {engine_id}: {str(e)}")

    return all_uptime_data

def calculate_service_statistics(all_teams_data):
    """
    Compare each service to the best RAW score for THAT SAME service across
    all teams in all_teams_data — which contains ONLY active teams, so
    benchmarks reflect the real competitive field, not Quotient test/baseline
    teams.

    SLA values remain raw — never averaged or normalized. Additionally
    computes the field-average SLA violation count per service and each
    team's signed delta from it, so teams can see if they took more/fewer
    SLA hits than the pack.

    Uptime (already normalized to a 0-100 percentage by fetch_service_uptime)
    is attached per service when available.

    Best/worst service for a team is judged on relative percentage so
    low-valued services can still stand out.
    """
    if not all_teams_data:
        return None, {}

    # --- Pass 1a: Per-service score benchmarks (max raw score per service name) ---
    service_benchmarks = {}  # {service_name: max raw points across all teams}

    for team_data in all_teams_data.values():
        for service_name, service_data in team_data['services'].items():
            points = service_data['service_points']
            if service_name not in service_benchmarks or points > service_benchmarks[service_name]:
                service_benchmarks[service_name] = points

    # --- Pass 1b: Field-average SLA violations per service ---
    # Only teams that actually HAVE the service contribute to its average.
    sla_sums = {}    # {service_name: total sla_violations across teams}
    sla_counts = {}  # {service_name: number of teams reporting this service}

    for team_data in all_teams_data.values():
        for service_name, service_data in team_data['services'].items():
            sla_sums[service_name] = sla_sums.get(service_name, 0) + service_data['sla_violations']
            sla_counts[service_name] = sla_counts.get(service_name, 0) + 1

    service_sla_averages = {
        name: (sla_sums[name] / sla_counts[name]) if sla_counts[name] > 0 else 0
        for name in sla_sums
    }

    # --- Pass 2: Build per-team statistics using per-service benchmarks ---
    team_stats = {}

    for team_id, team_data in all_teams_data.items():
        services = team_data['services']
        if not services:
            continue

        best_service = None
        worst_service = None
        best_pct = -1.0
        worst_pct = float('inf')

        # Uptime data for this team (may be absent if sync hasn't run)
        uptime_data = team_data.get('uptime_data', {})

        service_list = []
        for service_name, svc in services.items():
            benchmark = service_benchmarks.get(service_name, 0)
            percentage = (svc['service_points'] / benchmark * 100) if benchmark > 0 else 0

            field_avg_sla = service_sla_averages.get(service_name, 0)
            # Signed delta: positive = more SLA hits than the field average (worse)
            sla_delta = svc['sla_violations'] - field_avg_sla

            uptime = None
            if service_name in uptime_data:
                uptime = uptime_data[service_name].get('uptime')

            svc_entry = {
                'name': service_name,
                'score': svc['service_points'],           # Raw
                'benchmark': benchmark,                    # Raw top score for this service
                'percentage': percentage,
                'sla_violations': svc['sla_violations'],  # Raw
                'sla_penalty': svc['sla_penalty'],        # Raw
                'field_avg_sla': round(field_avg_sla, 1),
                'sla_delta': round(sla_delta, 1),
                'uptime': uptime,                           # 0-100 percentage, or None
                'source_engines': svc['source_engines']
            }
            service_list.append(svc_entry)

            # Best/worst judged on relative performance
            if percentage > best_pct:
                best_pct = percentage
                best_service = svc_entry
            if percentage < worst_pct:
                worst_pct = percentage
                worst_service = svc_entry

        # Sort by percentage descending (relative ranking)
        service_list.sort(key=lambda x: (-x['percentage'], x['name']))

        team_stats[team_id] = {
            'team_id': team_id,
            'display_name': team_data.get('display_name', f'Team {team_id}'),
            'best_service': best_service,
            'worst_service': worst_service,
            'services': service_list,
            'total_services': len(service_list)
        }

    return team_stats, service_benchmarks

def fetch_red_team_stats():
    """
    Build red-team attack statistics from the database, keyed by team NUMBER
    (which matches the Quotient team IDs used elsewhere on this page).

    ONLY ACTIVE teams are considered — both for per-team summaries and for
    the per-refID field averages, so red activity against test teams can
    never deflate the field average.

    Returns a dict: {team_number: {'summary': [...], 'totals': {...}}}
    For every reference ID seen ANYWHERE in the competition, every active
    team gets a row — including refIDs the red team never landed on them
    (count 0). The delta compares a team's hit count against the average
    hit count for that refID across all active teams, so a negative delta
    means fewer successful attacks (better defensive performance).
    """
    # 1. Active teams only — each counts toward the averages, even if never hit.
    teams = Team.query.filter_by(is_active=True).all()
    if not teams:
        return {}

    team_number_by_id = {t.id: t.team_number for t in teams}
    num_teams = len(teams)

    # 2. All approved red scores
    red_scores = Score.query.filter_by(category='red', is_approved=True).all()

    # 3. Aggregate
    ref_hits = {}      # {ref_id: {team_number: hits}}
    ref_points = {}    # {ref_id: {team_number: total points}}
    ref_meta = {}      # {ref_id: {'type': str, 'points': int}}
    ref_totals = {}    # {ref_id: total hits across all teams}

    for score in red_scores:
        team_number = team_number_by_id.get(score.team_id)
        if team_number is None:
            # Score belongs to an inactive/unknown team — excluded from
            # per-team rows AND from the averages.
            continue

        ref_id = score.reference_id or 'NO_REF'

        if ref_id not in ref_hits:
            ref_hits[ref_id] = {}
            ref_points[ref_id] = {}
            ref_meta[ref_id] = {
                'type': score.description or 'Unknown',
                'points': score.points
            }
            ref_totals[ref_id] = 0

        team_bucket = ref_hits[ref_id].get(team_number, 0)
        ref_hits[ref_id][team_number] = team_bucket + 1
        ref_totals[ref_id] += 1

        pts_bucket = ref_points[ref_id].get(team_number, 0)
        ref_points[ref_id][team_number] = pts_bucket + score.points

        # Keep the most recently-seen metadata for display purposes
        ref_meta[ref_id] = {
            'type': score.description or ref_meta[ref_id]['type'],
            'points': score.points
        }

    # 4. Per-refID field averages across ACTIVE teams
    ref_averages = {ref_id: (total / num_teams) for ref_id, total in ref_totals.items()}

    # 5. Build per-team summaries — every active team sees EVERY refID, count 0 if never hit
    red_stats = {}

    for team in teams:
        tn = team.team_number

        summary_rows = []
        total_attacks = 0
        total_red_points = 0
        refs_hit = 0

        for ref_id, average in ref_averages.items():
            count = ref_hits[ref_id].get(tn, 0)
            points = ref_points[ref_id].get(tn, 0)
            delta = count - average

            total_attacks += count
            total_red_points += points
            if count > 0:
                refs_hit += 1

            summary_rows.append({
                'ref_id': ref_id,
                'type': ref_meta[ref_id]['type'],
                'count': count,
                'field_avg': round(average, 1),
                'delta': round(delta, 1),   # positive = more hits than average (worse)
                'points': points
            })

        # Sort: most-hit refIDs first, then by ref_id for stability
        summary_rows.sort(key=lambda r: (-r['count'], r['ref_id']))

        red_stats[tn] = {
            'summary': summary_rows,
            'totals': {
                'total_attacks': total_attacks,
                'refs_hit': refs_hit,
                'total_ref_types': len(ref_averages),
                'total_red_points': total_red_points
            }
        }

    return red_stats


# =====================================================================
# SHARED DATA PIPELINE
# =====================================================================

def load_scorecard_data():
    """
    Run the full data pipeline once and return everything the page,
    PDF export, and email routes need.

    All comparisons (benchmarks, SLA averages, red-team averages,
    placements) use ONLY teams marked active in Vantage.

    Returns (all_teams_data, team_stats, service_benchmarks, red_stats,
             blue_placements, red_placements, num_active_teams, error)
    """
    active_team_numbers = get_active_team_numbers()

    all_teams_data, error = fetch_all_team_scores(active_team_numbers)
    red_stats = fetch_red_team_stats()
    ir_feedback, field_avg_ir_count, ir_recovery_benchmark, ir_top_recovery_pct = get_ir_feedback_data()

    if error:
        return None, None, None, red_stats, None, 0.0, None, None, {}, {}, 0, error

    all_teams_data = all_teams_data or {}

    # Fetch and attach uptime data (normalized to percentages, active teams only)
    uptime_data = fetch_service_uptime(active_team_numbers)
    for team_num, team_services in uptime_data.items():
        if team_num in all_teams_data:
            all_teams_data[team_num]['uptime_data'] = team_services

    build_display_names(all_teams_data)
    team_stats, service_benchmarks = calculate_service_statistics(all_teams_data)

    # Calculate placements
    blue_placements, num_active_teams = calculate_blue_placement()
    red_placements, _ = calculate_red_placement()

    return all_teams_data, team_stats, service_benchmarks, red_stats, ir_feedback, field_avg_ir_count, ir_recovery_benchmark, ir_top_recovery_pct, blue_placements, red_placements, num_active_teams, None

def build_team_pdf_from_data(team_number, all_teams_data, team_stats, red_stats,
                             ir_feedback, field_avg_ir_count, ir_recovery_benchmark,
                             ir_top_recovery_pct, blue_placements, red_placements,
                             num_active_teams):
    """
    Build the PDF for one team from a pre-computed pipeline result.
    Returns (pdf_bytes, display_name, has_data).
    """
    if all_teams_data is None or team_number not in all_teams_data:
        return None, f'Team {team_number}', False

    selected_stats = (team_stats or {}).get(team_number)
    selected_red = red_stats.get(team_number)
    selected_ir = (ir_feedback or {}).get(team_number)
    field_avg_ir = field_avg_ir_count or 0.0
    blue_placement = (blue_placements or {}).get(team_number)
    red_placement = (red_placements or {}).get(team_number)
    display_name = all_teams_data[team_number].get('display_name', f'Team {team_number}')

    pdf_bytes = build_scorecard_pdf(display_name, selected_stats, selected_red,
                                    selected_ir, field_avg_ir, ir_recovery_benchmark,
                                    ir_top_recovery_pct, blue_placement, red_placement,
                                    num_active_teams)
    return pdf_bytes, display_name, True

def sanitize_filename(display_name):
    """Sanitize a display name for use in a filename."""
    safe_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in display_name)
    return safe_name.replace(' ', '_')


# =====================================================================
# EMAIL HELPERS (mirroring routes/extAccounts.py conventions)
# =====================================================================

def _parse_recipients(to_field):
    """Split a comma-separated recipient string into a clean list."""
    if not to_field:
        return []
    return [addr.strip() for addr in to_field.split(',') if addr.strip()]

def _apply_message_template(message, team_obj):
    """Replace {schoolName} and {teamNum} placeholders in a message body."""
    return (message or '') \
        .replace('{schoolName}', (team_obj.school_name or '').strip()) \
        .replace('{teamNum}', str(team_obj.team_number))


# =====================================================================
# PDF GENERATION HELPERS
# =====================================================================

def _fmt_signed(value):
    """Format a signed delta: '+3', '-1.5', '0' (no stray sign on zero)."""
    if value > 0:
        return f"+{value:g}"
    if value < 0:
        return f"{value:g}"
    return "0"

def _pct_text_color(pct):
    """Map a percentage to a legible TEXT color (amber darkened on white)."""
    if pct >= 90:
        return COLOR_GREEN
    elif pct >= 70:
        return COLOR_AMBER_TEXT
    return COLOR_RED

def _delta_cell(delta, positive_is_good=False, alignment=TA_CENTER):
    good, bad = (COLOR_GREEN, COLOR_RED) if positive_is_good else (COLOR_RED, COLOR_GREEN)
    if delta > 0:
        color = good
    elif delta < 0:
        color = bad
    else:
        color = COLOR_MUTED
    return Paragraph(_fmt_signed(delta), ParagraphStyle(
        f'delta_{color}', parent=_STYLE_VALUE,
        alignment=alignment, textColor=color, fontName='Helvetica-Bold'))


def _recovery_cell(recovery_percent, situation, alignment=TA_CENTER):
    if situation == 'no_red':
        return Paragraph("Bonus", ParagraphStyle(
            'recovery_bonus', parent=_STYLE_VALUE,
            alignment=alignment, textColor=COLOR_GREEN, fontName='Helvetica-Bold'))
    if situation == 'no_data':
        return Paragraph("—", ParagraphStyle(
            'recovery_none', parent=_STYLE_VALUE,
            alignment=alignment, textColor=COLOR_MUTED))
    if situation == 'penalty':
        # Negative IR points: the write-ups cost the team additional points.
        text = (f"{recovery_percent:.1f}%"
                if recovery_percent is not None else "Lost points")
        return Paragraph(text, ParagraphStyle(
            'recovery_penalty', parent=_STYLE_VALUE,
            alignment=alignment, textColor=COLOR_RED, fontName='Helvetica-Bold'))
    pct = recovery_percent if recovery_percent is not None else 0.0
    return Paragraph(f"{pct:.1f}%", ParagraphStyle(
        f'recovery_{pct:.0f}', parent=_STYLE_VALUE,
        alignment=alignment, textColor=_pct_text_color(pct), fontName='Helvetica-Bold'))

def _pct_cell(pct):
    """
    Build a centered, color-coded Paragraph for a percentage value.

    Text-only (no bar). Uses the shared 90/70 green/amber/red thresholds,
    with amber darkened for legibility on white paper.
    """
    return Paragraph(f"{pct:.1f}%", ParagraphStyle(
        f'pct_{pct:.0f}', parent=_STYLE_VALUE,
        alignment=TA_CENTER, textColor=_pct_text_color(pct), fontName='Helvetica-Bold'))

def _uptime_cell(uptime):
    """
    Build a centered, colored Paragraph for a service uptime percentage.

    Text-only (no bar). Uses the shared 90/70 green/amber/red thresholds,
    with amber darkened for legibility on white paper. Missing uptime
    renders as a muted dash.
    """
    if uptime is None:
        return Paragraph("—", ParagraphStyle(
            'uptime_none', parent=_STYLE_VALUE,
            alignment=TA_CENTER, textColor=COLOR_MUTED))

    return Paragraph(f"{uptime:.1f}%", ParagraphStyle(
        f'uptime_{uptime:.0f}', parent=_STYLE_VALUE,
        alignment=TA_CENTER, textColor=_pct_text_color(uptime), fontName='Helvetica-Bold'))

def _kv_summary_table(rows):
    """
    Build a 2-column 'label / value' summary table used in both sections.

    Values may be plain strings (wrapped in _STYLE_VALUE here) or
    ready-made Flowables such as the Paragraphs returned by
    _delta_cell() / _recovery_cell() — those pass through untouched.
    """
    data = []
    for label, value in rows:
        label_p = Paragraph(label, _STYLE_LABEL)
        if isinstance(value, str):
            value_p = Paragraph(value, _STYLE_VALUE)
        else:
            value_p = value  # already a Flowable (cell helper output)
        data.append([label_p, value_p])

    t = Table(data, colWidths=[2.1 * inch, 4.5 * inch])
    t.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('LEFTPADDING', (0, 0), (0, -1), 0),
    ]))
    return t

# Styles (initialized lazily because reportlab styles are global)
def _init_styles():
    global _STYLE_TITLE, _STYLE_HEADING, _STYLE_LABEL, _STYLE_VALUE, _STYLE_NOTE
    base = getSampleStyleSheet()

    _STYLE_TITLE = ParagraphStyle('FeedbackTitle', parent=base['Title'],
                                  fontSize=20, leading=24, alignment=TA_CENTER,
                                  textColor=COLOR_TEXT, spaceAfter=6)

    _STYLE_HEADING = ParagraphStyle('SectionHeading', parent=base['Heading2'],
                                    fontSize=14, leading=18, textColor=COLOR_TEXT,
                                    spaceBefore=14, spaceAfter=6)

    _STYLE_LABEL = ParagraphStyle('SummaryLabel', parent=base['Normal'],
                                  fontSize=10, textColor=COLOR_MUTED)

    _STYLE_VALUE = ParagraphStyle('SummaryValue', parent=base['Normal'],
                                  fontSize=10, textColor=COLOR_TEXT)

    _STYLE_NOTE = ParagraphStyle('Footnote', parent=base['Normal'],
                                 fontSize=8, textColor=COLOR_MUTED, spaceBefore=4)

_init_styles()


def build_scorecard_pdf(display_name, selected_stats, selected_red,
                        selected_ir, field_avg_ir, ir_recovery_benchmark,
                        ir_top_recovery_pct, blue_placement, red_placement,
                        num_active_teams):
    """
    Assemble the team feedback PDF. Returns PDF bytes.

    Layout:
      - Header: "Team # — School Name" (or "Team #")
      - Horizontal rule between each section
      - Services: best/worst summary + X/N placement, then a compact
        per-service table (name, % of top, uptime, SLA violations,
        SLA vs Avg). Percentages and uptime are color-coded text only —
        no bars.
        NO point values, NO engine attribution.
      - Red Team: attack count, unique-hits fraction + X/N placement,
        then a compact per-refID table (refID, hits, hits-vs-average).
        NO points, NO attack-type disclosure.
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
        topMargin=0.75 * inch, bottomMargin=0.75 * inch,
        title=display_name
    )

    story = []

    # ---------- Header ----------
    story.append(Paragraph(display_name, _STYLE_TITLE))
    story.append(HRFlowable(width="100%", thickness=1, color=COLOR_TEXT, spaceAfter=10))

    # ---------- Services: Summary ----------
    story.append(Paragraph("Services", _STYLE_HEADING))

    svc_rows = []
    if selected_stats:
        best = selected_stats.get('best_service')
        worst = selected_stats.get('worst_service')

        if best:
            svc_rows.append((
                "Best Service",
                f'<font color="#28a745">{best["name"]}</font>'
                f' <font color="#6c757d">({best["percentage"]:.1f}% of top)</font>'
            ))
        if worst:
            svc_rows.append((
                "Worst Service",
                f'<font color="#dc3545">{worst["name"]}</font>'
                f' <font color="#6c757d">({worst["percentage"]:.1f}% of top)</font>'
            ))
        svc_rows.append(("Services Tracked", str(selected_stats.get('total_services', 0))))
        if best:
            svc_rows.append(("Best Relative Performance", f'{best["percentage"]:.1f}%'))

        # Placement among active teams
        if blue_placement:
            svc_rows.append(("Placement (Services)", f"{blue_placement['rank']} / {num_active_teams}"))
    else:
        svc_rows.append(("Services Tracked", "No service data available."))

    story.append(_kv_summary_table(svc_rows))

    # ---------- Services: Table ----------
    if selected_stats and selected_stats.get('services'):
        story.append(Spacer(1, 10))

        table_data = [["Service", "% of Top", "Uptime", "SLA Violations", "SLA vs Avg"]]

        for svc in selected_stats['services']:
            table_data.append([
                Paragraph(svc['name'], _STYLE_VALUE),
                _pct_cell(svc['percentage']),
                _uptime_cell(svc.get('uptime')),
                str(svc['sla_violations']),
                _delta_cell(svc['sla_delta']),
            ])

        # Total usable width: 8.5in page - 1.5in margins = 7.0in
        col_widths = [2.7 * inch, 1.2 * inch, 1.0 * inch, 1.05 * inch, 1.05 * inch]
        t = Table(table_data, colWidths=col_widths, repeatRows=1)

        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), COLOR_BAR_BG),
            ('TEXTCOLOR', (0, 0), (-1, 0), COLOR_TEXT),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 9),
            ('FONTSIZE', (0, 1), (-1, -1), 9),
            # Numeric / % of top / uptime / delta columns centered
            ('ALIGN', (1, 0), (-1, -1), 'CENTER'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('TOPPADDING', (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ('GRID', (0, 0), (-1, -1), 0.5, COLOR_BAR_BG),
        ]))
        story.append(t)

        story.append(Paragraph(
            "'% of Top' compares each service's points against the best score achieved by "
            "any active team for that same service. Uptime is the percentage of checked "
            "rounds the service was available. 'SLA vs Avg' is this team's violation count "
            "minus the average for that service across all active teams (negative is better).",
            _STYLE_NOTE))

    # ---------- Divider between major sections ----------
    story.append(Spacer(1, 16))
    story.append(HRFlowable(width="100%", thickness=1, color=COLOR_TEXT, spaceAfter=10))

    # ---------- Red Team: Summary ----------
    story.append(Paragraph("Red Team Activity", _STYLE_HEADING))

    red_rows = []
    if selected_red:
        totals = selected_red['totals']
        red_rows.append(("Successful Attacks", str(totals['total_attacks'])))
        red_rows.append((
            "Unique Attack Types Hit",
            f"{totals['refs_hit']} / {totals['total_ref_types']}"
        ))

        # Placement among active teams
        if red_placement:
            red_rows.append(("Placement (Red Defense)", f"{red_placement['rank']} / {num_active_teams}"))
    else:
        red_rows.append(("Successful Attacks", "No red team data available."))

    story.append(_kv_summary_table(red_rows))

    # ---------- Red Team: Table ----------
    if selected_red and selected_red.get('summary'):
        story.append(Spacer(1, 10))

        table_data = [["Ref ID", "Hits", "Hits vs Avg"]]

        for row in selected_red['summary']:
            table_data.append([
                Paragraph(row['ref_id'], _STYLE_VALUE),
                str(row['count']),
                _delta_cell(row['delta']),
            ])

        col_widths = [4.0 * inch, 1.55 * inch, 1.55 * inch]
        t = Table(table_data, colWidths=col_widths, repeatRows=1)

        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), COLOR_BAR_BG),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 9),
            ('FONTSIZE', (0, 1), (-1, -1), 9),
            ('ALIGN', (1, 0), (-1, -1), 'CENTER'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('TOPPADDING', (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ('GRID', (0, 0), (-1, -1), 0.5, COLOR_BAR_BG),
        ]))
        story.append(t)

        story.append(Paragraph(
            "'Hits vs Avg' is this team's hit count minus the average count for that "
            "attack type across all active teams (negative is better). A zero with a "
            "negative value means that attack succeeded against other teams but never this one.",
            _STYLE_NOTE))
    
    # ---------- Incident Response: Summary ----------
    story.append(Paragraph("Incident Response", _STYLE_HEADING))

    ir_rows = []
    if selected_ir:
        ir_delta = selected_ir['ir_count'] - field_avg_ir

        ir_rows.append(("IR Submissions", str(selected_ir['ir_count'])))
        ir_rows.append(("IRs vs Average", _delta_cell(ir_delta, positive_is_good=True,
                                                      alignment=TA_LEFT)))
        # Recovery vs field: team's own % plus a signed delta against the
        # field benchmark (median of defined recovery rates). The benchmark
        # VALUE itself is never shown — only the delta, so teams see their
        # positioning without the underlying field statistic.
        def _field_delta_frag(delta, label):
            """Signed delta fragment colored by sign: '(-8.2 % pts vs all teams)'."""
            sign = '+' if delta > 0 else ''
            color = ('#28a745' if delta > 0
                     else '#dc3545' if delta < 0
                     else '#6c757d')
            return (f"<font size='8' color='{color}'>"
                    f"({sign}{delta:.1f} % pts {label})</font>")

        team_pct = (0.0 if selected_ir['recovery_situation'] == 'no_ir'
                    else selected_ir['recovery_percent'])

        if (ir_recovery_benchmark is not None and team_pct is not None
                and ir_top_recovery_pct is not None):
            recovery_delta = round(team_pct - ir_recovery_benchmark, 1)
            top_delta = round(team_pct - ir_top_recovery_pct, 1)

            if team_pct >= 90:
                pct_color = '#28a745'
            elif team_pct >= 70:
                pct_color = '#b8860b'
            else:
                pct_color = '#dc3545'

            ir_rows.append(("Points Regained", Paragraph(
                f"<font color='{pct_color}'>{team_pct:.1f}%</font>"
                f" {_field_delta_frag(recovery_delta, 'vs all teams')}"
                f" {_field_delta_frag(top_delta, 'vs top')}",
                ParagraphStyle('recovery_vs_field', parent=_STYLE_VALUE,
                               alignment=TA_LEFT, fontName='Helvetica-Bold'))))
        else:
            ir_rows.append(("Points Regained", _recovery_cell(
                selected_ir['recovery_percent'],
                selected_ir['recovery_situation'],
                alignment=TA_LEFT)))
    else:
        ir_rows.append(("IR Submissions", "No incident response data available."))

    story.append(_kv_summary_table(ir_rows))

    story.append(Paragraph(
        "'IRs vs Average' compares the team's count of incident "
        "response submissions to the average across all active teams "
        "(above average is favorable). 'Points Regained' is the share of "
        "red-team points recovered through incident response, computed "
        "from raw scores; a 'Bonus' marker means the team earned IR "
        "points while conceding none. Incident feedback is provided by "
        "the red team where available.",
        _STYLE_NOTE))

    # ---------- Incident Response: Per-Incident Feedback ----------
    # Each approved IR entry with the red team's feedback (from the score
    # description). No "refID" wording is shown to teams.
    if selected_ir and selected_ir.get('ir_entries'):
        story.append(Spacer(1, 6))
        story.append(HRFlowable(width="100%", thickness=0.75,
                                color=COLOR_BAR_BG, spaceAfter=8))

        fb_rows = []
        for entry in selected_ir['ir_entries']:
            identifier = entry.get('identifier') or ''
            feedback = entry.get('feedback')
            points = entry.get('points', 0)

            if identifier is not None and points < 0:
                label = f'{identifier} (<font color="#dc3545"><b>- pts</b></font>)'
            else:
                label = identifier if identifier else "-"

            
            value = feedback if feedback else "Feedback not found"

            fb_rows.append((label, value))

        story.append(_kv_summary_table(fb_rows))

    doc.build(story)
    pdf_bytes = buf.getvalue()
    buf.close()
    return pdf_bytes


# =====================================================================
# ROUTES
# =====================================================================

@scorecard_bp.route('/', methods=['GET'])
@login_required
@role_required('admin')
def scorecard_dashboard():
    """Admin scorecard dashboard with team selection, service and red team feedback."""

    all_teams_data, team_stats, service_benchmarks, red_stats, ir_feedback, field_avg_ir_count, ir_recovery_benchmark, ir_top_recovery_pct, blue_placements, red_placements, num_active_teams, error = load_scorecard_data()

    # Recipient addresses per team (from team metadata), for the email modal
    team_emails = {}
    for team in Team.query.filter_by(is_active=True).all():
        team_emails[team.team_number] = (team.team_metadata or {}).get('emails', '') or ''

    if error:
        return render_template('scorecard.html',
                               is_admin_page=True,
                               error=error,
                               teams=[],
                               team_emails=team_emails,
                               selected_team=None,
                               team_stats=None,
                               selected_stats=None,
                               selected_red=None,
                               selected_ir=None,
                               ir_recovery_benchmark=None,
                               ir_top_recovery_pct=None,
                               service_benchmarks=None,
                               blue_placements={},
                               red_placements={},
                               num_active_teams=0)

    # Prepare team list for dropdown (sorted by team number).
    # all_teams_data contains only ACTIVE teams, so inactive / Quotient
    # test teams never appear in the dropdown.
    teams_list = sorted(all_teams_data.values(), key=lambda x: x['team_id'])

    # Get selected team from query param
    selected_team_id = request.args.get('team_id', type=int)
    selected_team = None
    selected_stats = None
    selected_red = None
    selected_ir = None

    if selected_team_id is not None and team_stats and selected_team_id in team_stats:
        selected_team = all_teams_data[selected_team_id]
        selected_stats = team_stats[selected_team_id]
        # Red stats are keyed by team NUMBER (== Quotient team ID)
        selected_red = red_stats.get(selected_team_id)
        # IR feedback keyed by team number, same as red stats
        selected_ir = (ir_feedback or {}).get(selected_team_id)

    return render_template('scorecard.html',
                           is_admin_page=True,
                           error=None,
                           teams=teams_list,
                           team_emails=team_emails,
                           selected_team=selected_team,
                           team_stats=team_stats,
                           selected_stats=selected_stats,
                           selected_red=selected_red,
                           selected_ir=selected_ir,
                           ir_recovery_benchmark=ir_recovery_benchmark,
                           field_avg_ir_count=field_avg_ir_count,
                           ir_top_recovery_pct=ir_top_recovery_pct,
                           service_benchmarks=service_benchmarks,
                           blue_placements=blue_placements,
                           red_placements=red_placements,
                           num_active_teams=num_active_teams)

@scorecard_bp.route('/export/pdf', methods=['GET'])
@login_required
@role_required('admin')
def export_scorecard_pdf():
    """Display a team's feedback PDF inline in the browser."""
    team_id = request.args.get('team_id', type=int)

    if team_id is None:
        flash('No team selected for PDF export.', 'error')
        return redirect(url_for('scorecard.scorecard_dashboard'))

    all_teams_data, team_stats, service_benchmarks, red_stats, ir_feedback, field_avg_ir_count, ir_recovery_benchmark, ir_top_recovery_pct, blue_placements, red_placements, num_active_teams, error = load_scorecard_data()

    if error or not all_teams_data or team_id not in all_teams_data:
        flash(error or f'No Quotient data available for team {team_id}.', 'error')
        return redirect(url_for('scorecard.scorecard_dashboard', team_id=team_id))

    try:
        pdf_bytes, display_name, _ = build_team_pdf_from_data(
            team_id, all_teams_data, team_stats, red_stats,
            ir_feedback, field_avg_ir_count, ir_recovery_benchmark,
            ir_top_recovery_pct, blue_placements, red_placements,
            num_active_teams)
    except Exception as e:
        current_app.logger.error(f"Feedback PDF generation failed for team {team_id}: {str(e)}")
        flash(f'Failed to generate PDF: {str(e)}', 'error')
        return redirect(url_for('scorecard.scorecard_dashboard', team_id=team_id))

    filename = f"{sanitize_filename(display_name)}.pdf"

    # 'inline' displays in the browser tab; filename still applies if saved
    return Response(
        pdf_bytes,
        mimetype='application/pdf',
        headers={
            'Content-Disposition': f'inline; filename="{filename}"',
            'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0'
        })

@scorecard_bp.route('/export/all-zip', methods=['GET'])
@login_required
@role_required('admin')
def export_all_scorecards_zip():
    """
    Generate a ZIP archive containing every ACTIVE team's feedback PDF and
    stream it to the browser as a download.

    An alternative to the email-all flow: the admin downloads the archive
    and distributes the files through some other channel (e.g., alongside
    additional materials). Inactive teams and Quotient test teams are
    excluded — the ZIP contains only teams active in Vantage.
    """
    all_teams_data, team_stats, service_benchmarks, red_stats, ir_feedback, field_avg_ir_count, ir_recovery_benchmark, ir_top_recovery_pct, blue_placements, red_placements, num_active_teams, error = load_scorecard_data()

    if error:
        flash(f'Cannot generate PDFs: {error}', 'error')
        return redirect(url_for('scorecard.scorecard_dashboard'))

    if not all_teams_data:
        flash('No Quotient data available for any active team.', 'error')
        return redirect(url_for('scorecard.scorecard_dashboard'))

    # Build the ZIP in memory. all_teams_data is already filtered to
    # active teams by the pipeline.
    zip_buf = io.BytesIO()
    included = 0
    failed = []

    try:
        with zipfile.ZipFile(zip_buf, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
            # Iterate by team number (matching the dropdown order), which also
            # matches the Quotient team_id keys in all_teams_data.
            for team_number in sorted(all_teams_data.keys()):
                try:
                    pdf_bytes, display_name, has_data = build_team_pdf_from_data(
                        team_number, all_teams_data, team_stats, red_stats,
                        ir_feedback, field_avg_ir_count, ir_recovery_benchmark,
                        ir_top_recovery_pct, blue_placements, red_placements,
                        num_active_teams)
                except Exception as e:
                    current_app.logger.error(
                        f'ZIP export: PDF generation failed for team {team_number}: {str(e)}')
                    failed.append(team_number)
                    continue

                if not has_data:
                    continue

                zf.writestr(f"{sanitize_filename(display_name)}.pdf", pdf_bytes)
                included += 1

        if included == 0:
            flash('No team PDFs could be generated for the ZIP export.', 'error')
            return redirect(url_for('scorecard.scorecard_dashboard'))

        if failed:
            current_app.logger.warning(
                f'ZIP export completed with failures for team(s): {", ".join(map(str, failed))}')

        zip_bytes = zip_buf.getvalue()
    except Exception as e:
        current_app.logger.error(f'ZIP export failed: {str(e)}')
        flash(f'Failed to build ZIP archive: {str(e)}', 'error')
        return redirect(url_for('scorecard.scorecard_dashboard'))
    finally:
        zip_buf.close()

    current_app.logger.info(f'ZIP export: generated {included} team PDF(s).')

    return Response(
        zip_bytes,
        mimetype='application/zip',
        headers={
            'Content-Disposition': 'attachment; filename="team_scorecards.zip"',
            'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0'
        })

@scorecard_bp.route('/email/<int:team_number>', methods=['POST'])
@login_required
@role_required('admin')
def email_selected_team(team_number):
    """Email a single team their PDF, using the modal's To/Subject/Message."""
    user = session.get('user_info', {})

    team_obj = Team.query.filter_by(team_number=team_number).first()
    if not team_obj:
        flash('Team not found.', 'error')
        return redirect(url_for('scorecard.scorecard_dashboard'))

    to_list = _parse_recipients(request.form.get('email_to'))
    subject = request.form.get('email_subject', '').strip()
    message = request.form.get('email_message', '')

    if not to_list:
        flash('At least one recipient address is required.', 'error')
        return redirect(url_for('scorecard.scorecard_dashboard', team_id=team_number))
    if not subject:
        flash('A subject is required.', 'error')
        return redirect(url_for('scorecard.scorecard_dashboard', team_id=team_number))

    all_teams_data, team_stats, service_benchmarks, red_stats, ir_feedback, field_avg_ir_count, ir_recovery_benchmark, ir_top_recovery_pct, blue_placements, red_placements, num_active_teams, error = load_scorecard_data()

    if error:
        flash(f'Cannot generate PDF: {error}', 'error')
        return redirect(url_for('scorecard.scorecard_dashboard', team_id=team_number))

    try:
        pdf_bytes, display_name, has_data = build_team_pdf_from_data(
            team_number, all_teams_data, team_stats, red_stats,
            ir_feedback, field_avg_ir_count, ir_recovery_benchmark,
            ir_top_recovery_pct, blue_placements, red_placements,
            num_active_teams)
        if not has_data:
            flash(f'Team {team_number} has no Quotient data to send.', 'error')
            return redirect(url_for('scorecard.scorecard_dashboard', team_id=team_number))

        success = send_email(
            subject=subject,
            body=_apply_message_template(message, team_obj),
            to=to_list,
            attachments=[(f"{sanitize_filename(display_name)}.pdf", pdf_bytes)],
            sent_by=user.get('username', 'UNKNOWN')
        )
    except Exception as e:
        current_app.logger.error(f'Failed to email team {team_number}: {str(e)}')
        flash(f'An error occurred while emailing Team {team_number}.', 'error')
        return redirect(url_for('scorecard.scorecard_dashboard', team_id=team_number))

    if success:
        flash(f'Email sent to {len(to_list)} recipient(s) for Team {team_number}.', 'success')
    else:
        flash(f'Failed to send email for Team {team_number}. Check server logs.', 'error')

    return redirect(url_for('scorecard.scorecard_dashboard', team_id=team_number))

@scorecard_bp.route('/email/all', methods=['POST'])
@login_required
@role_required('admin')
def email_all_teams():
    """
    Email every active team that has an 'emails' value in its team metadata,
    one email per team with that team's PDF attached.
    """
    user = session.get('user_info', {})

    subject = request.form.get('email_subject', '').strip()
    message = request.form.get('email_message', '')

    if not subject:
        flash('A subject is required.', 'error')
        return redirect(url_for('scorecard.scorecard_dashboard'))

    all_teams_data, team_stats, service_benchmarks, red_stats, ir_feedback, field_avg_ir_count, ir_recovery_benchmark, ir_top_recovery_pct, blue_placements, red_placements, num_active_teams, error = load_scorecard_data()

    if error:
        flash(f'Cannot generate PDFs: {error}', 'error')
        return redirect(url_for('scorecard.scorecard_dashboard'))

    teams = Team.query.filter_by(is_active=True).order_by(Team.team_number).all()

    sent_count = 0
    failed_teams = []
    skipped_no_emails = 0
    skipped_no_data = 0

    for team_obj in teams:
        to_list = _parse_recipients((team_obj.team_metadata or {}).get('emails', ''))

        if not to_list:
            skipped_no_emails += 1
            continue

        try:
            pdf_bytes, display_name, has_data = build_team_pdf_from_data(
                team_obj.team_number, all_teams_data, team_stats, red_stats,
                ir_feedback, field_avg_ir_count, ir_recovery_benchmark,
                ir_top_recovery_pct, blue_placements, red_placements,
                num_active_teams)
            if not has_data:
                skipped_no_data += 1
                continue

            success = send_email(
                subject=subject,
                body=_apply_message_template(message, team_obj),
                to=to_list,
                attachments=[(f"{sanitize_filename(display_name)}.pdf", pdf_bytes)],
                sent_by=user.get('username', 'UNKNOWN')
            )
        except Exception as e:
            current_app.logger.error(f'Failed to email team {team_obj.team_number}: {str(e)}')
            success = False

        if success:
            sent_count += 1
        else:
            failed_teams.append(team_obj.team_number)

    # Build a summary flash (mirrors extAccounts accounting)
    parts = [f'Emailed {sent_count} team(s).']
    if skipped_no_emails:
        parts.append(f'{skipped_no_emails} skipped (no "emails" in metadata).')
    if skipped_no_data:
        parts.append(f'{skipped_no_data} skipped (no Quotient data).')
    if failed_teams:
        flash('Failed to email team(s): ' + ', '.join(map(str, failed_teams)), 'error')

    if sent_count:
        flash(' '.join(parts), 'success')
    elif not failed_teams:
        flash('No teams were emailed. ' + (' '.join(parts[1:]) or 'No eligible teams found.'), 'info')

    return redirect(url_for('scorecard.scorecard_dashboard'))