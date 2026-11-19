# routes/extAccounts.py
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app, jsonify, send_file
from models import db, AuditLog, extAccounts, Team
from routes.decorators import login_required, role_required
from utils.smtp_client import send_email
import string
import secrets
import random
import re
import csv
import io
import zipfile
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.pdfgen import canvas

# Create the blueprint
ext_accounts_bp = Blueprint('ext_accounts', __name__, url_prefix='/externalAccounts')

def build_teams_data(accounts):
    """
    Group extAccounts rows into the structure generate_pdf_content() expects.

    Handles grouping, sorting, and the team relationship lookup so every
    caller gets identical output regardless of query order.
    """
    teams = {}

    for acc in accounts:
        if acc.team_number not in teams:
            team_obj = acc.team
            teams[acc.team_number] = {
                'team_number': acc.team_number,
                'team_name': team_obj.team_name if team_obj else 'Unknown',
                'school_name': (team_obj.school_name or '') if team_obj else '',
                'accounts': {},
            }

        teams[acc.team_number]['accounts'].setdefault(acc.account_name, []).append({
            'username': acc.account_username,
            'password': acc.account_password,
            'url': acc.account_url,
        })

    # Deterministic output: teams by number, systems alphabetically,
    # usernames alphabetically within each system.
    teams_data = [teams[num] for num in sorted(teams)]
    for team in teams_data:
        team['accounts'] = {
            name: sorted(lst, key=lambda x: x['username'].lower())
            for name, lst in sorted(team['accounts'].items())
        }

    return teams_data

def get_phonetic_password(password):
    """Converts a password string into a phonetic representation for easier verbal communication."""
    phonetic_map = {
        'a': 'Alpha', 'b': 'Bravo', 'c': 'Charlie', 'd': 'Delta', 'e': 'Echo',
        'f': 'Foxtrot', 'g': 'Golf', 'h': 'Hotel', 'i': 'India', 'j': 'Juliet',
        'k': 'Kilo', 'l': 'Lima', 'm': 'Mike', 'n': 'November', 'o': 'Oscar',
        'p': 'Papa', 'q': 'Quebec', 'r': 'Romeo', 's': 'Sierra', 't': 'Tango',
        'u': 'Uniform', 'v': 'Victor', 'w': 'Whiskey', 'x': 'X-ray', 'y': 'Yankee', 'z': 'Zulu',
        '0': 'Zero', '1': 'One', '2': 'Two', '3': 'Three', '4': 'Four',
        '5': 'Five', '6': 'Six', '7': 'Seven', '8': 'Eight', '9': 'Nine',
        '!': 'Bang', '@': 'At', '#': 'Hash', '$': 'Dollar', '%': 'Percent',
        '^': 'Caret', '&': 'Ampersand', '*': 'Star', '(': 'Open', ')': 'Close'
    }
    
    result = []
    for char in password:
        lower_char = char.lower()
        if lower_char in phonetic_map:
            word = phonetic_map[lower_char]
            if char.isupper():
                word = word.upper()
            result.append(word)
        else:
            result.append(char.upper())
    
    return " - ".join(result)

