# routes/api.py
from flask import Blueprint, jsonify, g, request, current_app, Response
from routes.decorators import api_key_required
from models import db, Score, Team, AuditLog, extAccounts
from sqlalchemy.exc import IntegrityError
from utils.valkey_client import get_valkey_client
import json
import csv
import io

# Create the blueprint
api_bp = Blueprint('api', __name__, url_prefix='/api')

@api_bp.route('/status', methods=['GET'])
@api_key_required()
def api_status():
    """Health check endpoint."""
    return jsonify({
        "status": "OK",
        "message": "API is operational",
        "authenticated": True
    }), 200

@api_bp.route('/red/addScore', methods=['POST'])
@api_key_required('red', 'admin')
def red_add_score():
    """
    Red Team API Endpoint to add scores.
    
    Parameters (Form Data):
    - teamNums: Comma-separated list of team numbers (e.g., "1,2,5")
    - refID: Reference ID for the score
    - score: Integer points
    - desc: Optional description (defaults to "RED API SCORE")
    """
    
    # 1. Parse Form Data
    team_nums_str = request.form.get('teamNums')
    ref_id = request.form.get('refID')
    score_str = request.form.get('score')
    description = request.form.get('desc', 'RED API SCORE')
    
    # 2. Validation
    if not team_nums_str:
        return jsonify({"error": "Missing required parameter: teamNums"}), 400
    if not ref_id:
        return jsonify({"error": "Missing required parameter: refID"}), 400
    if not score_str:
        return jsonify({"error": "Missing required parameter: score"}), 400
    
    try:
        points = int(score_str)
    except ValueError:
        return jsonify({"error": "Parameter 'score' must be an integer"}), 400

    try:
        team_numbers = [int(x.strip()) for x in team_nums_str.split(',') if x.strip()]
    except ValueError:
        return jsonify({"error": "Parameter 'teamNums' must be a comma-separated list of integers"}), 400

    if not team_numbers:
        return jsonify({"error": "No valid team numbers found in 'teamNums'"}), 400

    # 3. Process Scores
    inserted_count = 0
    skipped_teams = []
    inserted_team_numbers = []
    
    for t_num in team_numbers:
        team = Team.query.filter_by(team_number=t_num).first()
        
        if not team:
            skipped_teams.append(t_num)
            continue
        
        new_score = Score(
            team_id=team.id,
            scorer_username=f'{g.api_key_identity.prefix}_{g.api_key_identity.created_by}',
            category='red',
            points=points,
            description=description,
            reference_id=ref_id,
            is_approved=True,
            score_metadata={"source": "red_api"}
        )
        
        db.session.add(new_score)
        inserted_count += 1
        inserted_team_numbers.append(t_num)

    # 4. Commit Transaction
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Database error in red_add_score: {str(e)}")
        return jsonify({"error": "A database error occurred while adding scores. Check server logs."}), 500

    # 5. Create Audit Log Entry
    api_key = g.api_key_identity
    audit_username = f"{api_key.prefix}_{api_key.created_by}"
    
    audit_entry = AuditLog(
        username=audit_username,
        action='api_red_score_add',
        entity_type='score_batch',
        entity_id=None,
        old_value=None,
        new_value={
            "category": "red",
            "points": points,
            "reference_id": ref_id,
            "description": description,
            "teams_added": inserted_team_numbers,
            "teams_skipped": skipped_teams,
            "count": inserted_count
        }
    )
    db.session.add(audit_entry)
    
    try:
        db.session.commit()
    except Exception as e:
        current_app.logger.error(f"Failed to create audit log for red score add: {str(e)}")

    # 6. Construct Response
    response_data = {
        "success": True,
        "inserted": inserted_count,
        "skipped": len(skipped_teams),
        "details": {
            "teams_inserted": inserted_team_numbers,
            "teams_skipped": skipped_teams
        }
    }

    return jsonify(response_data), 200