def generate_pdf_content(teams_data, title="External Accounts Master List"):
    """Generates a PDF byte stream from a list of team data dictionaries."""
    buffer = io.BytesIO()
    
    # CHANGE 1: Use Landscape orientation (11 x 8.5 inches)
    from reportlab.lib.pagesizes import landscape, letter
    #page_size = landscape(letter) 
    page_size = letter
    
    doc = SimpleDocTemplate(buffer, pagesize=page_size)
    styles = getSampleStyleSheet()
    story = []

    # Custom Styles
    title_style = ParagraphStyle('CustomTitle', parent=styles['Heading1'], fontSize=24, spaceAfter=30, alignment=TA_CENTER)
    
    # CHANGE 2: Improved School Name styling
    team_header_style = ParagraphStyle('TeamHeader', parent=styles['Heading2'], fontSize=18, spaceAfter=5, textColor='#0d6efd', leading=20)
    sub_header_style = ParagraphStyle('SubHeader', parent=styles['Normal'], fontSize=12, spaceAfter=25, textColor='#555555', leading=14, leftIndent=10)
    
    # Account Header Style
    acc_header_style = ParagraphStyle('AccHeader', parent=styles['Heading3'], fontSize=14, spaceAfter=5, textColor='#333333')
    url_style = ParagraphStyle('UrlStyle', parent=styles['Normal'], fontSize=10, spaceAfter=15, textColor='#007bff', leading=12)

    # Cell style for table content with wrapping
    cell_style = ParagraphStyle('CellStyle', parent=styles['Normal'], fontSize=9, leading=11)
    cell_style_header = ParagraphStyle('CellHeaderStyle', parent=styles['Normal'], fontSize=10, leading=12, fontName='Helvetica-Bold')

    # Title
    #story.append(Paragraph(title, title_style))
    #story.append(Spacer(1, 20))

    for team in teams_data:
        # Team Header
        story.append(Paragraph(f"Team {team['team_number']}", team_header_style))
        
        # CHANGE 3: Better School Name Display
        # if team['school_name']:
        #     # Wrap school name in parentheses and add bolding for clarity
        #     story.append(Paragraph(f"<b>School:</b> {team['school_name']}", sub_header_style))
        # else:
        #     # Optional: Add a placeholder if no school name
        #     story.append(Paragraph("<i>School: Not listed</i>", sub_header_style))
        
        # Group by Account Name
        for acc_name, accounts in team['accounts'].items():
            if not accounts:
                continue
            
            # Create a temporary list for this account's content
            account_content = []
            
            account_content.append(Paragraph(f"System: {acc_name}", acc_header_style))
            if accounts[0].get('url'):
                account_content.append(Paragraph(f"URL: {accounts[0]['url']}", url_style))
            
            # Prepare Table Data
            import html

            data = [['Username', 'Password', 'Phonetic']]
            for acc in accounts:
                # Escape HTML entities to prevent parsing errors from special chars in passwords
                safe_username = html.escape(str(acc['username']))
                safe_password = html.escape(str(acc['password']))
                
                phonetic = get_phonetic_password(acc['password'])
                # The phonetic function returns safe text (words), but escaping it again is safe
                safe_phonetic = html.escape(str(phonetic))
                
                # Use Paragraph for wrapping
                phonetic_cell = Paragraph(safe_phonetic, cell_style)
                data.append([
                    Paragraph(safe_username, cell_style), 
                    Paragraph(safe_password, cell_style), 
                    phonetic_cell
                ])
            
            # Create Table
            col_widths = [1 * inch, 1.8 * inch, 4.3 * inch]
            t = Table(data, colWidths=col_widths)
            t.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), '#f8f9fa'),
                ('TEXTCOLOR', (0, 0), (-1, 0), '#000000'),
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
                ('TOPPADDING', (0, 1), (-1, -1), 6),
                ('BOTTOMPADDING', (0, 1), (-1, -1), 6),
                ('BACKGROUND', (0, 1), (-1, -1), '#ffffff'),
                ('GRID', (0, 0), (-1, -1), 1, '#dee2e6'),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('RIGHTPADDING', (0, 0), (-1, -1), 4),
                ('LEFTPADDING', (0, 0), (-1, -1), 4),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), ['#ffffff', '#f9f9f9']),
            ]))
            
            # Add table to account content
            account_content.append(t)
            account_content.append(Spacer(1, 15))
            
            # WRAP IN KeepTogether to prevent splitting
            from reportlab.platypus import KeepTogether
            story.append(KeepTogether(account_content))
            
            # Force a new page after EVERY account name
            #story.append(PageBreak())
        
        # Force new page for next team
        story.append(PageBreak())

    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()

def generate_csv_content(accounts):
    """Generates a CSV byte stream, sorted by username."""
    # Sort accounts by username to ensure consistent ordering
    sorted_accounts = sorted(accounts, key=lambda a: a['username'])
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Username', 'Password'])
    for acc in sorted_accounts:
        writer.writerow([acc['username'], acc['password']])
    return output.getvalue().encode('utf-8')

def generate_random_password(length=16, use_uppercase=True, use_lowercase=True, use_digits=True, use_special=True):
    """Generate a cryptographically secure random password with customizable complexity"""
    alphabet = ""
    if use_uppercase:
        alphabet += string.ascii_uppercase
    if use_lowercase:
        alphabet += string.ascii_lowercase
    if use_digits:
        alphabet += string.digits
    if use_special:
        alphabet += "!@#$%^&*"

    if not alphabet:
        raise ValueError("At least one character type must be selected.")

    password = ''.join(secrets.choice(alphabet) for _ in range(length))

    # Ensure at least one of each selected type is present
    if use_uppercase and not any(c.isupper() for c in password):
        password += secrets.choice(string.ascii_uppercase)
    if use_lowercase and not any(c.islower() for c in password):
        password += secrets.choice(string.ascii_lowercase)
    if use_digits and not any(c.isdigit() for c in password):
        password += secrets.choice(string.digits)
    if use_special and not any(c in "!@#$%^&*" for c in password):
        password += secrets.choice("!@#$%^&*")

    # Shuffle the password
    password_list = list(password)
    random.shuffle(password_list)
    return ''.join(password_list)

def generate_username_pattern(pattern, team_num, count):
    """
    Generate usernames using a template pattern.
    
    Supported placeholders (all optional):
    - {team} - Team number as-is (e.g., 1, 2, 10)
    - {team:02} - Zero-padded to 2 digits (e.g., 01, 02)
    - {team:03} - Zero-padded to 3 digits (e.g., 001, 002)
    - {letter} - Single letter (a, b, c...)
    - {number} - Sequential number (1, 2, 3...)
    
    Combinations:
    - {letter}{number} -> a1, a2, b1, b2...
    - {number}{letter} -> 1a, 2a, 1b, 2b...
    
    Examples:
    - "team{team}{letter}" → team1a, team1b, team2a
    - "team{team}{number}" → team11, team12, team21
    - "team{team}{letter}{number}" → team1a1, team1a2, team1b1
    - "user{number}" → user1, user2, user3
    """
    usernames = []
    
    # Validate count
    if count < 1 or count > 676 * 100:  # 26*26 letters * 100 numbers max
        raise ValueError("Count must be between 1 and 67600")
    
    has_letter = '{letter}' in pattern.lower()
    has_number = '{number}' in pattern.lower()
    
    # If neither is present, we can only generate 1 unique username per team
    if not has_letter and not has_number:
        if count > 1:
            # Warn later, but generate duplicates
            pass
    
    # Generate sequences
    letters = []
    numbers = []
    
    if has_letter:
        if count <= 26:
            letters = string.ascii_lowercase[:count]
        else:
            # Double letter combinations
            letters = []
            for first in string.ascii_lowercase:
                for second in string.ascii_lowercase:
                    if len(letters) >= count:
                        break
                    letters.append(first + second)
                if len(letters) >= count:
                    break
    
    if has_number:
        # Determine how many numbers we need
        # If we have letters, we need count / len(letters) numbers (rounded up)
        # If no letters, we need 'count' numbers
        if has_letter:
            num_groups = (count + len(letters) - 1) // len(letters)
        else:
            num_groups = count
        
        numbers = list(range(1, num_groups + 1))
    
    # Parse the pattern for team formatting
    def replace_team(match):
        format_spec = match.group(1) or ''
        if format_spec.startswith('0'):
            width = int(format_spec[1:])
            return f"{team_num:0{width}d}"
        return str(team_num)
    
    processed_pattern = re.sub(r'\{team(?::(\d+))?\}', replace_team, pattern)
    
    # Generate usernames
    for i in range(count):
        username = processed_pattern
        
        if has_letter and has_number:
            # Letter cycles first, number increments when letters exhaust
            letter_i = i % len(letters)
            number_i = i // len(letters)
            username = username.replace('{letter}', letters[letter_i])
            username = username.replace('{number}', str(number_i + 1))
        elif has_letter:
            if i < len(letters):
                username = username.replace('{letter}', letters[i])
            else:
                username = username.replace('{letter}', letters[-1])
        elif has_number:
            username = username.replace('{number}', str(i + 1))
        
        usernames.append(username)
    
    return usernames

def _build_team_pdf(team_obj):
    """Build the PDF bytes for a single team. Returns (pdf_bytes, has_accounts)."""
    accounts = extAccounts.query.filter_by(team_number=team_obj.team_number)\
        .order_by(extAccounts.account_name).all()

    teams_data = build_teams_data(accounts)
    pdf_bytes = generate_pdf_content(teams_data, f"Accounts for Team {team_obj.team_number}")
    return pdf_bytes, bool(accounts)


def _apply_message_template(message, team_obj):
    """Replace {schoolName} and {teamNum} placeholders in a message body."""
    return (message or '') \
        .replace('{schoolName}', (team_obj.school_name or '').strip()) \
        .replace('{teamNum}', str(team_obj.team_number))


def _parse_recipients(to_field):
    """Split a comma-separated recipient string into a clean list."""
    if not to_field:
        return []
    return [addr.strip() for addr in to_field.split(',') if addr.strip()]

@ext_accounts_bp.route('/', methods=['GET'])
@login_required
@role_required('admin')
def ext_accounts_list():
    """List all external accounts grouped by Account Name -> Team"""
    user = session.get('user_info', {})
    user_roles = session.get('user_roles', [])
    
    accounts = extAccounts.query.order_by(
        extAccounts.account_name, 
        extAccounts.team_number
    ).all()
    
    grouped_by_account = {}
    
    for account in accounts:
        acc_name = account.account_name
        if acc_name not in grouped_by_account:
            grouped_by_account[acc_name] = {
                'account_name': acc_name,
                'account_url': account.account_url,
                'teams': {}
            }
        
        team_num = account.team_number
        if team_num not in grouped_by_account[acc_name]['teams']:
            grouped_by_account[acc_name]['teams'][team_num] = []
        
        grouped_by_account[acc_name]['teams'][team_num].append(account)
    
    active_teams = Team.query.filter_by(is_active=True).order_by(Team.team_number).all()
    
    distinct_names = sorted(grouped_by_account.keys())

    team_emails = {}
    for team in active_teams:
        team_emails[team.team_number] = (team.team_metadata or {}).get('emails', '') or ''

    return render_template('extAccounts.html',
                         is_admin_page=True,
                         grouped_accounts=grouped_by_account,
                         active_teams=active_teams,
                         distinct_names=distinct_names,
                         team_emails=team_emails)

@ext_accounts_bp.route('/preview-username', methods=['POST'])
@login_required
@role_required('admin')
def preview_username():
    """AJAX endpoint to preview username pattern generation"""
    try:
        data = request.json or {}
        pattern = data.get('pattern', '')
        team_num = int(data.get('team_num', 1))
        count = int(data.get('count', 5))
        
        if not pattern:
            return jsonify({'error': 'Pattern is required'}), 400
        
        if count < 1 or count > 67600:
            return jsonify({'error': 'Count must be between 1 and 67600'}), 400
        
        has_letter = '{letter}' in pattern.lower()
        has_number = '{number}' in pattern.lower()
        
        if not has_letter and not has_number:
            if count > 1:
                warning = "Warning: Pattern has no {letter} or {number}. Generated usernames will be duplicates."
            else:
                warning = None
        elif not has_letter and has_number:
            if count > 100:
                warning = f"Warning: Using only {number} with count={count}. Numbers will go up to {count}."
            else:
                warning = None
        elif has_letter and not has_number:
            if count > 676:
                warning = f"Warning: Using only {letter} with count={count}. Letters will use double combinations (aa, ab...)."
            else:
                warning = None
        else:
            warning = None
        
        usernames = generate_username_pattern(pattern, team_num, min(count, 10))
        
        return jsonify({
            'success': True,
            'usernames': usernames,
            'total_possible': count,
            'pattern': pattern,
            'team_num': team_num,
            'warning': warning
        })
    
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        current_app.logger.error(f'Username preview error: {str(e)}')
        return jsonify({'error': 'Failed to generate preview'}), 500