@api_bp.route('/red/addRefAttack', methods=['POST'])
@api_key_required('red', 'admin')
def red_add_ref_attack():
    """
    Red Team API Endpoint to add attacks based on a Reference ID.
    
    Parameters (Form Data):
    - teamNums: Comma-separated list of team numbers (e.g., "1,2,5")
    - refID: Reference ID to lookup points and type
    - description: Optional description override (defaults to refID type)
    - srcIP: Optional source IP
    - dstIP: Optional destination IP
    - dstHost: Optional destination host
    - dstService: Optional destination service
    - notes: Optional notes
    
    Behavior:
    - Looks up refID in Valkey config (vantage:config:redScoreRefs).
    - Rejects if refID not found.
    - Creates 'red' category scores for each team.
    - Auto-approves scores.
    - Stores full submission data + lookup data in score_metadata.
    - Uses refType from config as the description.
    """
    
    # 1. Parse Form Data
    team_nums_str = request.form.get('teamNums')
    ref_id = request.form.get('refID')
    
    # Optional fields
    description_override = request.form.get('description', '').strip()
    src_ip = request.form.get('srcIP', '').strip() or None
    dst_ip = request.form.get('dstIP', '').strip() or None
    dst_host = request.form.get('dstHost', '').strip() or None
    dst_service = request.form.get('dstService', '').strip() or None
    notes = request.form.get('notes', '').strip() or None

    # 2. Validation
    if not team_nums_str:
        return jsonify({"error": "Missing required parameter: teamNums"}), 400
    if not ref_id:
        return jsonify({"error": "Missing required parameter: refID"}), 400

    try:
        team_numbers = [int(x.strip()) for x in team_nums_str.split(',') if x.strip()]
    except ValueError:
        return jsonify({"error": "Parameter 'teamNums' must be a comma-separated list of integers"}), 400

    if not team_numbers:
        return jsonify({"error": "No valid team numbers found in 'teamNums'"}), 400

    # 3. Lookup RefID Configuration using the shared utility
    vk = get_valkey_client()
    refs_key = "vantage:config:redScoreRefs"
    
    try:
        raw_refs = vk.get(refs_key)
        if not raw_refs:
            return jsonify({"error": "No reference configuration found in Valkey."}), 500
        
        refs_data = json.loads(raw_refs)
        
        if not isinstance(refs_data, dict) or ref_id not in refs_data:
            return jsonify({"error": f"refID '{ref_id}' not found in scoring configuration"}), 404
        
        ref_config = refs_data[ref_id]
        ref_type = ref_config.get('type', 'Unknown')
        ref_points = ref_config.get('points', 0)
        ref_desc_default = ref_config.get('desc', ref_type)
        
    except json.JSONDecodeError as e:
        current_app.logger.error(f"Failed to parse ref configuration: {str(e)}")
        return jsonify({"error": "Invalid reference configuration format"}), 500
    except Exception as e:
        current_app.logger.error(f"Failed to fetch ref configuration: {str(e)}")
        return jsonify({"error": "Failed to retrieve scoring configuration"}), 500

    # 4. Determine Description - Use refType from config
    # Optional description override still supported if provided
    final_description = description_override if description_override else ref_type

    # 5. Prepare Metadata
    # Store all submitted data + lookup data for audit trail
    optional_data = {k: v for k, v in {
        'srcIP': src_ip,
        'dstIP': dst_ip,
        'dstHost': dst_host,
        'dstService': dst_service,
        'notes': notes
    }.items() if v is not None}

    # 6. Process Teams & Create Scores
    inserted_count = 0
    skipped_teams = []
    inserted_team_numbers = []
    
    for t_num in team_numbers:
        team = Team.query.filter_by(team_number=t_num).first()
        
        if not team:
            skipped_teams.append(t_num)
            continue
        
        # Clone metadata for this specific entry
        score_metadata = {
            "source": "red_ref_api",
            "refID": ref_id,
            "refType": ref_type,
            "refPoints": ref_points,
            "refDesc": ref_desc_default,
            "submissionData": optional_data
        }

        new_score = Score(
            team_id=team.id,
            scorer_username=f'{g.api_key_identity.prefix}_{g.api_key_identity.created_by}',
            category='red',
            points=ref_points,
            description=final_description,
            reference_id=ref_id,
            is_approved=True,
            score_metadata=score_metadata
        )
        
        db.session.add(new_score)
        inserted_count += 1
        inserted_team_numbers.append(t_num)

    # 7. Commit Transaction
    try:
        db.session.commit()
    except IntegrityError as e:
        db.session.rollback()
        current_app.logger.error(f"Integrity error creating scores: {str(e)}")
        return jsonify({"error": "Database integrity error. Duplicate entry or constraint violation."}), 400
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Database error in red_add_ref_attack: {str(e)}")
        return jsonify({"error": "A database error occurred while adding scores. Check server logs."}), 500

    # 8. Create Audit Log Entry
    api_key = g.api_key_identity
    audit_username = f"{api_key.prefix}_{api_key.created_by}"
    
    audit_entry = AuditLog(
        username=audit_username,
        action='api_red_ref_attack_add',
        entity_type='score_batch',
        entity_id=None,
        old_value=None,
        new_value={
            "category": "red",
            "refID": ref_id,
            "points": ref_points,
            "description": final_description,
            "teams_added": inserted_team_numbers,
            "teams_skipped": skipped_teams,
            "count": inserted_count
        }
    )
    db.session.add(audit_entry)
    
    try:
        db.session.commit()
    except Exception as e:
        current_app.logger.error(f"Failed to create audit log for ref attack add: {str(e)}")

    # 9. Construct Response
    response_data = {
        "success": True,
        "inserted": inserted_count,
        "skipped": len(skipped_teams),
        "details": {
            "teams_inserted": inserted_team_numbers,
            "teams_skipped": skipped_teams,
            "refID": ref_id,
            "points": ref_points,
            "type": ref_type
        }
    }

    return jsonify(response_data), 200