@ext_accounts_bp.route('/generate', methods=['POST'])
@login_required
@role_required('admin')
def generate_accounts():
    """Generate new external accounts with improved pattern validation"""
    user = session.get('user_info', {})
    
    try:
        scope = request.form.get('scope')
        team_number = request.form.get('team_number', type=int) if scope == 'specific' else None
        account_name = request.form.get('account_name')
        account_url = request.form.get('account_url')
        count_per_team = request.form.get('count_per_team', type=int)
        username_pattern = request.form.get('username_pattern')
        password_length = request.form.get('password_length', type=int, default=16)
        use_shared_password = 'use_shared_password' in request.form
        use_upper = 'use_upper' in request.form
        use_lower = 'use_lower' in request.form
        use_digit = 'use_digit' in request.form
        use_special = 'use_special' in request.form

        # Validation
        if password_length < 4:
            flash('Password length must be at least 4.', 'error')
            return redirect(url_for('ext_accounts.ext_accounts_list'))
        if not (use_upper or use_lower or use_digit or use_special):
            flash('Select at least one character type.', 'error')
            return redirect(url_for('ext_accounts.ext_accounts_list'))
        
        if not account_name or not count_per_team or not username_pattern:
            flash('Please fill in all required fields.', 'error')
            return redirect(url_for('ext_accounts.ext_accounts_list'))
        
        if count_per_team < 1 or count_per_team > 67600:
            flash('Account count must be between 1 and 67600.', 'error')
            return redirect(url_for('ext_accounts.ext_accounts_list'))
        
        has_letter = '{letter}' in username_pattern.lower()
        has_number = '{number}' in username_pattern.lower()
        
        if not has_letter and not has_number and count_per_team > 1:
            flash('Warning: Pattern has no {letter} or {number}. This will create duplicate usernames.', 'warning')
        
        # Test pattern
        try:
            test_usernames = generate_username_pattern(username_pattern, 1, min(count_per_team, 3))
            if not test_usernames or len(test_usernames) != min(count_per_team, 3):
                raise ValueError("Pattern generation failed")
        except Exception as e:
            flash(f'Invalid pattern: {str(e)}.', 'error')
            return redirect(url_for('ext_accounts.ext_accounts_list'))
        
        if scope == 'all':
            teams = Team.query.filter_by(is_active=True).order_by(Team.team_number).all()
        elif scope == 'specific' and team_number:
            teams = Team.query.filter_by(team_number=team_number, is_active=True).first()
            teams = [teams] if teams else []
        else:
            flash('Invalid scope or team selection.', 'error')
            return redirect(url_for('ext_accounts.ext_accounts_list'))
        
        if not teams:
            flash('No active teams found.', 'error')
            return redirect(url_for('ext_accounts.ext_accounts_list'))
        
        created_count = 0
        for team in teams:
            try:
                usernames = generate_username_pattern(username_pattern, team.team_number, count_per_team)
            except Exception as e:
                flash(f'Error generating usernames for Team {team.team_number}: {str(e)}', 'error')
                continue
            
            # Generate shared password once per team if enabled
            shared_password = None
            if use_shared_password:
                shared_password = generate_random_password(
                    length=password_length,
                    use_uppercase=use_upper,
                    use_lowercase=use_lower,
                    use_digits=use_digit,
                    use_special=use_special
                )
            
            for username in usernames:
                # Use shared password if enabled, otherwise generate unique one
                if use_shared_password:
                    password = shared_password
                else:
                    password = generate_random_password(
                        length=password_length,
                        use_uppercase=use_upper,
                        use_lowercase=use_lower,
                        use_digits=use_digit,
                        use_special=use_special
                    )
                
                existing = extAccounts.query.filter_by(
                    team_number=team.team_number,
                    account_name=account_name,
                    account_username=username
                ).first()
                
                if existing:
                    continue
                
                new_account = extAccounts(
                    team_number=team.team_number,
                    account_name=account_name,
                    account_url = account_url if account_url.strip() else None,
                    account_username=username,
                    account_password=password
                )
                
                db.session.add(new_account)
                db.session.flush()
                
                audit_entry = AuditLog(
                    username=user.get('username', 'UNKNOWN'),
                    action='ext_account_create',
                    entity_type='ext_account',
                    entity_id=new_account.id,
                    old_value=None,
                    new_value={
                        'team_number': team.team_number,
                        'account_name': account_name,
                        'account_url': account_url,
                        'account_username': username,
                        'password_generated': True
                    },
                )
                db.session.add(audit_entry)
                created_count += 1
        
        db.session.commit()
        flash(f'Successfully created {created_count} external account(s).', 'success')
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'External account generation error: {str(e)}')
        flash('An error occurred while generating accounts. Please try again.', 'error')
    
    return redirect(url_for('ext_accounts.ext_accounts_list'))