@api_bp.route('/ext-accounts/<path:account_name>', methods=['GET'])
@api_key_required('admin')
def get_ext_accounts_csv(account_name):
    """
    API endpoint returning raw CSV data (no headers) for a specific external account name.
    Sorted by username. Intended for piping to other commands.
    
    Requires an API key with the 'admin' role.
    """
    try:
        # Fetch all accounts with the specified name across all teams
        # Order by username to ensure deterministic output for piping
        accounts = extAccounts.query.filter_by(account_name=account_name).order_by(
            extAccounts.account_username
        ).all()
        
        if not accounts:
            # Return empty response (200 OK) if no accounts found
            # This allows piping scripts to handle empty inputs gracefully
            return "", 200
        
        # Generate CSV content without headers
        output = io.StringIO()
        # Use lineterminator='\n' for Unix-style line endings (better for shell piping)
        writer = csv.writer(output, lineterminator='\n')
        
        for acc in accounts:
            writer.writerow([acc.account_username, acc.account_password])
        
        csv_data = output.getvalue()
        
        # Audit log every credential export. Fail closed: if the audit entry
        # cannot be written, refuse the export rather than hand out
        # credentials without a trail.
        api_key = g.api_key_identity
        audit_entry = AuditLog(
            username=f"{api_key.prefix}_{api_key.created_by}",
            action='api_ext_accounts_export',
            entity_type='ext_account',
            entity_id=None,
            old_value=None,
            new_value={
                'account_name': account_name,
                'rows_exported': len(accounts),
                'ip': request.remote_addr
            }
        )
        db.session.add(audit_entry)
        try:
            db.session.commit()
        except Exception as audit_err:
            db.session.rollback()
            current_app.logger.error(
                f"Audit log write failed for ext accounts export "
                f"(account_name={account_name}): {audit_err}"
            )
            return jsonify({'error': 'Export blocked: audit logging failed'}), 500
        
        return Response(
            csv_data,
            mimetype='text/csv',
            headers={
                'Content-Disposition': f'inline; filename="{account_name}_accounts.csv"',
                'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0'
            }
        )
        
    except Exception as e:
        current_app.logger.error(f'API export error for {account_name}: {str(e)}')
        return jsonify({'error': 'Failed to export accounts'}), 500

# NOTE: The /admin/test-email endpoint was intentionally removed.
# It previously emailed the Master External Accounts PDF (containing every
# team's credentials) to a hardcoded external address and was only ever
# intended for one-time SMTP verification. Permanent credential-storage
# features do not belong in the API surface.