@ext_accounts_bp.route('/delete/<int:team_number>/<path:account_name>', methods=['POST'])
@login_required
@role_required('admin')
def delete_account_group(team_number, account_name):
    """Delete ALL accounts matching a specific team_number and account_name"""
    user = session.get('user_info', {})
    
    try:
        matching_accounts = extAccounts.query.filter_by(
            team_number=team_number,
            account_name=account_name
        ).all()
        
        if not matching_accounts:
            flash('No accounts found to delete.', 'info')
            return redirect(url_for('ext_accounts.ext_accounts_list'))
        
        deleted_count = 0
        for account in matching_accounts:
            audit_entry = AuditLog(
                username=user.get('username', 'UNKNOWN'),
                action='ext_account_group_delete',
                entity_type='ext_account_group',
                entity_id=account.id,
                old_value={
                    'team_number': account.team_number,
                    'account_name': account.account_name,
                    'account_username': account.account_username,
                    'account_url': account.account_url
                },
                new_value=None,
            )
            db.session.add(audit_entry)
            db.session.delete(account)
            deleted_count += 1
        
        db.session.commit()
        flash(f'Successfully deleted {deleted_count} account(s) for "{account_name}" (Team {team_number}).', 'success')
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'External account group deletion error: {str(e)}')
        flash('An error occurred while deleting the accounts.', 'error')
    
    return redirect(url_for('ext_accounts.ext_accounts_list'))

@ext_accounts_bp.route('/delete-account-name/<path:account_name>', methods=['POST'])
@login_required
@role_required('admin')
def delete_entire_account_name(account_name):
    """Delete ALL accounts matching a specific account_name across ALL teams"""
    user = session.get('user_info', {})
    
    try:
        # Find all accounts with this name regardless of team
        matching_accounts = extAccounts.query.filter_by(account_name=account_name).all()
        
        if not matching_accounts:
            flash(f'No accounts found for "{account_name}".', 'info')
            return redirect(url_for('ext_accounts.ext_accounts_list'))
        
        deleted_count = 0
        teams_affected = set()
        
        for account in matching_accounts:
            teams_affected.add(account.team_number)
            
            audit_entry = AuditLog(
                username=user.get('username', 'UNKNOWN'),
                action='ext_account_name_delete',
                entity_type='ext_account_name',
                entity_id=account.id,
                old_value={
                    'team_number': account.team_number,
                    'account_name': account.account_name,
                    'account_username': account.account_username,
                    'account_url': account.account_url
                },
                new_value=None,
            )
            db.session.add(audit_entry)
            db.session.delete(account)
            deleted_count += 1
        
        db.session.commit()
        
        team_count = len(teams_affected)
        team_text = f"across {team_count} team(s)" if team_count > 1 else "for this team"
        
        flash(f'Successfully deleted {deleted_count} account(s) for "{account_name}" {team_text}.', 'success')
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'Entire account name deletion error: {str(e)}')
        flash('An error occurred while deleting the accounts.', 'error')
    
    return redirect(url_for('ext_accounts.ext_accounts_list'))

@ext_accounts_bp.route('/export/master-pdf')
@login_required
@role_required('admin')
def export_master_pdf():
    """Generate a single PDF with all teams"""
    accounts = extAccounts.query.order_by(extAccounts.team_number, extAccounts.account_name).all()
    teams_data = build_teams_data(accounts)
    pdf_bytes = generate_pdf_content(teams_data, "Master External Accounts List")

    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype='application/pdf',
        as_attachment=False,
        download_name='master_external_accounts.pdf'
    )

@ext_accounts_bp.route('/export/team-pdf/<int:team_number>')
@login_required
@role_required('admin')
def export_team_pdf(team_number):
    """Generate a PDF for a specific team"""
    team_obj = Team.query.filter_by(team_number=team_number).first()
    if not team_obj:
        flash('Team not found.', 'error')
        return redirect(url_for('ext_accounts.ext_accounts_list'))
    
    accounts = extAccounts.query.filter_by(team_number=team_number).order_by(extAccounts.account_name).all()
    
    teams_data = [{
        'team_number': team_number,
        'team_name': team_obj.team_name,
        'school_name': team_obj.school_name,
        'accounts': {}
    }]
    
    for acc in accounts:
        acc_name = acc.account_name
        if acc_name not in teams_data[0]['accounts']:
            teams_data[0]['accounts'][acc_name] = []
        teams_data[0]['accounts'][acc_name].append({
            'username': acc.account_username,
            'password': acc.account_password,
            'url': acc.account_url
        })
    
    # --- SORT USERNAME LISTS BY USERNAME ---
    for acc_name, account_list in teams_data[0]['accounts'].items():
        account_list.sort(key=lambda x: x['username'].lower())
    
    pdf_bytes = generate_pdf_content(teams_data, f"Accounts for Team {team_number}")
    
    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype='application/pdf',
        as_attachment=True,
        download_name=f'team_{team_number}_accounts.pdf'
    )

@ext_accounts_bp.route('/export/master-csv')
@login_required
@role_required('admin')
def export_master_csv():
    """
    Generate a ZIP containing:
    1. Root: Master CSVs (one per account_name, containing ALL teams)
    2. Folders: Team_X/ containing CSVs specific to that team
    """
    accounts = extAccounts.query.all()
    
    # --- 1. Group by Account Name for Master CSVs ---
    master_groups = {}
    for acc in accounts:
        if acc.account_name not in master_groups:
            master_groups[acc.account_name] = []
        master_groups[acc.account_name].append(acc)
    
    # --- 2. Group by Team Number for Team Folders ---
    team_groups = {}
    for acc in accounts:
        if acc.team_number not in team_groups:
            team_groups[acc.team_number] = {}
        
        acc_name = acc.account_name
        if acc_name not in team_groups[acc.team_number]:
            team_groups[acc.team_number][acc_name] = []
        team_groups[acc.team_number][acc_name].append(acc)
    
    zip_buffer = io.BytesIO()
    
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        
        # --- A. Write Master CSVs to Root ---
        for acc_name, acc_list in master_groups.items():
            # Sort by username for consistency
            sorted_accs = sorted(acc_list, key=lambda a: a.account_username)
            
            output = io.StringIO()
            writer = csv.writer(output)
            #writer.writerow(['Username', 'Password', 'Team Number']) # Added Team Number to Master for clarity
            for acc in sorted_accs:
                #writer.writerow([acc.account_username, acc.account_password, acc.team_number])
                writer.writerow([acc.account_username, acc.account_password])
            
            csv_data = output.getvalue().encode('utf-8')
            safe_name = "".join(c if c.isalnum() or c in ('-', '_', '.') else '_' for c in acc_name)
            
            # Write to root of ZIP
            zip_file.writestr(f"{safe_name}.csv", csv_data)
        
        # --- B. Write Team-Specific CSVs to Folders ---
        for team_num in sorted(team_groups.keys()):
            team_accounts = team_groups[team_num]
            safe_team_folder = f"Team_{team_num}"
            
            for acc_name, acc_list in team_accounts.items():
                # Sort by username
                sorted_accs = sorted(acc_list, key=lambda a: a.account_username)
                
                output = io.StringIO()
                writer = csv.writer(output)
                writer.writerow(['Username', 'Password'])
                for acc in sorted_accs:
                    writer.writerow([acc.account_username, acc.account_password])
                
                csv_data = output.getvalue().encode('utf-8')
                safe_acc_name = "".join(c if c.isalnum() or c in ('-', '_', '.') else '_' for c in acc_name)
                
                # Write to Team Folder
                zip_path = f"{safe_team_folder}/{safe_acc_name}.csv"
                zip_file.writestr(zip_path, csv_data)
    
    zip_buffer.seek(0)
    return send_file(
        zip_buffer,
        mimetype='application/zip',
        as_attachment=True,
        download_name='master_external_accounts_complete.zip'
    )

@ext_accounts_bp.route('/export/team-csv/<int:team_number>')
@login_required
@role_required('admin')
def export_team_csv(team_number):
    """Generate a ZIP of CSVs for a specific team, one per account name"""
    team_obj = Team.query.filter_by(team_number=team_number).first()
    if not team_obj:
        flash('Team not found.', 'error')
        return redirect(url_for('ext_accounts.ext_accounts_list'))
    
    accounts = extAccounts.query.filter_by(team_number=team_number).all()
    
    grouped = {}
    for acc in accounts:
        if acc.account_name not in grouped:
            grouped[acc.account_name] = []
        grouped[acc.account_name].append(acc)
    
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        for acc_name, acc_list in grouped.items():
            safe_name = "".join(c if c.isalnum() or c in ('-', '_', '.') else '_' for c in acc_name)
            csv_data = generate_csv_content([{'username': a.account_username, 'password': a.account_password} for a in acc_list])
            zip_file.writestr(f"Team{team_number}_{safe_name}.csv", csv_data)
    
    zip_buffer.seek(0)
    return send_file(
        zip_buffer,
        mimetype='application/zip',
        as_attachment=True,
        download_name=f'team_{team_number}_accounts.zip'
    )

@ext_accounts_bp.route('/export/all-team-pdfs-zip')
@login_required
@role_required('admin')
def export_all_team_pdfs_zip():
    """Generate a ZIP file containing individual PDFs for every team"""
    # Fetch all active teams
    teams = Team.query.filter_by(is_active=True).order_by(Team.team_number).all()
    
    if not teams:
        flash('No active teams found to export.', 'info')
        return redirect(url_for('ext_accounts.ext_accounts_list'))

    zip_buffer = io.BytesIO()
    
    try:
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for team in teams:
                # Fetch accounts for this team
                accounts = extAccounts.query.filter_by(team_number=team.team_number).order_by(extAccounts.account_name).all()
                
                if not accounts:
                    continue

                # Prepare data structure for PDF generation
                teams_data = [{
                    'team_number': team.team_number,
                    'team_name': team.team_name,
                    'school_name': team.school_name if hasattr(team, 'school_name') else '',
                    'accounts': {}
                }]
                
                for acc in accounts:
                    acc_name = acc.account_name
                    if acc_name not in teams_data[0]['accounts']:
                        teams_data[0]['accounts'][acc_name] = []
                    teams_data[0]['accounts'][acc_name].append({
                        'username': acc.account_username,
                        'password': acc.account_password,
                        'url': acc.account_url
                    })
                
                # --- SORT USERNAME LISTS BY USERNAME ---
                for acc_name, account_list in teams_data[0]['accounts'].items():
                    account_list.sort(key=lambda x: x['username'].lower())
                
                # Generate PDF for this team
                pdf_bytes = generate_pdf_content(teams_data, f"Accounts for Team {team.team_number}")
                
                # Sanitize filename
                safe_team_name = "".join(c if c.isalnum() or c in ('-', '_', '.') else '_' for c in team.team_name)
                filename = f"Team_{team.team_number}.pdf"
                
                zip_file.writestr(filename, pdf_bytes)
        
        zip_buffer.seek(0)
        return send_file(
            zip_buffer,
            mimetype='application/zip',
            as_attachment=True,
            download_name='all_team_password_pdfs.zip'
        )
        
    except Exception as e:
        current_app.logger.error(f'Error generating team PDFs ZIP: {str(e)}')
        flash('An error occurred while generating the ZIP file.', 'error')
        return redirect(url_for('ext_accounts.ext_accounts_list'))

@ext_accounts_bp.route('/add-single', methods=['POST'])
@login_required
@role_required('admin')
def add_single_account():
    """Add a single external account with a defined password"""
    user = session.get('user_info', {})
    
    try:
        team_number = request.form.get('single_team_number', type=int)
        account_name = request.form.get('single_account_name')
        account_url = request.form.get('single_account_url', '')
        account_username = request.form.get('single_username')
        account_password = request.form.get('single_password')
        
        # Validation
        if not team_number or not account_name or not account_username or not account_password:
            flash('Team, Account Name, Username, and Password are required.', 'error')
            return redirect(url_for('ext_accounts.ext_accounts_list'))
        
        # Verify team exists
        team = Team.query.filter_by(team_number=team_number).first()
        if not team:
            flash(f'Team {team_number} does not exist.', 'error')
            return redirect(url_for('ext_accounts.ext_accounts_list'))
        
        # Check for duplicate
        existing = extAccounts.query.filter_by(
            team_number=team_number,
            account_name=account_name,
            account_username=account_username
        ).first()
        
        if existing:
            flash(f'Account "{account_username}" already exists for Team {team_number} under "{account_name}".', 'error')
            return redirect(url_for('ext_accounts.ext_accounts_list'))
        
        # Clean up URL
        account_url = account_url.strip() if account_url else None
        
        # Create the account
        new_account = extAccounts(
            team_number=team_number,
            account_name=account_name,
            account_url=account_url,
            account_username=account_username,
            account_password=account_password
        )
        
        db.session.add(new_account)
        db.session.flush()
        
        # Audit log
        audit_entry = AuditLog(
            username=user.get('username', 'UNKNOWN'),
            action='ext_account_manual_create',
            entity_type='ext_account',
            entity_id=new_account.id,
            old_value=None,
            new_value={
                'team_number': team_number,
                'account_name': account_name,
                'account_url': account_url,
                'account_username': account_username,
                'password_generated': False
            },
        )
        db.session.add(audit_entry)
        db.session.commit()
        
        flash(f'Successfully added account "{account_username}" to Team {team_number} under "{account_name}".', 'success')
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'Single account creation error: {str(e)}')
        flash('An error occurred while adding the account. Please try again.', 'error')
    
    return redirect(url_for('ext_accounts.ext_accounts_list'))

@ext_accounts_bp.route('/replicate', methods=['POST'])
@login_required
@role_required('admin')
def replicate_accounts():
    """Copy all usernames/passwords from an existing system name to a new system name."""
    user = session.get('user_info', {})

    try:
        scope = request.form.get('rep_scope')
        source_name = request.form.get('rep_source_name', '').strip()
        new_name = request.form.get('rep_new_name', '').strip()
        new_url = request.form.get('rep_new_url', '').strip() or None
        team_number = request.form.get('rep_team_number', type=int) if scope == 'specific' else None

        # Validation
        if not source_name or not new_name:
            flash('Source system name and new system name are required.', 'error')
            return redirect(url_for('ext_accounts.ext_accounts_list'))

        if source_name == new_name:
            flash('New system name must be different from the source system name.', 'error')
            return redirect(url_for('ext_accounts.ext_accounts_list'))

        # Determine which source accounts to copy
        source_query = extAccounts.query.filter_by(account_name=source_name)
        if scope == 'specific':
            if not team_number:
                flash('Please select a team for a single-team copy.', 'error')
                return redirect(url_for('ext_accounts.ext_accounts_list'))
            source_query = source_query.filter_by(team_number=team_number)
        elif scope != 'all':
            flash('Invalid scope selection.', 'error')
            return redirect(url_for('ext_accounts.ext_accounts_list'))

        source_accounts = source_query.all()

        if not source_accounts:
            flash(f'No accounts found for source system "{source_name}".', 'error')
            return redirect(url_for('ext_accounts.ext_accounts_list'))

        # Warn if the destination already has accounts for any affected team
        teams_affected = {a.team_number for a in source_accounts}
        existing_dest = extAccounts.query.filter_by(account_name=new_name).filter(
            extAccounts.team_number.in_(teams_affected)
        ).count()
        if existing_dest > 0:
            flash(
                f'Copy aborted: {existing_dest} account(s) already exist under "{new_name}" '
                'for the selected team(s). Delete them first or choose a different system name.',
                'error'
            )
            return redirect(url_for('ext_accounts.ext_accounts_list'))

        # Copy accounts (same usernames AND passwords)
        created_count = 0
        teams_copied = set()
        for account in source_accounts:
            new_account = extAccounts(
                team_number=account.team_number,
                account_name=new_name,
                account_url=new_url,
                account_username=account.account_username,
                account_password=account.account_password
            )
            db.session.add(new_account)
            db.session.flush()

            audit_entry = AuditLog(
                username=user.get('username', 'UNKNOWN'),
                action='ext_account_replicate',
                entity_type='ext_account',
                entity_id=new_account.id,
                old_value=None,
                new_value={
                    'source_team_number': account.team_number,
                    'source_account_name': source_name,
                    'new_account_name': new_name,
                    'new_account_url': new_url,
                    'account_username': account.account_username,
                    'password_copied': True
                },
            )
            db.session.add(audit_entry)
            created_count += 1
            teams_copied.add(account.team_number)

        db.session.commit()
        flash(
            f'Successfully copied {created_count} account(s) for '
            f'{len(teams_copied)} team(s) from "{source_name}" to "{new_name}".',
            'success'
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'Account replication error: {str(e)}')
        flash('An error occurred while copying accounts. Please try again.', 'error')

    return redirect(url_for('ext_accounts.ext_accounts_list'))

@ext_accounts_bp.route('/email-team/<int:team_number>', methods=['POST'])
@login_required
@role_required('admin')
def email_team(team_number):
    """Email a single team their account PDF, using the modal's To/Subject/Message."""
    user = session.get('user_info', {})

    team_obj = Team.query.filter_by(team_number=team_number).first()
    if not team_obj:
        flash('Team not found.', 'error')
        return redirect(url_for('ext_accounts.ext_accounts_list'))

    to_list = _parse_recipients(request.form.get('email_to'))
    subject = request.form.get('email_subject', '').strip()
    message = request.form.get('email_message', '')

    if not to_list:
        flash('At least one recipient address is required.', 'error')
        return redirect(url_for('ext_accounts.ext_accounts_list'))
    if not subject:
        flash('A subject is required.', 'error')
        return redirect(url_for('ext_accounts.ext_accounts_list'))

    try:
        pdf_bytes, has_accounts = _build_team_pdf(team_obj)
        if not has_accounts:
            flash(f'Team {team_number} has no external accounts to send.', 'error')
            return redirect(url_for('ext_accounts.ext_accounts_list'))

        success = send_email(
            subject=subject,
            body=_apply_message_template(message, team_obj),
            to=to_list,
            attachments=[(f'team_{team_number}_accounts.pdf', pdf_bytes)],
            sent_by=user.get('username', 'UNKNOWN')
        )
    except Exception as e:
        current_app.logger.error(f'Failed to email team {team_number}: {str(e)}')
        flash(f'An error occurred while emailing Team {team_number}.', 'error')
        return redirect(url_for('ext_accounts.ext_accounts_list'))

    if success:
        flash(f'Email sent to {len(to_list)} recipient(s) for Team {team_number}.', 'success')
    else:
        flash(f'Failed to send email for Team {team_number}. Check server logs.', 'error')

    return redirect(url_for('ext_accounts.ext_accounts_list'))


@ext_accounts_bp.route('/email-all-teams', methods=['POST'])
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
        return redirect(url_for('ext_accounts.ext_accounts_list'))

    teams = Team.query.filter_by(is_active=True).order_by(Team.team_number).all()

    sent_count = 0
    failed_teams = []
    skipped_no_emails = 0
    skipped_no_accounts = 0

    for team_obj in teams:
        to_list = _parse_recipients((team_obj.team_metadata or {}).get('emails', ''))

        if not to_list:
            skipped_no_emails += 1
            continue

        try:
            pdf_bytes, has_accounts = _build_team_pdf(team_obj)
            if not has_accounts:
                skipped_no_accounts += 1
                continue

            success = send_email(
                subject=subject,
                body=_apply_message_template(message, team_obj),
                to=to_list,
                attachments=[(f"team_{team_obj.team_number}_accounts.pdf", pdf_bytes)],
                sent_by=user.get('username', 'UNKNOWN')
            )
        except Exception as e:
            current_app.logger.error(f'Failed to email team {team_obj.team_number}: {str(e)}')
            success = False

        if success:
            sent_count += 1
        else:
            failed_teams.append(team_obj.team_number)

    # Build a summary flash
    parts = [f'Emailed {sent_count} team(s).']
    if skipped_no_emails:
        parts.append(f'{skipped_no_emails} skipped (no "emails" in metadata).')
    if skipped_no_accounts:
        parts.append(f'{skipped_no_accounts} skipped (no accounts).')
    if failed_teams:
        flash('Failed to email team(s): ' + ', '.join(map(str, failed_teams)), 'error')

    if sent_count:
        flash(' '.join(parts), 'success')
    elif not failed_teams:
        flash('No teams were emailed. ' + ' '.join(parts[1:]) or 'No eligible teams found.', 'info')

    return redirect(url_for('ext_accounts.ext_accounts_list'))