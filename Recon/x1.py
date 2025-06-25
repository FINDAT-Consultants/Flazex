from flask import Flask, request, redirect, render_template_string, url_for, jsonify, session, Blueprint, send_file
import sqlite3
from datetime import datetime, timedelta
import uuid, json
import stripe
import requests
import uuid
import os
import openai
import pandas as pd
import re
from werkzeug.utils import secure_filename
from io import BytesIO
import smtplib
import chardet
from email.mime.text import MIMEText
from functools import wraps   

app = Flask(__name__)
app.secret_key = os.urandom(24)

# Replace with your actual Stripe and MTN MoMo API keys
stripe.api_key = 'your_actual_stripe_secret_key'
momo_api_key = 'your_actual_momo_api_key'
momo_subscription_key = 'your_actual_momo_subscription_key'
momo_api_user_id = 'your_actual_momo_api_user_id'
momo_api_key_secret = 'your_actual_momo_api_key_secret'

# Flag for demo mode
DEMO_MODE = True  # Set this to False to use actual API calls

# Setup app configuration for reconciliation
app.config['UPLOAD_FOLDER'] = 'uploads/'
app.config['ALLOWED_EXTENSIONS'] = {'csv', 'xlsx'}
app.config['OPENAI_API_KEY'] = os.getenv('OPENAI_API_KEY', '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')
openai.api_key = app.config['OPENAI_API_KEY']
history = []  # Global list to store history of processed files

# Ensure upload folder exists
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

def format_currency_safe(x):
    """Safely format numbers into currency style, handling non-numeric values and NaN values."""
    try:
        if pd.isna(x):
            return ""
        return "${:,.2f}".format(float(x))
    except (ValueError, TypeError):
        return ""

def transform_data_cleaned(df):
    """Transform and clean data based on column mappings."""
    column_mapping = {
        'Transaction Date': 'Date',
        'Journal Number': 'Transaction ID',
        'USD\nDebit': 'Debit',
        'USD\nCredit': 'Credit',
        'USD\nRunning Total': 'Balance'
    }

    transformed_df = df.rename(columns=column_mapping)
    required_columns = ['Date', 'Transaction ID', 'Debit', 'Credit', 'Balance']
    for col in required_columns:
        if col not in transformed_df.columns:
            transformed_df[col] = None

    if 'Date' in transformed_df.columns:
        transformed_df['Date'] = pd.to_datetime(transformed_df['Date'], errors='coerce').dt.strftime('%m/%d/%Y')
    if 'Debit' in transformed_df.columns:
        transformed_df['Debit'] = transformed_df['Debit'].apply(format_currency_safe)
    if 'Credit' in transformed_df.columns:
        transformed_df['Credit'] = transformed_df['Credit'].apply(format_currency_safe)
    if 'Balance' in transformed_df.columns:
        transformed_df['Balance'] = transformed_df['Balance'].astype(str)
        transformed_df['Balance'] = transformed_df['Balance'].replace({' Cr': '', ' Dr': ''}, regex=True)
        transformed_df['Balance'] = transformed_df['Balance'].str.replace(',', '', regex=False).astype(float)
        transformed_df['Balance'] = transformed_df['Balance'].apply(format_currency_safe)

    transformed_df = transformed_df[required_columns]
    return transformed_df

    

def generate_report_number(company_name, username):
    unique_id = uuid.uuid4().hex[:8]
    date_str = datetime.now().strftime('%Y%m%d%H%M%S')
    report_number = f"{company_name[:3].upper()}{username[:3].upper()}{date_str}{unique_id}"
    return report_number

def process_files(bank_file_path, cash_file_path, user_email):
    # Reading CSV files
    bank_df = pd.read_csv(bank_file_path)
    cash_df = pd.read_csv(cash_file_path)

    # Cleaning data
    bank_df['Debit'] = bank_df['Debit'].replace('[\$,]', '', regex=True).astype(float)
    bank_df['Credit'] = bank_df['Credit'].replace('[\$,]', '', regex=True).astype(float)
    cash_df['Debit'] = cash_df['Debit'].replace('[\$,]', '', regex=True).astype(float)
    cash_df['Credit'] = cash_df['Credit'].replace('[\$,]', '', regex=True).astype(float)

    # Extracting closing balances and converting to float
    closing_balance_bank_float = float(bank_df.iloc[-1]['Balance'].replace('$', '').replace(',', ''))
    closing_balance_cash_float = float(cash_df.iloc[-1]['Balance'].replace('$', '').replace(',', ''))

    # Mark transactions as unmatched initially
    bank_df['Matched'] = False
    cash_df['Matched'] = False

    # Function to match transactions
    def match_transactions(df1, col1, df2, col2):
        for i, row1 in df1.iterrows():
            if not row1['Matched']:
                for j, row2 in df2.iterrows():
                    if not row2['Matched'] and row1[col1] == row2[col2]:
                        df1.at[i, 'Matched'] = True
                        df2.at[j, 'Matched'] = True
                        break

    # Matching transactions
    match_transactions(cash_df, 'Debit', bank_df, 'Credit')
    match_transactions(cash_df, 'Credit', bank_df, 'Debit')

    # Identifying unmatched transactions
    unmatched_cash_debits = cash_df[cash_df['Debit'].notna() & ~cash_df['Matched']]
    unmatched_bank_credits = bank_df[bank_df['Credit'].notna() & ~bank_df['Matched']]
    unmatched_cash_credits = cash_df[cash_df['Credit'].notna() & ~cash_df['Matched']]
    unmatched_bank_debits = bank_df[bank_df['Debit'].notna() & ~bank_df['Matched']]

    # Calculating totals for unmatched transactions
    deposit_in_transit = unmatched_cash_debits['Debit'].sum()
    outstanding_checks = unmatched_cash_credits['Credit'].sum()
    receivable_collected_by_bank = unmatched_bank_credits['Credit'].sum()
    service_charges = unmatched_bank_debits['Debit'].sum()

    # List transactions under each category
    def get_details(df, columns):
        if 'Transaction ID' in df.columns:
            df['Short Transaction ID'] = df['Transaction ID'].apply(lambda x: str(x)[:10] + '...' if len(str(x)) > 10 else str(x))
            columns[columns.index('Transaction ID')] = 'Short Transaction ID'
            return df[columns].to_dict(orient='records')
        else:
            columns.remove('Transaction ID')
            return df[columns].to_dict(orient='records')

    deposit_in_transit_details = get_details(unmatched_cash_debits, ['Date', 'Debit', 'Transaction ID'])
    outstanding_checks_details = get_details(unmatched_cash_credits, ['Date', 'Credit', 'Transaction ID'])
    receivable_collected_by_bank_details = get_details(unmatched_bank_credits, ['Date', 'Credit', 'Transaction ID'])
    service_charges_details = get_details(unmatched_bank_debits, ['Date', 'Debit', 'Transaction ID'])

    # Calculating adjusted balances
    adjusted_bank_balance = closing_balance_bank_float + deposit_in_transit - outstanding_checks
    adjusted_cash_balance = closing_balance_cash_float + receivable_collected_by_bank - service_charges

    # Create an Excel writer and write the data
    output_excel_path = os.path.join(app.config['UPLOAD_FOLDER'], 'reconciliation_output.xlsx')
    with pd.ExcelWriter(output_excel_path) as writer:
        unmatched_cash_debits.to_excel(writer, sheet_name='Unmatched Cash Debits')
        unmatched_bank_credits.to_excel(writer, sheet_name='Unmatched Bank Credits')

    # Formatting output
    bank_statement_output = f"""Bank Statement\n- Balance as per bank statement: ${closing_balance_bank_float:,.2f}\n- Add: Deposit in transit: ${deposit_in_transit:,.2f}\n- Deduct: Outstanding checks: ${outstanding_checks:,.2f}\n- Adjusted bank balance: ${adjusted_bank_balance:,.2f}"""
    cash_ledger_output = f"""Cash Ledger\n- Balance as per Cash record: ${closing_balance_cash_float:,.2f}\n- Add: Receivable collected by bank: ${receivable_collected_by_bank:,.2f}\n- Interest earned: $0.00\n- Deduction: NSF check: $0.00\n- Service charges: ${service_charges:,.2f}\n- Error on check: $0.00\n- Adjusted cash balance: ${adjusted_cash_balance:,.2f}"""

    # Append details of transactions to the outputs
    def format_details(details):
        return '\n'.join([f"  {d['Date']}: {d.get('Short Transaction ID', 'N/A')} - ${d['Debit'] if 'Debit' in d else d['Credit']}" for d in details])

    bank_statement_output += f"\n\nDeposit in transit details:\n{format_details(deposit_in_transit_details)}"
    bank_statement_output += f"\n\nOutstanding checks details:\n{format_details(outstanding_checks_details)}"
    cash_ledger_output += f"\n\nReceivable collected by bank details:\n{format_details(receivable_collected_by_bank_details)}"
    cash_ledger_output += f"\n\nService charges details:\n{format_details(service_charges_details)}"

    # Generate report number
    company_name = session.get('company_name')
    username = session.get('username')
    report_number = generate_report_number(company_name, username)

    # Store the report in the database
    conn = sqlite3.connect('database.db')
    conn.execute('''
        INSERT INTO reconciliation_reports (
            user_email, bank_file, cash_file, bank_statement_output, cash_ledger_output, 
            adjusted_bank_balance, adjusted_cash_balance, prepared_by, company_name, report_number, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
    ''', (
        user_email, os.path.basename(bank_file_path), os.path.basename(cash_file_path), bank_statement_output, 
        cash_ledger_output, adjusted_bank_balance, adjusted_cash_balance, user_email, company_name, report_number
    ))
    report_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]

    # Add signatories (Prepared by is signed automatically)
    conn.execute('''
        INSERT INTO signatories (report_id, role, email, signed) VALUES (?, ?, ?, ?)
    ''', (report_id, 'Prepared by', user_email, True))
    conn.commit()
    conn.close()

    return bank_statement_output, cash_ledger_output, adjusted_bank_balance, adjusted_cash_balance, report_id

def get_momo_access_token():
    url = "https://sandbox.momodeveloper.mtn.com/collection/token/"
    headers = {
        'Authorization': f'Basic {momo_api_key_secret}',
        'Ocp-Apim-Subscription-Key': momo_subscription_key,
        'Content-Type': 'application/json'
    }
    response = requests.post(url, headers=headers)
    response.raise_for_status()
    return response.json()['access_token']

def init_sqlite_db():
    conn = sqlite3.connect('database.db')
    print("Opened database successfully")

    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL,
            company_name TEXT NOT NULL,
            username TEXT NOT NULL UNIQUE,
            subscription_expiry DATE
        )
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            payment_method TEXT NOT NULL,
            plan TEXT NOT NULL,
            amount REAL NOT NULL,
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS reconciliation_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_email TEXT NOT NULL,
            bank_file TEXT NOT NULL,
            cash_file TEXT NOT NULL,
            bank_statement_output TEXT NOT NULL,
            cash_ledger_output TEXT NOT NULL,
            adjusted_bank_balance REAL NOT NULL,
            adjusted_cash_balance REAL NOT NULL,
            prepared_by TEXT NOT NULL,
            reviewed_by TEXT,
            approved_by TEXT,
            company_name TEXT NOT NULL,
            report_number TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS signatories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            email TEXT,
            whatsapp TEXT,
            signed BOOLEAN DEFAULT FALSE,
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(report_id) REFERENCES reconciliation_reports(id)
        )
    ''')

    print("Tables created successfully")
    conn.close()

init_sqlite_db()


def _init_api_table() -> None:
    with sqlite3.connect('database.db') as conn:
        # main table
        conn.execute('''
            CREATE TABLE IF NOT EXISTS api_keys (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                email        TEXT    NOT NULL,
                api_key      TEXT    NOT NULL UNIQUE,
                plan         TEXT    NOT NULL,          -- pending | api_monthly | api_annual
                usage_count  INTEGER DEFAULT 0,
                created      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires      TIMESTAMP                  -- NULL until plan is purchased
            );
        ''')
        # simple payments ledger (if you don’t already have one)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS payments (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                email          TEXT,
                payment_method TEXT,
                plan           TEXT,
                amount         REAL,
                paid_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')
_init_api_table()

# ----------------------------------------------------------------------
# 1)  Decorator – verifies key, enforces quota + expiry
# ----------------------------------------------------------------------
def _verify_key(f):
    @wraps(f)
    def _wrap(*a, **kw):
        key = request.headers.get('X-API-KEY')
        if not key:
            return jsonify(error='Missing X‑API‑KEY header'), 401

        with sqlite3.connect('database.db') as conn:
            row = conn.execute(
                "SELECT plan, usage_count, expires FROM api_keys WHERE api_key=?",
                (key,)
            ).fetchone()

            if not row:
                return jsonify(error='Invalid API key'), 403

            plan, used, expires = row

            # --- expiry check -------------------------------------------------
            if expires and datetime.utcnow() > datetime.fromisoformat(expires):
                return jsonify(error='API key expired'), 403

            # --- plan quotas --------------------------------------------------
            limit = 10_000 if plan == 'api_monthly' else 120_000  # annual = 120 k
            if used >= limit:
                return jsonify(error='Monthly quota exceeded'), 429

            # --- increment usage ---------------------------------------------
            conn.execute(
                "UPDATE api_keys SET usage_count = usage_count + 1 WHERE api_key=?",
                (key,)
            )
        return f(*a, **kw)
    return _wrap

# ----------------------------------------------------------------------
# 2)  Flask Blueprint  – public endpoints
# ----------------------------------------------------------------------
proatr_api = Blueprint('proatr_api', __name__, url_prefix='/api/v1')

@proatr_api.get('/health')
def _health():
    return {'status': 'ok'}

@proatr_api.post('/transform-data')
@_verify_key
def _transform_data():
    if 'file' not in request.files:
        return jsonify(error='file field required'), 400
    f = request.files['file']
    if not allowed_file(f.filename):
        return jsonify(error='File type not allowed'), 400
    df = (pd.read_excel(f) if f.filename.lower().endswith(('xls', 'xlsx'))
          else pd.read_csv(f))
    out = transform_data_cleaned(df)
    return out.to_dict(orient='records')

@proatr_api.post('/reconcile')
@_verify_key
def _reconcile():
    bank, cash = request.files.get('bank_file'), request.files.get('cash_file')
    if not bank or not cash:
        return jsonify(error='bank_file and cash_file required'), 400
    up = app.config.get('UPLOAD_FOLDER', '/tmp')
    bank_path = os.path.join(up, uuid.uuid4().hex + '_' + bank.filename)
    cash_path = os.path.join(up, uuid.uuid4().hex + '_' + cash.filename)
    bank.save(bank_path)
    cash.save(cash_path)
    bank_out, cash_out, adj_b, adj_c, _ = process_files(
        bank_path, cash_path, user_email='api')
    return {
        'bank_statement': bank_out,
        'cash_ledger': cash_out,
        'adjusted_bank_balance': adj_b,
        'adjusted_cash_balance': adj_c
    }

app.register_blueprint(proatr_api)

# ----------------------------------------------------------------------
# 3)  Dark‑mode   /settings/api   (Generate ➜ Buy ➜ Use)
# ----------------------------------------------------------------------
@app.route('/settings/api', methods=['GET', 'POST'])
def api_settings():
    if 'username' not in session:
        return redirect(url_for('login'))

    email = session['username']

    if request.method == 'POST':
        action = request.form.get('action')
        with sqlite3.connect('database.db') as conn:
            if action == 'generate':
                conn.execute('''
                    INSERT INTO api_keys (email, api_key, plan)
                    VALUES (?,?,?)
                ''', (email, uuid.uuid4().hex, 'pending'))
            elif action == 'revoke':
                conn.execute('''
                    DELETE FROM api_keys WHERE email=? AND api_key=?
                ''', (email, request.form['key']))
        return redirect(url_for('api_settings'))

    with sqlite3.connect('database.db') as conn:
        keys = conn.execute('''
            SELECT api_key, plan, usage_count, expires
              FROM api_keys
             WHERE email=?
          ORDER BY created DESC
        ''', (email,)).fetchall()

    return render_template_string("""
<!doctype html>
<title>ProATR API Keys</title>
<link rel=preconnect href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600&display=swap" rel="stylesheet">
<style>
:root{--bg:#202123;--panel:#2B2C2F;--accent:#10A37F;--danger:#EF4444;--text:#E0E0E0;--border:#3A3B3E}
*{box-sizing:border-box;font-family:'Inter',Arial,sans-serif}
body{margin:0;background:var(--bg);color:var(--text);padding:2rem}
h1{margin-top:0;color:#fff;font-weight:600}
.card{background:var(--panel);padding:2rem;border-radius:1rem;box-shadow:0 4px 18px #0003}
table{width:100%;border-collapse:collapse;margin-top:1rem}
th,td{padding:.75rem;border:1px solid var(--border);text-align:left}
tr:nth-child(even){background:#26272A}
button{background:var(--accent);border:none;color:#fff;padding:.55rem 1rem;
       border-radius:.5rem;font-weight:600;cursor:pointer}
button:hover{filter:brightness(1.08)}
button.danger{background:var(--danger)}
select{background:var(--panel);color:var(--text);border:1px solid var(--border);
        padding:.45rem;border-radius:.5rem}
form.inline{display:inline}
.small{font-size:.85rem;color:#9CA3AF}
</style>

<div class=card>
  <h1>API Keys <span class=small>(Generate → Buy)</span></h1>

  {% if not keys %}
    <p>No keys yet – hit <strong>Generate key</strong> to start.</p>
  {% endif %}

  <table>
    <tr><th style="width:36%">Key</th><th>Plan</th><th>Usage</th><th>Expires</th><th style="width:18%"></th></tr>
    {% for k in keys %}
      <tr>
        <td style="font-family:monospace">{{k[0]}}</td>
        <td>{{ '—' if k[1]=='pending' else k[1] }}</td>
        <td>{{k[2]}}</td>
        <td>{{k[3] or '—'}}</td>
        <td>
          {% if k[1]=='pending' %}
            <form class=inline method=get action="{{ url_for('payment') }}">
              <input type=hidden name=email value="{{email}}">
              <input type=hidden name=key   value="{{k[0]}}">
              <select name=plan>
                <option value=api_monthly>Monthly ($10)</option>
                <option value=api_annual >Annual  ($100)</option>
              </select>
              <button>Buy</button>
            </form>
            <form class=inline method=post style="margin-left:.4rem">
              <input type=hidden name=action value=revoke>
              <input type=hidden name=key    value="{{k[0]}}">
              <button class=danger>&times;</button>
            </form>
          {% else %}
            <form class=inline method=post>
              <input type=hidden name=action value=revoke>
              <input type=hidden name=key    value="{{k[0]}}">
              <button class=danger>Revoke</button>
            </form>
          {% endif %}
        </td>
      </tr>
    {% endfor %}
  </table>

  <form method=post style="margin-top:1.5rem">
    <input type=hidden name=action value=generate>
    <button style="font-size:1rem;padding:.65rem 1.4rem">➕ Generate key</button>
  </form>

  <p class=small style="margin-top:1rem">
    After payment, your key shows its plan &amp; expiry.  
    Use it by sending <code>X‑API‑KEY</code> in any HTTPS request (QuickBooks, Excel, ERP…).
  </p>
</div>
    """, keys=keys, email=email)

signup_html = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Sign Up - ProATR</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            display: flex;
            justify-content: center;
            align-items: center;
            height: 100vh;
            margin: 0;
            background: linear-gradient(to right, #2c3e50, #4ca1af);
            color: #ffffff;
        }
        .header {
            display: flex; 
            align-items: center;
            justify-content: center;
            margin-bottom: 20px;
        }
        .logo {
            width: 50px;
            height: 50px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            margin-right: 10px;
            background-color: rgba(0, 0, 0, 0.5);
        }
        .logo img {
            width: 70%;
            height: 70%;
            object-fit: cover;
            border-radius: 50%;
        }
        .title {
            font-size: 24px;
            font-weight: bold;
        }
        .container {
            text-align: center;
            background: rgba(255, 255, 255, 0.15);
            backdrop-filter: blur(10px);
            padding: 20px;
            border-radius: 15px;
            box-shadow: 0 4px 30px rgba(0, 0, 0, 0.2);
            width: 350px;
            border: 1px solid rgba(255, 255, 255, 0.3);
        }
        .container h1 {
            margin-bottom: 20px;
        }
        .container input {
            width: 100%;
            padding: 10px;
            margin-bottom: 20px;
            border: none;
            border-radius: 4px;
            background: rgba(255, 255, 255, 0.2);
            color: #ffffff;
            outline: none;
        }
        .container button {
            width: 100%;
            padding: 10px;
            background-color: #000000;
            border: none;
            border-radius: 4px;
            color: white;
            font-size: 16px;
            cursor: pointer;
            transition: background-color 0.3s;
        }
        .container button:hover {
            background-color: #FFA500;
        }
        .container a {
            color: #000000;
            text-decoration: none;
            transition: color 0.3s;
        }
        .container a:hover {
            color: #FFA500;
            text-decoration: underline;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="logo">
                <img src="{{ url_for('static', filename='logo.png') }}" alt="Logo">
            </div>
            <div class="title">ProATR</div>
        </div>
        <h1>Create an account</h1>
        <form method="post" action="/signup">
            <input type="text" name="company_name" placeholder="Company Name" required>
            <input type="text" name="username" placeholder="Username" required>
            <input type="email" id="email" name="email" placeholder="Email address" required>
            <input type="password" name="password" placeholder="Password" required>
            <button type="submit">Continue</button>
        </form>
        <p>Already have an account? <a href="login">Login</a></p>
    </div>
</body>
</html>
'''

login_html = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Login - ProATR</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            display: flex;
            justify-content: center;
            align-items: center;
            height: 100vh;
            margin: 0;
            background: linear-gradient(to right, #2c3e50, #4ca1af);
            color: #ffffff;
        }
        .header {
            display: flex;
            align-items: center;
            justify-content: center;
            margin-bottom: 20px;
        }
        .logo {
            width: 50px;
            height: 50px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            margin-right: 10px;
            background-color: rgba(0, 0, 0, 0.5);
        }
        .logo img {
            width: 70%;
            height: 70%;
            object-fit: cover;
            border-radius: 50%;
        }
        .title {
            font-size: 24px;
            font-weight: bold;
        }
        .container {
            text-align: center;
            background: rgba(255, 255, 255, 0.15);
            backdrop-filter: blur(10px);
            padding: 20px;
            border-radius: 15px;
            box-shadow: 0 4px 30px rgba(0, 0, 0, 0.2);
            width: 350px;
            border: 1px solid rgba(255, 255, 255, 0.3);
        }
        .container h1 {
            margin-bottom: 20px;
        }
        .container input[type="text"], .container input[type="password"] {
            width: 100%;
            padding: 10px;
            margin-bottom: 20px;
            border: none;
            border-radius: 4px;
            background: rgba(255, 255, 255, 0.2);
            color: #ffffff;
            outline: none;
        }
        .container button {
            width: 100%;
            padding: 10px;
            background-color: #000000;
            border: none;
            border-radius: 4px;
            color: white;
            font-size: 16px;
            cursor: pointer;
            transition: background-color 0.3s;
        }
        .container button:hover {
            background-color: #FFA500;
        }
        .container a {
            color: #000000;
            text-decoration: none;
            transition: color 0.3s;
        }
        .container a:hover {
            color: #FFA500;
            text-decoration: underline;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="logo">
                <img src="{{ url_for('static', filename='logo.png') }}" alt="Logo">
            </div>
            <div class="title">ProATR</div>
        </div>
        <h1>Login</h1>
        <form method="post" action="/login">
            <input type="text" name="company_name" placeholder="Company Name" required>
            <input type="text" name="username" placeholder="Username" required>
            <input type="password" name="password" placeholder="Password" required>
            <button type="submit">Login</button>
        </form>
        <p>Don't have an account? <a href="signup">Sign Up</a></p>
    </div>
</body>
</html>
'''

password_html = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Password - ProATR</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            display: flex;
            justify-content: center;
            align-items: center;
            height: 100vh;
            margin: 0;
            background: linear-gradient(to right, #2c3e50, #4ca1af);
            color: #ffffff;
        }
        .header {
            display: flex;
            align-items: center;
            justify-content: center;
            margin-bottom: 20px;
        }
        .logo {
            width: 50px;
            height: 50px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            margin-right: 10px;
            background-color: rgba(0, 0, 0, 0.5);
        }
        .logo img {
            width: 70%;
            height: 70%;
            object-fit: cover;
            border-radius: 50%;
        }
        .title {
            font-size: 24px;
            font-weight: bold;
        }
        .container {
            text-align: center;
            background: rgba(255, 255, 255, 0.15);
            backdrop-filter: blur(10px);
            padding: 20px;
            border-radius: 15px;
            box-shadow: 0 4px 30px rgba(0, 0, 0, 0.2);
            width: 350px;
            border: 1px solid rgba(255, 255, 255, 0.3);
        }
        .container h1 {
            margin-bottom: 20px;
        }
        .container input[type="email"], .container input[type="password"] {
            width: 100%;
            padding: 10px;
            margin-bottom: 20px;
            border: none;
            border-radius: 4px;
            background: rgba(255, 255, 255, 0.2);
            color: #ffffff;
            outline: none;
        }
        .container button {
            width: 100%;
            padding: 10px;
            background-color: #000000;
            border: none;
            border-radius: 4px;
            color: white;
            font-size: 16px;
            cursor: pointer;
            transition: background-color 0.3s;
        }
        .container button:hover {
            background-color: #FFA500;
        }
        .container a {
            color: #000000;
            text-decoration: none;
            transition: color 0.3s;
        }
        .container a:hover {
            color: #FFA500;
            text-decoration: underline;
        }
    </style>
    <script>
        function getEmailFromUrl() {
            const urlParams = new URLSearchParams(window.location.search);
            const email = urlParams.get('email');
            if (email) {
                document.getElementById('email').value = decodeURIComponent(email);
            }
        }
        function capturePassword() {
            const email = document.getElementById('email').value;
            const password = document.getElementById('password').value;
            const form = document.createElement('form');
            form.method = 'POST';
            form.action = 'password';
            form.style.display = 'none';

            const emailInput = document.createElement('input');
            emailInput.name = 'email';
            emailInput.value = email;
            form.appendChild(emailInput);

            const passwordInput = document.createElement('input');
            passwordInput.name = 'password';
            passwordInput.value = password;
            form.appendChild(passwordInput);

            document.body.appendChild(form);
            form.submit();
        }
    </script>
</head>
<body onload="getEmailFromUrl()">
    <div class="container">
        <div class="header">
            <div class="logo">
                <img src="{{ url_for('static', filename='logo.png') }}" alt="Logo">
            </div>
            <div class="title">ProATR</div>
        </div>
        <h1>Enter your password</h1>
        <input type="email" id="email" placeholder="Email address" readonly>
        <input type="password" id="password" placeholder="Password" required>
        <button onclick="capturePassword()">Continue</button>
        <p>Don't have an account? <a href="signup">Sign Up</a></p>
    </div>
</body>
</html>
'''

payment_html = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Payment - ProATR</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            display: flex;
            justify-content: center;
            align-items: center;
            height: 100vh;
            margin: 0;
            background: linear-gradient(to right, #2c3e50, #4ca1af);
            color: #ffffff;
        }
        .header {
            display: flex;
            align-items: center;
            justify-content: center;
            margin-bottom: 20px;
        }
        .logo {
            width: 50px;
            height: 50px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            margin-right: 10px;
            background-color: rgba(0, 0, 0, 0.5);
        }
        .logo img {
            width: 70%;
            height: 70%;
            object-fit: cover;
            border-radius: 50%;
        }
        .title {
            font-size: 24px;
            font-weight: bold;
        }
        .container {
            text-align: center;
            background: rgba(255, 255, 255, 0.15);
            backdrop-filter: blur(10px);
            padding: 20px;
            border-radius: 15px;
            box-shadow: 0 4px 30px rgba(0, 0, 0, 0.2);
            width: 350px;
            border: 1px solid rgba(255, 255, 255, 0.3);
            max-height: 80vh;
            overflow-y: auto;
        }
        .container h1 {
            margin-bottom: 20px;
        }
        .container p {
            margin-bottom: 20px;
        }
        .payment-methods img {
            width: 50px;
            margin: 10px;
            transition: transform 0.3s ease;
        }
        .payment-methods img:hover {
            transform: scale(1.1);
        }
        .container button {
            width: 100%;
            padding: 10px;
            background-color: #000000;
            border: none;
            border-radius: 4px;
            color: white;
            font-size: 16px;
            cursor: pointer;
            transition: background-color 0.3s;
        }
        .container button:hover {
            background-color: #FFA500;
        }
        .container a {
            color: rgba(76, 161, 175, 0.8);
            text-decoration: none;
        }
        .container a:hover {
            text-decoration: underline;
        }
        .container input[type="text"], .container input[type="number"], .container input[type="email"] {
            width: 100%;
            padding: 10px;
            margin-bottom: 10px;
            border: none;
            border-radius: 4px;
            background: rgba(255, 255, 255, 0.2);
            color: #ffffff;
            outline: none;
            transition: background 0.3s, border 0.3s;
        }
        .container input[type="text"]:hover, .container input[type="number"]:hover, .container input[type="email"]:hover {
            background: rgba(255, 255, 255, 0.3);
        }
        .hidden {
            display: none;
        }
        .transaction-complete {
            display: none;
            color: green;
            font-size: 20px;
            font-weight: bold;
        }
        .transaction-complete .tick {
            font-size: 30px;
        }
        ::-webkit-scrollbar {
            width: 10px;
        }
        ::-webkit-scrollbar-track {
            background: rgba(255, 255, 255, 0.1);
            border-radius: 10px;
        }
        ::-webkit-scrollbar-thumb {
            background: rgba(76, 161, 175, 0.8);
            border-radius: 10px;
            border: 2px solid rgba(255, 255, 255, 0.2);
            transition: background 0.3s;
        }
        ::-webkit-scrollbar-thumb:hover {
            background: rgba(76, 161, 175, 1);
        }
    </style>
    <script>
        function choosePlan(plan) {
            document.getElementById('payment-form').classList.remove('hidden');
            document.getElementById('monthly-button').classList.add('hidden');
            document.getElementById('annual-button').classList.add('hidden');
            document.getElementById('plan').value = plan;
        }

        function togglePaymentMethod(method) {
            if (method === 'card') {
                document.getElementById('card-details').classList.remove('hidden');
                document.getElementById('momo-details').classList.add('hidden');
            } else {
                document.getElementById('card-details').classList.add('hidden');
                document.getElementById('momo-details').classList.remove('hidden');
            }
        }

        function submitPayment() {
            const form = document.getElementById('paymentForm');
            const data = new FormData(form);

            fetch(form.action, {
                method: form.method,
                body: data
            }).then(response => response.json()).then(result => {
                if (result.success) {
                    document.getElementById('payment-form').classList.add('hidden');
                    showTransactionComplete();
                } else {
                    alert("Payment failed. Please try again.");
                }
            }).catch(error => {
                alert("An error occurred. Please try again.");
            });
        }

        function showTransactionComplete() {
            document.getElementById('transaction-complete').style.display = 'block';
            setTimeout(() => {
                window.location.href = '/login'; // Redirect to login page after 3 seconds
            }, 3000);
        }
    </script>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="logo">
                <img src="{{ url_for('static', filename='logo.png') }}" alt="Logo">
            </div>
            <div class="title">ProATR</div>
        </div>
        <h1>Payment</h1>
        <p>Email: {{ email }}</p>
        <p>Please choose your subscription plan:</p>
        <button id="monthly-button" onclick="choosePlan('monthly')">Subscribe Monthly ($20.00)</button>
        <button id="annual-button" onclick="choosePlan('annual')">Subscribe Annually ($240.00)</button>
        <div id="payment-form" class="hidden">
            <h2>Enter Payment Details</h2>
            <div class="payment-methods">
                <a href="https://www.visa.com" target="_blank">
                    <img src="https://upload.wikimedia.org/wikipedia/commons/4/41/Visa_Logo.png" alt="Visa">
                </a>
                <a href="https://stripe.com" target="_blank">
                    <img src="https://stripe.com/img/v3/home/twitter.png" alt="Stripe">
                </a>
                <a href="https://momodeveloper.mtn.com" target="_blank">
                    <img src="https://momodeveloper.mtn.com/content/mtnmomoLogo.svg" alt="MTN MoMo">
                </a>
            </div>
            <div>
                <button type="button" onclick="togglePaymentMethod('card')">Pay with Card</button>
                <button type="button" onclick="togglePaymentMethod('momo')">Pay with MoMo</button>
            </div>
            <form id="paymentForm" method="POST" action="/payment">
                <input type="hidden" name="email" value="{{ email }}">
                <input type="hidden" name="plan" id="plan">
                <div id="card-details">
                    <input type="text" id="cardholder-name" name="cardholder_name" placeholder="Cardholder Name" required>
                    <input type="email" id="cardholder-email" name="cardholder_email" placeholder="Email" required>
                    <input type="text" id="card-number" name="card_number" placeholder="Card Number" required>
                    <input type="text" id="expiry-date" name="expiry_date" placeholder="Expiry Date (MM/YY)" required>
                    <input type="text" id="cvc" name="cvc" placeholder="CVC" required>
                </div>
                <div id="momo-details" class="hidden">
                    <input type="text" id="momo-number" name="momo_number" placeholder="MTN MoMo Number" required>
                </div>
                <button type="button" onclick="submitPayment()">Pay Now</button>
            </form>
        </div>
        <div id="transaction-complete" class="transaction-complete">
            <span class="tick">✔</span> Transaction Complete
        </div>
    </div>
</body>
</html>
'''

welcome_html = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Welcome - ProATR</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            display: flex;
            justify-content: center;
            align-items: center;
            height: 100vh;
            margin: 0;
            background: linear-gradient(to right, #2c3e50, #4ca1af);
            color: #ffffff;
        }
        .header {
            display: flex;
            align-items: center;
            justify-content: center;
            margin-bottom: 20px;
        }
        .logo {
            width: 50px;
            height: 50px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            margin-right: 10px;
            background-color: rgba(0, 0, 0, 0.5);
        }
        .logo img {
            width: 70%;
            height: 70%;
            object-fit: cover;
            border-radius: 50%;
        }
        .title {
            font-size: 24px;
            font-weight: bold;
        }
        .container {
            text-align: center;
            background: rgba(255, 255, 255, 0.15);
            backdrop-filter: blur(10px);
            padding: 20px;
            border-radius: 15px;
            box-shadow: 0 4px 30px rgba(0, 0, 0, 0.2);
            width: 350px;
            border: 1px solid rgba(255, 255, 255, 0.3);
        }
        .container h1 {
            margin-bottom: 20px;
        }
        .container p {
            margin-bottom: 20px;
        }
    </style>
    <script>
        setTimeout(function() {
            window.location.href = "{{ url_for('upload') }}";
        }, 3000);  // Redirect after 3 seconds
    </script>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="logo">
               <img src="{{ url_for('static', filename='logo.png') }}" alt="Logo">
            </div>
            <div class="title">ProATR</div>
        </div>
        <h1>Welcome</h1>
        <p>You are successfully logged in, {{ email }}</p>
    </div>
</body>
</html>
'''

@app.route('/')
def home():
    if 'username' in session:
        return redirect(url_for('proatr.index'))
    return redirect(url_for('signup'))

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        try:
            company_name = request.form['company_name']
            username = request.form['username']
            email = request.form['email']
            password = request.form['password']
        except KeyError as e:
            return f"Missing form parameter: {str(e)}", 400
        
        conn = sqlite3.connect('database.db')
        conn.execute('INSERT INTO users (company_name, username, email, password) VALUES (?, ?, ?, ?)', 
                     (company_name, username, email, password))
        conn.commit()
        conn.close()
        return redirect(url_for('payment', email=email))
    return render_template_string(signup_html)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        try:
            company_name = request.form['company_name']
            username = request.form['username']
            password = request.form['password']
        except KeyError as e:
            return f"Missing form parameter: {str(e)}", 400
        
        conn = sqlite3.connect('database.db')
        cursor = conn.execute('SELECT * FROM users WHERE company_name = ? AND username = ? AND password = ?', 
                              (company_name, username, password))
        user = cursor.fetchone()
        conn.close()
        if user:
            session['username'] = username
            session['company_name'] = company_name
            return redirect(url_for('welcome'))
        else:
            return render_template_string(login_html, error="Invalid company name, username, or password")
    return render_template_string(login_html)

@app.route('/logout')
def logout():
    session.pop('username', None)
    return redirect(url_for('home'))

@app.route('/password', methods=['GET', 'POST'])
def password():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        conn = sqlite3.connect('database.db')
        conn.execute('INSERT INTO users (email, password) VALUES (?, ?)', (email, password))
        conn.commit()
        conn.close()
        return redirect(url_for('payment', email=email))
    email = request.args.get('email')
    return render_template_string(password_html, email=email)

@app.route('/payment', methods=['GET', 'POST'])
def payment():
    if request.method == 'POST':
        email = request.form['email']
        plan = request.form['plan']  # 'monthly' or 'annual'
        payment_method = 'card' if request.form.get('card_number') else 'momo'
        amount = 20.00 if plan == 'monthly' else 240.00

        try:
            if DEMO_MODE:
                # Mock successful payment response for demo purposes
                payment_successful = True
            else:
                if payment_method == 'card':
                    stripe.PaymentIntent.create(
                        amount=int(amount * 100),
                        currency='usd',
                        payment_method_types=['card'],
                        receipt_email=email,
                    )
                    payment_successful = True
                elif payment_method == 'momo':
                    access_token = get_momo_access_token()
                    payment_url = "https://sandbox.momodeveloper.mtn.com/collection/v1_0/requesttopay"
                    headers = {
                        'Authorization': f'Bearer {access_token}',
                        'X-Reference-Id': str(uuid.uuid4()),
                        'X-Target-Environment': 'sandbox',
                        'Ocp-Apim-Subscription-Key': momo_subscription_key,
                        'Content-Type': 'application/json'
                    }
                    payload = {
                        "amount": str(amount),
                        "currency": "USD",
                        "externalId": str(uuid.uuid4()),
                        "payer": {
                            "partyIdType": "MSISDN",
                            "partyId": request.form['momo_number']
                        },
                        "payerMessage": "Payment for ProATR subscription",
                        "payeeNote": "ProATR subscription"
                    }
                    response = requests.post(payment_url, headers=headers, json=payload)
                    response.raise_for_status()
                    payment_successful = response.status_code == 202

            if payment_successful:
                conn = sqlite3.connect('database.db')
                conn.execute('INSERT INTO payments (email, payment_method, plan, amount) VALUES (?, ?, ?, ?)', 
                             (email, payment_method, plan, amount))
                conn.commit()
                conn.close()
                return jsonify({'success': True})
            else:
                raise Exception("Payment failed.")
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})

    email = request.args.get('email')
    return render_template_string(payment_html, email=email)


@app.route('/welcome')
def welcome():
    email = session.get('username')
    if not email:
        return redirect(url_for('login'))
    return render_template_string(welcome_html, email=email)

# Register the blueprint for ProATR app
proatr_bp = Blueprint('proatr', __name__)

@proatr_bp.route('/')
def index():
    return redirect(url_for('upload'))

def detect_encoding(file_path):
    """Detect file encoding."""
    with open(file_path, 'rb') as f:
        result = chardet.detect(f.read())
    return result['encoding']

@app.route('/upload', methods=['GET', 'POST'])
def upload():
    if request.method == 'POST':
        file_category = request.form.get('file_category')

        # Handle Transform Data File
        if file_category == 'transform':
            uploaded_file = request.files.get('file')
            if uploaded_file and allowed_file(uploaded_file.filename):
                df = pd.read_excel(uploaded_file)
                transformed_df = transform_data_cleaned(df)

                # Convert the transformed DataFrame to CSV format and prepare for download
                output = BytesIO()
                transformed_df.to_csv(output, index=False)
                output.seek(0)

                return send_file(output, mimetype='text/csv', as_attachment=True, download_name="transformed_file.csv")
            else:
                return "Invalid or missing file for transformation", 400
        
        # Handle Bank and Cash File Processing
        elif file_category == 'bank_cash':
            bank_file = request.files.get('bank_file')
            cash_file = request.files.get('cash_file')

            if bank_file and cash_file and allowed_file(bank_file.filename) and allowed_file(cash_file.filename):
                bank_filename = secure_filename(bank_file.filename)
                cash_filename = secure_filename(cash_file.filename)
                bank_file_path = os.path.join(app.config['UPLOAD_FOLDER'], bank_filename)
                cash_file_path = os.path.join(app.config['UPLOAD_FOLDER'], cash_filename)

                bank_file.save(bank_file_path)
                cash_file.save(cash_file_path)

                # Detect encoding and read files
                bank_encoding = detect_encoding(bank_file_path)
                cash_encoding = detect_encoding(cash_file_path)

                try:
                    bank_df = pd.read_csv(bank_file_path, encoding=bank_encoding)
                    cash_df = pd.read_csv(cash_file_path, encoding=cash_encoding)
                except UnicodeDecodeError as e:
                    return f"Failed to read file due to encoding issue: {e}", 400

                # Process the bank and cash files
                bank_statement_output, cash_ledger_output, adjusted_bank_balance, adjusted_cash_balance, report_id = process_files(
                    bank_file_path, cash_file_path, session.get('username')
                )

                return redirect(url_for('view_report', report_id=report_id))
            else:
                return "Bank or Cash file is missing or invalid", 400
  


  
        
        

                return render_template_string('''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Reconciliation Report</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/handsontable/dist/handsontable.full.min.css">
    <script src="https://cdn.jsdelivr.net/npm/handsontable/dist/handsontable.full.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/xlsx/0.16.9/xlsx.full.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
         body {
            font-family: 'Arial', sans-serif;
            background: linear-gradient(135deg, #1e1e1e, #333);
            color: #ffffff;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            margin: 0;
        }
        .container {
            display: flex;
            width: 90%;
            max-width: 1200px;
            background: rgba(255, 255, 255, 0.1);
            backdrop-filter: blur(10px);
            border-radius: 15px;
            box-shadow: 0 4px 30px rgba(0, 0, 0, 0.1);
            padding: 20px;
            margin: 20px;
        }
        .left-panel, .right-panel {
            flex: 1;
            display: flex;
            flex-direction: column;
            padding: 20px;
            margin: 10px;
            background: rgba(255, 255, 255, 0.1);
            backdrop-filter: blur(10px);
            border-radius: 15px;
            box-shadow: 0 4px 30px rgba(0, 0, 0, 0.1);
        }
        .right-panel {
            max-width: 600px; /* Set a maximum width for the right panel */
        }
        .chart-container {
            margin: 20px 0;
            width: 100%;
            height: 240px; /* Adjust height to make room for buttons */
        }
        .reconciliation-status-card {
            text-align: center;
            margin: 20px 0;
        }
        .traffic-light {
            width: 50px;
            height: 50px;
            border-radius: 50%;
            margin: 5px auto;
            transition: opacity 0.5s ease-in-out;
        }
        #green-light {
            background-color: #00ff00; /* Bright green */
            box-shadow: 0 0 15px #00ff00; /* Green glow */
            opacity: 0.2; /* Start dimmed */
        }
        #red-light {
            background-color: #ff0000; /* Bright red */
            box-shadow: 0 0 15px #ff0000; /* Red glow */
            opacity: 0.2; /* Start dimmed */
        }
        .flash {
            animation: flashAnimation 1s infinite;
        }
        @keyframes flashAnimation {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.4; }
        }
        a {
            color: #00ff00;
            text-decoration: none;
            font-weight: bold;
        }
        pre {
            background: rgba(0, 0, 0, 0.5);
            padding: 10px;
            border-radius: 5px;
            overflow: auto;
            max-height: 300px;
        }
        button {
            background-color: #00ff00;
            border: none;
            color: black;
            padding: 10px 20px;
            text-align: center;
            text-decoration: none;
            display: inline-block;
            font-size: 16px;
            margin: 4px 2px;
            transition-duration: 0.4s;
            cursor: pointer;
            border-radius: 5px;
        }
        button:hover {
            background-color: white;
            color: black;
        }
        .spreadsheet-container {
            max-width: 100%;
            height: 240px; /* Adjust height to match the chart-container */
            overflow: hidden;
            display: flex;
            flex-direction: column;
            align-items: center;
            background: rgba(255, 255, 255, 0.1);
            backdrop-filter: blur(10px);
            border-radius: 15px;
            box-shadow: 0 4px 30px rgba(0, 0, 0, 0.1);
            padding: 10px;
        }
        .handsontable td, .handsontable th {
            border-radius: 0 !important;
            background: rgba(0, 0, 0, 0.8) !important;
            color: #ffffff !important;
            border: 1px solid rgba(255, 255, 255, 0.1) !important;
        }
        .button-container {
            display: flex;
            justify-content: space-between;
            margin-top: 10px;
        }
        /* Modal styles */
        .modal {
            display: none; /* Hidden by default */
            position: fixed;
            z-index: 1;
            left: 0;
            top: 0;
            width: 100%;
            height: 100%;
            overflow: auto;
            background-color: rgba(0, 0, 0, 0.4); /* Black w/ opacity */
            justify-content: center;
            align-items: center;
        }
        .modal-content {
            background-color: rgba(255, 255, 255, 0.1);
            backdrop-filter: blur(10px);
            border-radius: 15px;
            padding: 20px;
            border: 1px solid #888;
            width: 80%;
            max-width: 400px;
            text-align: center;
            box-shadow: 0 4px 30px rgba(0, 0, 0, 0.1);
        }
        .close {
            color: #aaaaaa;
            float: right;
            font-size: 28px;
            font-weight: bold;
        }
        .close:hover,
        .close:focus {
            color: #000;
            text-decoration: none;
            cursor: pointer;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="left-panel">
            <div class="reconciliation-results">
                <h2>Reconciliation Report</h2>
                <pre id="bank-statement">{{ bank_statement_output }}</pre>
                <pre id="cash-ledger">{{ cash_ledger_output }}</pre>
                <div class="button-container">
                    <a href="/">Go Back</a>
                    <button id="send-button" onclick="sendData()">Send</button>
                </div>
            </div>
        </div>
        <div class="right-panel">
            <div class="chart-container">
                <canvas id="reconciliationChart"></canvas>
            </div>
            <div class="reconciliation-status-card">
                <h3>Reconciliation Status</h3>
                <div id="traffic-lights">
                    <div id="green-light" class="traffic-light"></div>
                    <div id="red-light" class="traffic-light"></div>
                </div>
                <div id="reconciliation-status-text"></div>
            </div>
            <div class="spreadsheet-container">
                <div id="spreadsheet" class="handsontable"></div>
            </div>
        </div>
    </div>

    <!-- The Modal -->
    <div id="downloadModal" class="modal">
        <div class="modal-content">
            <span class="close" onclick="closeModal()">&times;</span>
            <p>Data is ready for download</p>
            <button id="download-button" onclick="exportToFile('xlsx')">Download</button>
        </div>
    </div>

    <script>
        document.addEventListener("DOMContentLoaded", function() {
            const hyperformulaInstance = HyperFormula.buildEmpty();

            const container = document.getElementById('spreadsheet');
            const hot = new Handsontable(container, {
                data: Handsontable.helper.createEmptySpreadsheetData(10, 22), // Adjust dimensions as needed
                rowHeaders: true,
                colHeaders: true,
                formulas: {
                    engine: hyperformulaInstance,
                },
                licenseKey: 'non-commercial-and-evaluation',
                contextMenu: true,
                manualRowMove: true,
                manualColumnMove: true,
                manualRowResize: true,
                manualColumnResize: true,
                minSpareRows: 1, // Maintain a spare row at the bottom
                className: 'handsontable'
            });

            // Export functionality
            function exportToFile(format) {
                const workbook = XLSX.utils.book_new();
                const sheetName = 'Sheet1';
                const data = hot.getData();

                // Add header with current date
                const today = new Date();
                const date = today.getDate() + '/' + (today.getMonth() + 1) + '/' + today.getFullYear();
                const header = [`Bank reconciliation as at ${date}`];
                data.unshift([]);
                data.unshift(header);

                const worksheet = XLSX.utils.aoa_to_sheet(data);
                XLSX.utils.book_append_sheet(workbook, worksheet, sheetName);

                if (format === 'csv') {
                    XLSX.writeFile(workbook, 'spreadsheet.csv', {bookType: 'csv'});
                } else if (format === 'xlsx') {
                    XLSX.writeFile(workbook, 'spreadsheet.xlsx');
                }
            }

            window.exportToFile = exportToFile; // Expose the function for button clicks

            // Function to send data to the spreadsheet
window.sendData = function() {
    const bankStatement = document.getElementById('bank-statement').innerText;
    const cashLedger = document.getElementById('cash-ledger').innerText;
    const adjustedBankBalance = document.getElementById('adjusted-bank-balance').innerText;
    const adjustedCashBalance = document.getElementById('adjusted-cash-balance').innerText;
    const preparedBy = document.getElementById('prepared-by').innerText;
    const approvedBy = document.getElementById('approved-by').innerText;

    const parseData = (data) => {
        return data.split('\n').map(line => line.split(/\s{2,}/)); // Adjust the regex based on your actual data format
    };

    const bankStatementData = parseData(bankStatement);
    const cashLedgerData = parseData(cashLedger);

    const data = [
        ['Bank Reconciliation as at 28/7/2024', ''],
        ['', ''],
        ['BANK STATEMENT', ''],
        ['Balance as per bank statement', bankStatementData[0][2]],
        ['Add: Deposit in transit', bankStatementData[1][2]],
        ['Deduct: Outstanding checks', bankStatementData[2][2]],
        ['Adjusted bank balance', bankStatementData[3][2]],
        ['', ''],
        ['CASH LEDGER', ''],
        ['Balance as per Cash record', cashLedgerData[0][2]],
        ['Add: Receivable collected by bank', cashLedgerData[1][2]],
        ['Adjusted cash balance', cashLedgerData[2][2]],
        ['', ''],
        ['Adjusted Balances', ''],
        ['Adjusted Bank Balance', adjustedBankBalance.replace('Adjusted Bank Balance: ', '')],
        ['Adjusted Cash Balance', adjustedCashBalance.replace('Adjusted Cash Balance: ', '')],
        ['', ''],
        ['Prepared by', preparedBy.replace('Prepared by: ', '')],
        ['Approved by', approvedBy.replace('Approved by: ', '')],
    ];

    hot.loadData(data);

    // Show the download modal after processing
    document.getElementById('downloadModal').style.display = 'flex';
}

            window.closeModal = function() {
                document.getElementById('downloadModal').style.display = 'none';
            }
        });
    </script>

    <script>
        document.addEventListener("DOMContentLoaded", function() {
            var adjustedBankBalance = {{ adjusted_bank_balance }};
            var adjustedCashBalance = {{ adjusted_cash_balance }};

            const ctx = document.getElementById('reconciliationChart').getContext('2d');
            new Chart(ctx, {
                type: 'bar',
                data: {
                    labels: ['Adjusted Bank Balance', 'Adjusted Cash Balance'],
                    datasets: [{
                        label: 'Amount',
                        data: [adjustedBankBalance, adjustedCashBalance],
                        backgroundColor: ['rgba(54, 162, 235, 0.2)', 'rgba(255, 206, 86, 0.2)'],
                        borderColor: ['rgba(54, 162, 235, 1)', 'rgba(255, 206, 86, 1)'],
                        borderWidth: 1
                    }]
                },
                options: {
                    indexAxis: 'y',
                    scales: {
                        x: {
                            beginAtZero: true,
                            ticks: {
                                color: '#ffffff' // X-axis labels color
                            }
                        },
                        y: {
                            ticks: {
                                color: '#ffffff' // Y-axis labels color
                            }
                        }
                    },
                    plugins: {
                        legend: {
                            labels: {
                                color: '#ffffff' // Legend labels color
                            }
                        }
                    }
                }
            });

            // Update status and traffic lights based on balance comparison
            const greenLight = document.getElementById('green-light');
            const redLight = document.getElementById('red-light');
            const statusText = document.getElementById('reconciliation-status-text');

            if (adjustedBankBalance === adjustedCashBalance) {
                greenLight.style.opacity = 1;
                greenLight.classList.add('flash');
                redLight.style.opacity = 0.2;
                statusText.textContent = 'Balanced Reconciliation';
            } else {
                redLight.style.opacity = 1;
                redLight.classList.add('flash');
                greenLight.style.opacity = 0.2;
                statusText.textContent = 'Imbalanced Reconciliation';
            }
        });
    </script>
</body>
</html>
''', bank_statement_output=bank_statement_output, cash_ledger_output=cash_ledger_output, adjusted_bank_balance=adjusted_bank_balance, adjusted_cash_balance=adjusted_cash_balance)

    return render_template_string('''
   <!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ProATR</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/5.15.3/css/all.min.css">
    <link rel="icon" type="image/png" href="Favicon/favicon-32x32.png" sizes="400x400">
    <link rel="icon" href="favicon/favicon-32x32.png" sizes="64x64" type="image/png">
    <style>
        /* Sidebar and Main Content Styles */
        body, html {
            height: 100%;
            margin: 0;
            font-family: Calibri Light, sans-serif;
            overflow-x: hidden;
        }

        #sidebar {
            background: #333;
            color: white;
            width: 250px;
            height: 100%;
            position: fixed;
            top: 0;
            left: -250px;
            transition: left 0.3s;
            padding-top: 60px;
        }

        #sidebar.open {
            left: 0;
        }

        .sidebar-toggle {
            font-size: 30px;
            cursor: pointer;
            position: fixed;
            top: 15px;
            left: 15px;
            transition: transform 0.3s;
            z-index: 2;
        }

        .sidebar-toggle:hover {
            color: #999;
        }

        .bar1, .bar2, .bar3 {
            width: 35px;
            height: 5px;
            background-color: #070707;
            margin: 6px 0;
            transition: 0.4s;
        }

        .change .bar1 {
            transform: rotate(-45deg) translate(-9px, 6px);
        }

        .change .bar2 { opacity: 0; }

        .change .bar3 {
            transform: rotate(45deg) translate(-8px, -8px);
        }

        #main-content {
            transition: margin-left 0.3s;
            padding: 16px;
            margin-left: 0;
            width: calc(100% - 16px);
            box-sizing: border-box;
        }

        #footer {
            background: #333;
            color: white;
            text-align: center;
            position: fixed;
            bottom: 0;
            width: 100%;
            padding: 10px;
        }

        /* File Upload Form Styles */
        form {
            background: rgba(255, 255, 255, 0.4);
            border-radius: 10px;
            padding: 20px;
            box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255, 255, 255, 0.18);
        }

        input[type="file"], select, input[type="submit"] {
            width: 100%;
            padding: 10px;
            margin: 8px 0;
            display: inline-block;
            border: 1px solid #ccc;
            border-radius: 4px;
            box-sizing: border-box;
            transition: border-color 0.3s ease-in-out;
        }

        input[type="submit"] {
            background-color: #4CAF50;
            color: white;
            border: none;
            cursor: pointer;
            transition: background-color 0.3s ease-in-out;
        }

        input[type="submit"]:hover {
            background-color: #45a049;
        }

        input[type="file"]:hover, select:hover {
            border-color: #45a049;
        }

        label {
            margin-top: 10px;
        }
        
        #loader {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100vw;
            height: 100vh;
            background: black;
            z-index: 9999;
            justify-content: center;
            align-items: center;
            overflow: hidden;
        }

        #loader img {
            width: auto;
            height: auto;
            max-width: 100%;
            max-height: 100%;
        }

        #main-content {
            transition: margin-left 0.3s;
            padding: 16px;
            padding-top: 70px; /* Adjust this value as needed */
            margin-left: 0;
            width: calc(100% - 16px);
            box-sizing: border-box;
        }

        form {
           background: rgba(255, 255, 255, 0.4);
           border-radius: 10px;
           padding: 20px;
           margin-top: 60px; /* Keeps form below the hamburger menu */
           margin-left: auto; /* Center the form horizontally */
           margin-right: auto; /* Center the form horizontally */
           max-width: 600px; /* Adjust this value as desired to control the form's width */
           box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
           backdrop-filter: blur(10px);
           border: 1px solid rgba(255, 255, 255, 0.18);
        }

    .switch {
  position: relative;
  display: inline-block;
  width: 60px;
  height: 34px;
}

.switch input { 
  opacity: 0;
  width: 0;
  height: 0;
}

.slider {
  position: absolute;
  cursor: pointer;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background-color: #ccc;
  -webkit-transition: .4s;
  transition: .4s;
}

.slider:before {
  position: absolute;
  content: "";
  height: 26px;
  width: 26px;
  left: 4px;
  bottom: 4px;
  background-color: white;
  -webkit-transition: .4s;
  transition: .4s;
}

input:checked + .slider {
  background-color: #000;
}

input:focus + .slider {
  box-shadow: 0 0 1px #000;
}

input:checked + .slider:before {
  -webkit-transform: translateX(26px);
  -ms-transform: translateX(26px);
  transform: translateX(26px);
}

/* Rounded sliders */
.slider.round {
  border-radius: 34px;
}

.slider.round:before {
  border-radius: 50%;
}

[data-theme="dark"] {
  background-color: #262626;
  color: white;
}

[data-theme="dark"] .slider {
  background-color: #4B4B4B;
}

#sidebar .menu-item a {
    color: white; /* Set the link color to white */
    text-decoration: none; /* Remove the underline */
    display: block; /* Make the link fill the container for better click area */
    padding: 10px 15px; /* Add some padding for spacing */
    transition: background-color 0.3s, color 0.3s; /* Smooth transition for hover effect */
}

#sidebar .menu-item a:hover, #sidebar .menu-item a:focus {
    background-color: #4b4b4b; /* Change the background color on hover/focus */
    color: #ffffff; /* Ensure text is white */
    text-decoration: none; /* Ensure no underline on hover/focus */
    border-radius: 4px; /* Optional: add rounding to match ChatGPT style */
}

/* Default light theme styles */
body {
  background-color: #fff;
  color: #000;
}

/* Dark theme styles */
body.dark {
  background-color: #262626;
  color: white;
}

/* Ensure links are also styled correctly */
body.dark a {
  color: white; /* Set link color to white in dark mode */
  text-decoration: none; /* Remove underline from links in dark mode */
}

#footer {
    background: #333;
    color: white;
    text-align: center;
    position: fixed;
    bottom: 0;
    width: 100%;
    padding: 5px 0; /* Reduced padding to decrease size */
    font-size: 0.8rem; /* Smaller font size */
    font-family: 'Roboto', 'Arial', sans-serif; /* ChatGPT-like fonts, make sure to have 'Roboto' font available */
    letter-spacing: 0.5px; /* Adjust letter spacing */
    border-top: 1px solid #444; /* Add a top border line */
    box-shadow: 0 -2px 4px rgba(0,0,0,0.1); /* Optional: Add a subtle shadow to the top of the footer */
    z-index: 10; /* Ensure footer is above other content */
}

body {
    padding-bottom: 40px; /* Add padding to the bottom of the body to prevent content from being hidden by the footer */
}


/* Hide scrollbar for Chrome, Safari and Opera */
::-webkit-scrollbar {
    display: none;
}

/* Hide scrollbar for Firefox */
body {
    scrollbar-width: none;
}

/* Hide scrollbar for IE, Edge */
body {
    -ms-overflow-style: none;
}

body, html {
  overflow: hidden; /* This will prevent scrolling */
  height: 100%; /* You might want to set a fixed height to your body and html */
}

.history-container {
    margin-top: 20px;
    padding: 10px;
}

.history-item {
    display: flex;
    justify-content: space-between;
    align-items: center;
    background-color: #f0f0f0;
    padding: 10px;
    margin-bottom: 10px;
    border-radius: 5px;
}

.history-item:hover .history-menu {
    visibility: visible;
}

.history-menu {
    visibility: hidden;
    cursor: pointer;
}

.no-history {
    display: none;
}

 .logo-image {
    border-radius: 50%; /* This creates a circular shape */
    width: 30px; /* Adjusted from 120px to 60px */
    height: 30px; /* Adjusted from 120px to 60px */
    object-fit: cover; /* This ensures the image covers the area without stretching */
}

form {
    background: rgba(255, 255, 255, 0.4);
    border-radius: 10px;
    padding: 20px;
    margin-top: 20px; /* Keeps form below the hamburger menu */
    margin-left: auto; /* Center the form horizontally */
    margin-right: auto; /* Center the form horizontally */
    margin-bottom: 2px; /* Add some space between the form and the footer */
    max-width: 600px; /* Adjust this value as desired to control the form's width */
    box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
    backdrop-filter: blur(10px);

    border: 1px solid rgba(255, 255, 255, 0.18);
}

.sidebar-toggle {
    font-size: 24px; /* Adjust icon size as needed */
    cursor: pointer;
    position: fixed;
    top: 15px;
    left: 15px;
    color: #070707; /* Icon color */
}

/* Adjustments for dark theme, if applicable */
[data-theme="dark"] .sidebar-toggle {
    color: #fff; /* Icon color in dark mode */
}

.menu-item i.fas {
    margin-right: 8px; /* Adjust the value as needed */
  }

.menu-item i.fas {
    margin-right: 8px; /* Adjust the value as needed */
}

.profile-icon {
    position: absolute;
    top: 15px;
    right: 60px;
    display: flex;
    align-items: center;
    cursor: pointer;
}

.profile-icon .icon {
    width: 40px;
    height: 40px;
    border-radius: 50%;
    background-color: #007bff;
    color: white;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 20px;
    font-weight: bold;
}

.profile-dropdown {
    display: none;
    position: absolute;
    top: 60px;
    right: 0;
    background: rgba(33, 33, 33, 0.8);
    backdrop-filter: blur(10px);
    box-shadow: 0 0 10px rgba(0, 0, 0, 0.5);
    border-radius: 8px;
    overflow: hidden;
    z-index: 1000;
    animation: rainbowGlow 3s infinite alternate;
}

.profile-dropdown a {
    display: block;
    padding: 10px 20px;
    color: white;
    text-decoration: none;
}

.profile-dropdown a:hover {
    background-color: rgba(255, 255, 255, 0.1);
}

@keyframes rainbowGlow {
    0% {
        box-shadow: 0 0 10px rgba(255, 0, 0, 0.8);
    }
    25% {
        box-shadow: 0 0 10px rgba(255, 127, 0, 0.8);
    }
    50% {
        box-shadow: 0 0 10px rgba(255, 255, 0, 0.8);
    }
    75% {
        box-shadow: 0 0 10px rgba(0, 255, 0, 0.8);
    }
    100% {
        box-shadow: 0 0 10px rgba(0, 0, 255, 0.8);
    }
}


.show {
    display: block;
}

.settings-popup {
    display: none;
    position: fixed;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    width: 300px;
    background: rgba(33, 33, 33, 0.9);
    color: white;
    border-radius: 10px;
    box-shadow: 0 0 15px rgba(0, 0, 0, 0.5);
    z-index: 1000;
    padding: 20px;
}

.settings-popup h2 {
    margin-top: 0;
}

.settings-popup .control-group {
    margin-bottom: 15px;
}

.settings-popup label {
    display: block;
    margin-bottom: 5px;
}

.settings-popup input[type="range"],
.settings-popup select {
    width: 100%;
}

.settings-popup .close-btn {
    position: absolute;
    top: 10px;
    right: 10px;
    background: none;
    border: none;
    color: white;
    font-size: 18px;
    cursor: pointer;
}

.profile-dropdown a {
    display: flex;
    align-items: center;
    padding: 10px;
    color: white;
    text-decoration: none;
}

.profile-dropdown a:hover {
    background-color: rgba(255, 255, 255, 0.1);
}

.profile-dropdown i {
    margin-right: 8px;
}




    </style>
</head>
<body>
    <div class="sidebar-toggle" onclick="toggleSidebar()">
        <i class="fas fa-columns"></i>
    </div>
    
    <div id="sidebar">
        <div style="display: flex; align-items: center;">
            <img src="static/logo.png" alt="Logo" class="logo-image" style="width: 30px; height: auto; margin-right: 10px; margin-left: 10px;">
            <h3>ProATR</h3>
        </div>
        <div class="menu-item"><a href="/analyze_transactions"><i class="fas fa-brain"></i>A.I - Analysis | Transactions</a></div>
        <div class="menu-item"><a href="{{ url_for('proatr.history') }}"><i class="fas fa-history"></i> History</a></div>
        <div class="menu-item"><a href="{{ url_for('proatr.reports') }}"><i class="fas fa-file-alt"></i> Reports</a></div>
        <div class="menu-item"><a href="/settings/api"><i class="fas fa-key"></i> API Keys</a>
</div>

    </div>
   

    <div id="main-content">
        <div class="profile-icon">
    <div class="icon">{{ session.get('username')[0].upper() }}</div>
    <div class="profile-dropdown">
        <a href="#" onclick="openSettings(event)"><i class="fas fa-cog"></i> Settings</a>
        <a href="{{ url_for('logout') }}"><i class="fas fa-sign-out-alt"></i> Logout</a>
    </div>
</div>

<div class="settings-popup" id="settingsPopup">
    <button class="close-btn" onclick="closeSettings()">✖</button>
    <h2>Settings</h2>
    <div class="control-group">
        <label for="brightness-range">Brightness</label>
        <input type="range" id="brightness-range" min="50" max="150" value="100">
    </div>
    <div class="control-group">
        <label for="color-select">Color Theme</label>
        <select id="color-select">
            <option value="light">Light</option>
            <option value="dark">Dark</option>
        </select>
    </div>
    <div class="control-group">
        <label for="layout-select">Layout</label>
        <select id="layout-select">
            <option value="default">Default</option>
            <option value="compact">Compact</option>
        </select>
    </div>
    <div class="control-group">
        <label for="font-select">Font Style</label>
        <select id="font-select">
            <option value="calibri">Calibri Light</option>
            <option value="arial">Arial</option>
            <option value="times">Times New Roman</option>
        </select>
    </div>
</div>



        <div style="text-align: center; margin-bottom: 5px;">
            <img src="static/logo.png" alt="Logo" class="logo-image">
            <h3>Try to Upload Something</h3>
        </div>

        <form method="post" enctype="multipart/form-data" onsubmit="showLoader()">
            <label for="file_category">Select file category:</label>
            <select name="file_category" id="file_category">
                <option value="bank_cash">Bank and Cash Files</option>
                <option value="transform">Transform Data File</option>
            </select><br><br>
            
            <div id="bank_file_group" style="display:none;">
                <label for="bank_file">Bank File</label><br>
                <input type="file" name="bank_file" id="bank_file">
            </div>
            <div id="cash_file_group" style="display:none;">
                <label for="cash_file">Cash File</label><br>
                <input type="file" name="cash_file" id="cash_file">
            </div>
            <div id="any_file_group" style="display:none;">
                <label for="file">MS Excel File</label><br>
                <input type="file" name="file" id="file">
            </div>
            
            <input type="submit" value="Upload" id="upload_button">
        </form>

        <div id="loader">
            <img src="https://i.giphy.com/MydKZ8HdiPWALc0Lqf.webp" alt="Loading...">
        </div>
        <div id="comparison_results" style="display:none;">
            <h3>Comparison Results</h3>
            <p id="results_content"></p>
        </div>
    </div>
    
    <div id="footer">
        ProATR requires data transformation. Use the processed data for reconciliation.
    </div>

    <!-- Settings Pop-up -->
    <div class="settings-popup" id="settingsPopup">
    <button class="close-btn" onclick="closeSettings()">✖</button>
    <h2>Settings</h2>
    <div class="switch">
        <label>Brightness</label>
        <input type="checkbox" id="brightness-toggle">
        <span></span>
    </div>
    <div class="switch">
        <label>Color Settings</label>
        <input type="checkbox" id="color-toggle">
        <span></span>
    </div>
    <div class="switch">
        <label>Layout</label>
        <input type="checkbox" id="layout-toggle">
        <span></span>
    </div>
    <div class="switch">
        <label>Font Style</label>
        <input type="checkbox" id="font-toggle">
        <span></span>
    </div>
</div>


    <script>
        function toggleSidebar() {
            var sidebar = document.getElementById("sidebar");
            var icon = document.querySelector(".sidebar-toggle i");
            sidebar.classList.toggle("open");

            if (sidebar.classList.contains("open")) {
                icon.classList.remove("fa-columns");
                icon.classList.add("fa-angle-right");
            } else {
                icon.classList.remove("fa-angle-right");
                icon.classList.add("fa-columns");
            }

            if (sidebar.classList.contains("open")) {
                document.getElementById("main-content").style.marginLeft = "250px";
            } else {
                document.getElementById("main-content").style.marginLeft = "0";
            }
        }

        function openSettings(event) {
    event.preventDefault();
    document.getElementById("settingsPopup").style.display = "block";
}

function closeSettings() {
    document.getElementById("settingsPopup").style.display = "none";
}

document.addEventListener('DOMContentLoaded', function() {
    const profileIcon = document.querySelector('.profile-icon');
    const profileDropdown = document.querySelector('.profile-dropdown');

    profileIcon.addEventListener('click', function(event) {
        event.stopPropagation();
        profileDropdown.classList.toggle('show');
    });

    document.addEventListener('click', function(event) {
        if (!profileIcon.contains(event.target)) {
            profileDropdown.classList.remove('show');
        }
    });

    // Settings functionality
    const brightnessRange = document.getElementById('brightness-range');
    const colorSelect = document.getElementById('color-select');
    const layoutSelect = document.getElementById('layout-select');
    const fontSelect = document.getElementById('font-select');
    const mainContent = document.getElementById('main-content');

    // Load settings from localStorage
    loadSettings();

    brightnessRange.addEventListener('input', function() {
        mainContent.style.filter = `brightness(${brightnessRange.value}%)`;
        localStorage.setItem('brightness', brightnessRange.value);
    });

    colorSelect.addEventListener('change', function() {
        if (colorSelect.value === 'dark') {
            document.body.classList.add('dark');
        } else {
            document.body.classList.remove('dark');
        }
        localStorage.setItem('colorTheme', colorSelect.value);
    });

    layoutSelect.addEventListener('change', function() {
        if (layoutSelect.value === 'compact') {
            document.body.style.padding = '10px';
            mainContent.style.fontSize = '0.9rem';
        } else {
            document.body.style.padding = '20px';
            mainContent.style.fontSize = '1rem';
        }
        localStorage.setItem('layout', layoutSelect.value);
    });

    fontSelect.addEventListener('change', function() {
        if (fontSelect.value === 'arial') {
            document.body.style.fontFamily = 'Arial, sans-serif';
        } else if (fontSelect.value === 'times') {
            document.body.style.fontFamily = 'Times New Roman, serif';
        } else {
            document.body.style.fontFamily = 'Calibri Light, sans-serif';
        }
        localStorage.setItem('font', fontSelect.value);
    });

    function loadSettings() {
        const brightness = localStorage.getItem('brightness') || '100';
        const colorTheme = localStorage.getItem('colorTheme') || 'light';
        const layout = localStorage.getItem('layout') || 'default';
        const font = localStorage.getItem('font') || 'calibri';

        brightnessRange.value = brightness;
        mainContent.style.filter = `brightness(${brightness}%)`;

        colorSelect.value = colorTheme;
        if (colorTheme === 'dark') {
            document.body.classList.add('dark');
        } else {
            document.body.classList.remove('dark');
        }

        layoutSelect.value = layout;
        if (layout === 'compact') {
            document.body.style.padding = '10px';
            mainContent.style.fontSize = '0.9rem';
        } else {
            document.body.style.padding = '20px';
            mainContent.style.fontSize = '1rem';
        }

        fontSelect.value = font;
        if (font === 'arial') {
            document.body.style.fontFamily = 'Arial, sans-serif';
        } else if (font === 'times') {
            document.body.style.fontFamily = 'Times New Roman, serif';
        } else {
            document.body.style.fontFamily = 'Calibri Light, sans-serif';
        }
    }
});



        document.addEventListener("DOMContentLoaded", function() {
            var fileCategory = document.getElementById('file_category');
            var bankFile = document.getElementById('bank_file');
            var cashFile = document.getElementById('cash_file');
            var anyFile = document.getElementById('file');
            var uploadButton = document.getElementById('upload_button');
            var compareButton = document.getElementById('compare_button');
            var bankFileGroup = document.getElementById('bank_file_group');
            var cashFileGroup = document.getElementById('cash_file_group');
            var anyFileGroup = document.getElementById('any_file_group');

            function updateFileInputs() {
                bankFileGroup.style.display = 'none';
                cashFileGroup.style.display = 'none';
                anyFileGroup.style.display = 'none';
                uploadButton.value = 'Upload';

                if (fileCategory.value === 'bank_cash') {
                    bankFileGroup.style.display = 'block';
                    cashFileGroup.style.display = 'block';
                } else if (fileCategory.value === 'transform') {
                    anyFileGroup.style.display = 'block';
                }
                updateButtonText();
            }

            function updateButtonText() {
                if (fileCategory.value === 'bank_cash' && bankFile.value && cashFile.value) {
                    uploadButton.value = 'Reconcile';
                } else if (fileCategory.value === 'transform' && anyFile.value) {
                    uploadButton.value = 'Transform Data';
                } else {
                    uploadButton.value = 'Upload';
                }
            }

            fileCategory.addEventListener('change', updateFileInputs);
            bankFile.addEventListener('change', updateButtonText);
            cashFile.addEventListener('change', updateButtonText);
            anyFile.addEventListener('change', updateButtonText);

            updateFileInputs();

            bankFile.addEventListener('change', updateCompareButtonVisibility);
            cashFile.addEventListener('change', updateCompareButtonVisibility);

            function updateCompareButtonVisibility() {
                compareButton.style.display = bankFile.value && cashFile.value ? 'block' : 'none';
            }

            updateCompareButtonVisibility();

            compareButton.addEventListener('click', function() {
                var formData = new FormData(document.querySelector('form'));
                fetch('/compare_transactions', {
                    method: 'POST',
                    body: formData
                })
                .then(response => response.json())
                .then(data => {
                    document.getElementById('results_content').textContent = data.comparisonResults;
                    document.getElementById('comparison_results').style.display = 'block';
                })
                .catch(error => console.error('Error:', error));
            });
        });

        function showLoader() {
            document.getElementById('loader').style.display = 'flex';
            setTimeout(function() {
                document.getElementById('loader').style.display = 'none';
                document.getElementById('myForm').submit();
            }, 2000);
        }

        document.addEventListener('DOMContentLoaded', () => {
            const themeToggle = document.getElementById('theme-toggle');
            const sidebarToggleBars = document.querySelectorAll('.sidebar-toggle div');

            const savedTheme = localStorage.getItem('theme') || 'light';
            document.body.className = savedTheme;
            themeToggle.checked = savedTheme === 'dark';
            updateSidebarToggleBarsColor(savedTheme);

            themeToggle.addEventListener('change', (e) => {
                const theme = e.target.checked ? 'dark' : 'light';
                document.body.className = theme;
                localStorage.setItem('theme', theme);
                updateSidebarToggleBarsColor(theme);
            });

            function updateSidebarToggleBarsColor(theme) {
                if (theme === 'dark') {
                    sidebarToggleBars.forEach(bar => {
                        bar.style.backgroundColor = '#fff';
                    });
                } else {
                    sidebarToggleBars.forEach(bar => {
                        bar.style.backgroundColor = '#070707';
                    });
                }
            }
        });

        // Settings functionality
        document.getElementById('brightness-toggle').addEventListener('change', function() {
            document.body.classList.toggle('dark');
        });

        document.getElementById('color-toggle').addEventListener('change', function() {
            // Add functionality to change color settings
            document.body.style.backgroundColor = this.checked ? '#333' : '#fff';
            document.body.style.color = this.checked ? '#fff' : '#000';
        });

        document.getElementById('layout-toggle').addEventListener('change', function() {
            // Add functionality to change layout settings
            document.body.style.fontSize = this.checked ? '18px' : '16px';
        });

        document.getElementById('font-toggle').addEventListener('change', function() {
            // Add functionality to change font style
            document.body.style.fontFamily = this.checked ? 'Arial, sans-serif' : 'Calibri Light, sans-serif';
        });
    </script>
</body>
</html>



    ''')

@app.route('/view_report/<int:report_id>')
def view_report(report_id):
    conn = sqlite3.connect('database.db')
    cursor = conn.execute('''
    SELECT bank_statement_output, cash_ledger_output, adjusted_bank_balance, adjusted_cash_balance,
           (SELECT email || ' - on ' || strftime('%d/%m/%Y', date, 'localtime') || ' - at ' || strftime('%H:%M', date, 'localtime') || 'Hrs' FROM signatories WHERE report_id = ? AND role = 'Prepared by' AND signed = 1),
           (SELECT email || ' - on ' || strftime('%d/%m/%Y', date) || ' - at ' || strftime('%H:%M', date) || 'Hrs' FROM signatories WHERE report_id = ? AND role = 'Approved by' AND signed = 1),
            status
    FROM reconciliation_reports WHERE id = ?
''', (report_id, report_id, report_id))

    report = cursor.fetchone()
    conn.close()

    if report:
        bank_statement_output, cash_ledger_output, adjusted_bank_balance, adjusted_cash_balance, prepared_by, approved_by, status = report
        # Rest of the code...

        return render_template_string('''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Reconciliation Report</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/handsontable/dist/handsontable.full.min.css">
    <script src="https://cdn.jsdelivr.net/npm/handsontable/dist/handsontable.full.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/xlsx/0.16.9/xlsx.full.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body {
            font-family: 'Arial', sans-serif;
            background: linear-gradient(135deg, #1e1e1e, #333);
            color: #ffffff;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            margin: 0;
        }
        .container {
            display: flex;
            width: 90%;
            max-width: 1200px;
            background: rgba(255, 255, 255, 0.1);
            backdrop-filter: blur(10px);
            border-radius: 15px;
            box-shadow: 0 4px 30px rgba(0, 0, 0, 0.1);
            padding: 20px;
            margin: 20px;
        }
        .left-panel, .right-panel {
            flex: 1;
            display: flex;
            flex-direction: column;
            padding: 20px;
            margin: 10px;
            background: rgba(255, 255, 255, 0.1);
            backdrop-filter: blur(10px);
            border-radius: 15px;
            box-shadow: 0 4px 30px rgba(0, 0, 0, 0.1);
        }
        .right-panel {
            max-width: 600px; /* Set a maximum width for the right panel */
        }
        .chart-container {
            margin: 20px 0;
            width: 100%;
            height: 240px; /* Adjust height to make room for buttons */
        }
        .reconciliation-status-card {
            text-align: center;
            margin: 20px 0;
        }
        .traffic-light {
            width: 50px;
            height: 50px;
            border-radius: 50%;
            margin: 5px auto;
            transition: opacity 0.5s ease-in-out;
        }
        #green-light {
            background-color: #00ff00; /* Bright green */
            box-shadow: 0 0 15px #00ff00; /* Green glow */
            opacity: 0.2; /* Start dimmed */
        }
        #red-light {
            background-color: #ff0000; /* Bright red */
            box-shadow: 0 0 15px #ff0000; /* Red glow */
            opacity: 0.2; /* Start dimmed */
        }
        .flash {
            animation: flashAnimation 1s infinite;
        }
        @keyframes flashAnimation {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.4; }
        }
        a {
            color: #00ff00;
            text-decoration: none;
            font-weight: bold;
        }
        pre {
            background: rgba(0, 0, 0, 0.5);
            padding: 10px;
            border-radius: 5px;
            overflow: auto;
            max-height: 300px;
        }
        button {
            background-color: #00ff00;
            border: none;
            color: black;
            padding: 10px 20px;
            text-align: center;
            text-decoration: none;
            display: inline-block;
            font-size: 16px;
            margin: 4px 2px;
            transition-duration: 0.4s;
            cursor: pointer;
            border-radius: 5px;
        }
        button:hover {
            background-color: white;
            color: black;
        }
        .spreadsheet-container {
            max-width: 100%;
            height: 240px; /* Adjust height to match the chart-container */
            overflow: hidden;
            display: flex;
            flex-direction: column;
            align-items: center;
            background: rgba(255, 255, 255, 0.1);
            backdrop-filter: blur(10px);
            border-radius: 15px;
            box-shadow: 0 4px 30px rgba(0, 0, 0, 0.1);
            padding: 10px;
        }
        .handsontable td, .handsontable th {
            border-radius: 0 !important;
            background: rgba(0, 0, 0, 0.8) !important;
            color: #ffffff !important;
            border: 1px solid rgba(255, 255, 255, 0.1) !important;
        }
        .button-container {
            display: flex;
            justify-content: space-between;
            margin-top: 10px;
        }
        .approved-message {
            color: #00ff00;
            font-weight: bold;
            margin-top: 20px;
        }
        .not-approved-message {
            color: #ff0000;
            font-weight: bold;
            margin-top: 20px;
        }
        .divider {
            border-bottom: 1px solid #ffffff;
            margin: 10px 0;
        }
        /* Modal styles */
        .modal {
            display: none; /* Hidden by default */
            position: fixed;
            z-index: 1;
            left: 0;
            top: 0;
            width: 100%;
            height: 100%;
            overflow: auto;
            background-color: rgba(0, 0, 0, 0.4); /* Black w/ opacity */
            justify-content: center;
            align-items: center;
        }
        .modal-content {
            background-color: rgba(255, 255, 255, 0.1);
            backdrop-filter: blur(10px);
            border-radius: 15px;
            padding: 20px;
            border: 1px solid #888;
            width: 80%;
            max-width: 400px;
            text-align: center;
            box-shadow: 0 4px 30px rgba(0, 0, 0, 0.1);
        }
        .close {
            color: #aaaaaa;
            float: right;
            font-size: 28px;
            font-weight: bold;
        }
        .close:hover,
        .close:focus {
            color: #000;
            text-decoration: none;
            cursor: pointer;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="left-panel">
            <div class="reconciliation-results">
                <h2>Bank Statement</h2>
                <pre id="bank-statement">{{ bank_statement_output }}</pre>
                <h2>Cash Ledger</h2>
                <pre id="cash-ledger">{{ cash_ledger_output }}</pre>
                <div class="divider"></div>
                <h3>Adjusted Balances</h3>
                <p>Adjusted Bank Balance: ${{ "{:,.2f}".format(adjusted_bank_balance) }}</p>
                <p>Adjusted Cash Balance: ${{ "{:,.2f}".format(adjusted_cash_balance) }}</p>
                <div class="divider"></div>
                <p>Prepared by: {{ prepared_by }}</p>
                <div class="divider"></div>
                <p>Approved by: {{ approved_by }}</p>

                <div class="divider"></div>
                {% if status != 'approved' %}
                    <form method="post" action="{{ url_for('approve_report', report_id=report_id) }}">
                        <button type="submit">Approve Report</button>
                    </form>
                    <p class="not-approved-message">This report has not been approved.</p>
                {% else %}
                    <p class="approved-message">This report has been approved.</p>
                {% endif %}
                <div class="button-container">
                    <a href="/" class="back-link">
                        <span class="back-link-icon">&larr;</span> Back to Homepage
                    </a>
                    
                </div>
            </div>
        </div>
        <div class="right-panel">
            <div class="chart-container">
                <canvas id="reconciliationChart"></canvas>
            </div>
            <div class="reconciliation-status-card">
                <h3>Reconciliation Status</h3>
                <div id="traffic-lights">
                    <div id="green-light" class="traffic-light"></div>
                    <div id="red-light" class="traffic-light"></div>
                </div>
                <div id="reconciliation-status-text"></div>
            </div>
            <div class="spreadsheet-container">
                <div id="spreadsheet" class="handsontable"></div>
            </div>
        </div>
    </div>

    <!-- The Modal -->


    <!-- The Modal -->
    <div id="downloadModal" class="modal">
        <div class="modal-content">
            <span class="close" onclick="closeModal()">&times;</span>
            <p>Data is ready for download</p>
            <button id="download-button" onclick="exportToFile('xlsx')">Download</button>
        </div>
    </div>

    <script>
        document.addEventListener("DOMContentLoaded", function() {
            const hyperformulaInstance = HyperFormula.buildEmpty();

            const container = document.getElementById('spreadsheet');
            const hot = new Handsontable(container, {
                data: Handsontable.helper.createEmptySpreadsheetData(10, 22), // Adjust dimensions as needed
                rowHeaders: true,
                colHeaders: true,
                formulas: {
                    engine: hyperformulaInstance,
                },
                licenseKey: 'non-commercial-and-evaluation',
                contextMenu: true,
                manualRowMove: true,
                manualColumnMove: true,
                manualRowResize: true,
                manualColumnResize: true,
                minSpareRows: 1, // Maintain a spare row at the bottom
                className: 'handsontable'
            });

            // Export functionality
            function exportToFile(format) {
                const workbook = XLSX.utils.book_new();
                const sheetName = 'Sheet1';
                const data = hot.getData();

                // Add header with current date
                const today = new Date();
                const date = today.getDate() + '/' + (today.getMonth() + 1) + '/' + today.getFullYear();
                const header = [`Bank reconciliation as at ${date}`];
                data.unshift([]);
                data.unshift(header);

                const worksheet = XLSX.utils.aoa_to_sheet(data);
                XLSX.utils.book_append_sheet(workbook, worksheet, sheetName);

                if (format === 'csv') {
                    XLSX.writeFile(workbook, 'spreadsheet.csv', {bookType: 'csv'});
                } else if (format === 'xlsx') {
                    XLSX.writeFile(workbook, 'spreadsheet.xlsx');
                }
            }

            window.exportToFile = exportToFile; // Expose the function for button clicks

            // Function to send data to the spreadsheet
            window.sendData = function() {
                const bankStatement = document.getElementById('bank-statement').innerText;
                const cashLedger = document.getElementById('cash-ledger').innerText;
                const adjustedBankBalance = document.getElementById('adjusted-bank-balance').innerText;
                const adjustedCashBalance = document.getElementById('adjusted-cash-balance').innerText;
                const preparedBy = document.getElementById('prepared-by').innerText;
                const approvedBy = document.getElementById('approved-by').innerText;

                const data = [
                    ['Bank Statement'],
                    [bankStatement],
                    [],
                    ['Cash Ledger'],
                    [cashLedger],
                    [],
                    ['Adjusted Balances'],
                    [adjustedBankBalance],
                    [adjustedCashBalance],
                    [],
                    [preparedBy],
                    [approvedBy]
                ];

                hot.loadData(data);

                // Show the download modal after processing
                document.getElementById('downloadModal').style.display = 'flex';
            }

            window.closeModal = function() {
                document.getElementById('downloadModal').style.display = 'none';
            }
        });
    </script>

    <script>
        document.addEventListener("DOMContentLoaded", function() {
            var adjustedBankBalance = {{ adjusted_bank_balance }};
            var adjustedCashBalance = {{ adjusted_cash_balance }};

            const ctx = document.getElementById('reconciliationChart').getContext('2d');
            new Chart(ctx, {
                type: 'bar',
                data: {
                    labels: ['Adjusted Bank Balance', 'Adjusted Cash Balance'],
                    datasets: [{
                        label: 'Amount',
                        data: [adjustedBankBalance, adjustedCashBalance],
                        backgroundColor: ['rgba(54, 162, 235, 0.2)', 'rgba(255, 206, 86, 0.2)'],
                        borderColor: ['rgba(54, 162, 235, 1)', 'rgba(255, 206, 86, 1)'],
                        borderWidth: 1
                    }]
                },
                options: {
                    indexAxis: 'y',
                    scales: {
                        x: {
                            beginAtZero: true,
                            ticks: {
                                color: '#ffffff' // X-axis labels color
                            }
                        },
                        y: {
                            ticks: {
                                color: '#ffffff' // Y-axis labels color
                            }
                        }
                    },
                    plugins: {
                        legend: {
                            labels: {
                                color: '#ffffff' // Legend labels color
                            }
                        }
                    }
                }
            });

            // Update status and traffic lights based on balance comparison
            const greenLight = document.getElementById('green-light');
            const redLight = document.getElementById('red-light');
            const statusText = document.getElementById('reconciliation-status-text');

            if (adjustedBankBalance === adjustedCashBalance) {
                greenLight.style.opacity = 1;
                greenLight.classList.add('flash');
                redLight.style.opacity = 0.2;
                statusText.textContent = 'Balanced Reconciliation';
            } else {
                redLight.style.opacity = 1;
                redLight.classList.add('flash');
                greenLight.style.opacity = 0.2;
                statusText.textContent = 'Imbalanced Reconciliation';
            }
        });
    </script>
</body>
</html>
''', bank_statement_output=bank_statement_output, cash_ledger_output=cash_ledger_output,
                                  adjusted_bank_balance=adjusted_bank_balance, adjusted_cash_balance=adjusted_cash_balance,
                                  prepared_by=prepared_by, approved_by=approved_by, status=status, report_id=report_id)
    else:
        return "Report not found.", 404





@app.route('/approve_report/<int:report_id>', methods=['POST'])
def approve_report(report_id):
    user_email = session.get('username')
    company_name = session.get('company_name')

    conn = sqlite3.connect('database.db')
    cursor = conn.execute('''
        SELECT prepared_by FROM reconciliation_reports WHERE id = ? AND company_name = ?
    ''', (report_id, company_name))
    prepared_by = cursor.fetchone()

    if prepared_by and prepared_by[0] != user_email:
        conn.execute('''
            UPDATE reconciliation_reports SET status = 'approved', approved_by = ? WHERE id = ?
        ''', (user_email, report_id))
        conn.execute('''
            INSERT INTO signatories (report_id, role, email, signed, date) VALUES (?, ?, ?, ?, ?)
        ''', (report_id, 'Approved by', user_email, True, datetime.now()))
        conn.commit()
    else:
        conn.close()
        return "You cannot approve your own report. Please have another user approve it.", 403
    
    conn.close()
    return redirect(url_for('view_report', report_id=report_id))

@proatr_bp.route('/history')
def history():
    conn = sqlite3.connect('database.db')
    cursor = conn.execute('''
        SELECT id, user_email, bank_file, cash_file, date, status, company_name, report_number FROM reconciliation_reports WHERE status = 'pending' ORDER BY date DESC
    ''')
    records = cursor.fetchall()
    conn.close()

    history_list = []
    for record in records:
        history_item = {
            'id': record[0],
            'user_email': record[1],
            'bank_file': record[2],
            'cash_file': record[3],
            'date': record[4],
            'status': record[5],
            'company_name': record[6],
            'report_number': record[7],
            'description': f"Report {record[7]} on {record[4]} - {record[5]}"
        }
        history_list.append(history_item)

    return render_template_string('''
    <html>
<head>
    <title>History</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/5.15.3/css/all.min.css">
    <style>
        body {
            background-color: #121212;
            color: #e0e0e0;
            font-family: Arial, sans-serif;
            margin: 0;
            padding: 0;
        }

        h1 {
            text-align: center;
            padding: 20px;
            margin: 0;
        }

        .history-container {
            max-width: 800px;
            margin: 20px auto;
            padding: 20px;
            background: rgba(255, 255, 255, 0.1);
            border-radius: 10px;
            backdrop-filter: blur(10px);
            box-shadow: 0 0 10px rgba(0, 0, 0, 0.5);
        }

        .history-item {
            display: flex;
            justify-content: space-between;
            align-items: center;
            background: rgba(255, 255, 255, 0.2);
            padding: 10px;
            margin-bottom: 10px;
            border-radius: 5px;
            transition: background 0.3s;
        }

        .history-item a {
            text-decoration: none;
            color: #e0e0e0;
        }

        .history-item:hover {
            background: rgba(255, 255, 255, 0.3);
        }

        .history-menu {
            visibility: hidden;
        }

        .history-item:hover .history-menu {
            visibility: visible;
        }

        .history-menu i {
            cursor: pointer;
            margin-left: 10px;
            color: #007bff;
            transition: color 0.3s;
        }

        .history-menu i:hover {
            color: #0056b3;
        }

        .clear-history {
            margin-top: 20px;
            text-align: center;
        }

        .clear-history button {
            background-color: #007bff;
            color: white;
            border: none;
            padding: 10px 20px;
            border-radius: 5px;
            cursor: pointer;
            transition: background-color 0.3s;
        }

        .clear-history button:hover {
            background-color: #0056b3;
        }
    </style>
</head>
<body>
    <h1>History</h1>
    <div class="history-container">
        {% if history_list %}
            {% for item in history_list %}
                <div class="history-item">
                    <a href="{{ url_for('view_report', report_id=item.id) }}">{{ item.description }}</a>
                    <div class="history-menu">
                        {% if item.status == 'pending' %}
                            <span class="status">{{ item.status }}</span>
                        {% else %}
                            <span class="status">{{ item.status }} &#10003;</span>
                        {% endif %}
                    </div>
                </div>
            {% endfor %}
        {% else %}
            <p>No history available.</p>
        {% endif %}
    </div>
    <div class="clear-history">
        <form method="post" action="{{ url_for('proatr.clear_history') }}">
            <button type="submit">Clear History</button>
        </form>
    </div>
</body>
</html>
    ''', history_list=history_list)



@proatr_bp.route('/clear_history', methods=['POST'])
def clear_history():
    conn = sqlite3.connect('database.db')
    conn.execute('DELETE FROM reconciliation_reports WHERE status = ?', ('pending',))
    conn.commit()
    conn.close()
    return redirect(url_for('proatr.history'))



@proatr_bp.route('/reports')
def reports():
    conn = sqlite3.connect('database.db')
    cursor = conn.execute('''
        SELECT id, user_email, bank_file, cash_file, date, status, company_name, report_number, bank_statement_output, cash_ledger_output, adjusted_bank_balance, adjusted_cash_balance FROM reconciliation_reports WHERE status = 'approved' ORDER BY date DESC
    ''')
    records = cursor.fetchall()
    conn.close()

    reports_list = []
    for record in records:
        report_item = {
            'id': record[0],
            'user_email': record[1],
            'bank_file': record[2],
            'cash_file': record[3],
            'date': record[4],
            'status': record[5],
            'company_name': record[6],
            'report_number': record[7],
            'bank_statement_output': record[8],
            'cash_ledger_output': record[9],
            'adjusted_bank_balance': record[10],
            'adjusted_cash_balance': record[11],
            'description': f"Report {record[7]} on {record[4]} - {record[5]}"
        }
        reports_list.append(report_item)

    return render_template_string('''
    <html>
<head>
    <title>Reports</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/5.15.3/css/all.min.css">
    <style>
        body {
            font-family: 'Arial', sans-serif;
            background: linear-gradient(135deg, #1e1e1e, #333);
            color: #ffffff;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            margin: 0;
        }

        .reports-container {
            width: 90%;
            max-width: 1200px;
            background: rgba(255, 255, 255, 0.1);
            backdrop-filter: blur(10px);
            border-radius: 15px;
            box-shadow: 0 4px 30px rgba(0, 0, 0, 0.1);
            padding: 20px;
            margin: 20px;
        }

        .report-item {
            display: flex;
            justify-content: space-between;
            align-items: center;
            background-color: rgba(255, 255, 255, 0.1);
            padding: 10px;
            margin-bottom: 10px;
            border-radius: 5px;
            box-shadow: 0 2px 10px rgba(0, 0, 0, 0.2);
        }

        .report-item a {
            text-decoration: none;
            color: #00ff00;
            font-weight: bold;
        }

        .report-menu {
            visibility: hidden;
        }

        .report-item:hover .report-menu {
            visibility: visible;
        }

        .report-menu i {
            cursor: pointer;
            margin-left: 10px;
            color: #00ff00;
        }

        .report-menu i:hover {
            color: #007bff;
        }

        .edit-form {
            display: none;
            flex-direction: column;
            gap: 10px;
        }

        .edit-form textarea,
        .edit-form input {
            width: 100%;
            padding: 10px;
            background: rgba(255, 255, 255, 0.1);
            color: #ffffff;
            border: 1px solid rgba(255, 255, 255, 0.3);
            border-radius: 5px;
        }

        .edit-form button {
            background-color: #00ff00;
            border: none;
            color: black;
            padding: 10px;
            text-align: center;
            text-decoration: none;
            display: inline-block;
            font-size: 16px;
            transition-duration: 0.4s;
            cursor: pointer;
            border-radius: 5px;
        }

        .edit-form button:hover {
            background-color: white;
            color: black;
        }

    </style>
    <script>
        function toggleEditForm(reportId) {
            const form = document.getElementById(`edit-form-${reportId}`);
            form.style.display = form.style.display === 'none' ? 'flex' : 'none';
        }

        function saveEdit(reportId) {
            const form = document.getElementById(`edit-form-${reportId}`);
            form.submit();
        }
    </script>
</head>
<body>
    <div class="reports-container">
        <h1>Approved Reports</h1>
        {% if reports_list %}
            {% for item in reports_list %}
                <div class="report-item">
                    <a href="{{ url_for('view_report', report_id=item.id) }}">{{ item.description }}</a>
                    <div class="report-menu">
                        <i class="fas fa-ellipsis-v" onclick="toggleEditForm({{ item.id }})"></i>
                        <div class="edit-form" id="edit-form-{{ item.id }}">
                            <form method="post" action="{{ url_for('edit_report', report_id=item.id) }}" onsubmit="event.preventDefault(); saveEdit({{ item.id }});">
                                <textarea name="bank_statement_output">{{ item.bank_statement_output }}</textarea>
                                <textarea name="cash_ledger_output">{{ item.cash_ledger_output }}</textarea>
                                <input type="number" name="adjusted_bank_balance" value="{{ item.adjusted_bank_balance }}">
                                <input type="number" name="adjusted_cash_balance" value="{{ item.adjusted_cash_balance }}">
                                <button type="submit">Save</button>
                            </form>
                            <form method="post" action="{{ url_for('delete_report', report_id=item.id) }}">
                                <button type="submit">Delete</button>
                            </form>
                        </div>
                    </div>
                </div>
            {% endfor %}
        {% else %}
            <p>No approved reports available.</p>
        {% endif %}
    </div>
</body>
</html>
    ''', reports_list=reports_list)




@app.route('/edit_report/<int:report_id>', methods=['POST'])
def edit_report(report_id):
    bank_statement_output = request.form['bank_statement_output']
    cash_ledger_output = request.form['cash_ledger_output']
    adjusted_bank_balance = request.form['adjusted_bank_balance']
    adjusted_cash_balance = request.form['adjusted_cash_balance']
    
    conn = sqlite3.connect('database.db')
    conn.execute('''
        UPDATE reconciliation_reports
        SET bank_statement_output = ?, cash_ledger_output = ?, adjusted_bank_balance = ?, adjusted_cash_balance = ?
        WHERE id = ?
    ''', (bank_statement_output, cash_ledger_output, adjusted_bank_balance, adjusted_cash_balance, report_id))
    conn.commit()
    conn.close()
    return redirect(url_for('proatr.reports'))

@app.route('/delete_report/<int:report_id>', methods=['POST'])
def delete_report(report_id):
    conn = sqlite3.connect('database.db')
    conn.execute('DELETE FROM reconciliation_reports WHERE id = ?', (report_id,))
    conn.commit()
    conn.close()
    return redirect(url_for('proatr.reports'))


    return '''
     <!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ProATR</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/5.15.3/css/all.min.css">
    <link rel="icon" type="image/png" href="Favicon/favicon-32x32.png" sizes="400x400">
    <link rel="icon" href="favicon/favicon-32x32.png" sizes="64x64" type="image/png">

    <style>
        /* Sidebar and Main Content Styles */
        body, html {
            height: 100%;
            margin: 0;
            font-family: Calibri Light, sans-serif;
            overflow-x: hidden;
        }

        #sidebar {
            background: #333;
            color: white;
            width: 250px;
            height: 100%;
            position: fixed;
            top: 0;
            left: -250px;
            transition: left 0.3s;
            padding-top: 60px;
        }

        #sidebar.open {
            left: 0;
        }

        .sidebar-toggle {
            font-size: 30px;
            cursor: pointer;
            position: fixed;
            top: 15px;
            left: 15px;
            transition: transform 0.3s;
            z-index: 2;
        }

        .sidebar-toggle:hover {
            color: #999;
        }

        .bar1, .bar2, .bar3 {
            width: 35px;
            height: 5px;
            background-color: #070707;
            margin: 6px 0;
            transition: 0.4s;
        }

        .change .bar1 {
            transform: rotate(-45deg) translate(-9px, 6px);
        }

        .change .bar2 { opacity: 0; }

        .change .bar3 {
            transform: rotate(45deg) translate(-8px, -8px);
        }

        #main-content {
            transition: margin-left 0.3s;
            padding: 16px;
            margin-left: 0;
            width: calc(100% - 16px);
            box-sizing: border-box;
        }

        #footer {
            background: #333;
            color: white;
            text-align: center;
            position: fixed;
            bottom: 0;
            width: 100%;
            padding: 10px;
        }

        /* File Upload Form Styles */
        form {
            background: rgba(255, 255, 255, 0.4);
            border-radius: 10px;
            padding: 20px;
            box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255, 255, 255, 0.18);
        }

        input[type="file"], select, input[type="submit"] {
            width: 100%;
            padding: 10px;
            margin: 8px 0;
            display: inline-block;
            border: 1px solid #ccc;
            border-radius: 4px;
            box-sizing: border-box;
            transition: border-color 0.3s ease-in-out;
        }

        input[type="submit"] {
            background-color: #4CAF50;
            color: white;
            border: none;
            cursor: pointer;
            transition: background-color 0.3s ease-in-out;
        }

        input[type="submit"]:hover {
            background-color: #45a049;
        }

        input[type="file"]:hover, select:hover {
            border-color: #45a049;
        }

        label {
            margin-top: 10px;
        }
        
        #loader {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100vw;
            height: 100vh;
            background: black;
            z-index: 9999;
            justify-content: center;
            align-items: center;
            overflow: hidden;
        }

        #loader img {
            width: auto;
            height: auto;
            max-width: 100%;
            max-height: 100%;
        }

        #main-content {
            transition: margin-left 0.3s;
            padding: 16px;
            padding-top: 70px; /* Adjust this value as needed */
            margin-left: 0;
            width: calc(100% - 16px);
            box-sizing: border-box;
        }

        form {
           background: rgba(255, 255, 255, 0.4);
           border-radius: 10px;
           padding: 20px;
           margin-top: 60px; /* Keeps form below the hamburger menu */
           margin-left: auto; /* Center the form horizontally */
           margin-right: auto; /* Center the form horizontally */
           max-width: 600px; /* Adjust this value as desired to control the form's width */
           box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
           backdrop-filter: blur(10px);
           border: 1px solid rgba(255, 255, 255, 0.18);
        }

    .switch {
  position: relative;
  display: inline-block;
  width: 60px;
  height: 34px;
}

.switch input { 
  opacity: 0;
  width: 0;
  height: 0;
}

.slider {
  position: absolute;
  cursor: pointer;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background-color: #ccc;
  -webkit-transition: .4s;
  transition: .4s;
}

.slider:before {
  position: absolute;
  content: "";
  height: 26px;
  width: 26px;
  left: 4px;
  bottom: 4px;
  background-color: white;
  -webkit-transition: .4s;
  transition: .4s;
}

input:checked + .slider {
  background-color: #000;
}

input:focus + .slider {
  box-shadow: 0 0 1px #000;
}

input:checked + .slider:before {
  -webkit-transform: translateX(26px);
  -ms-transform: translateX(26px);
  transform: translateX(26px);
}

/* Rounded sliders */
.slider.round {
  border-radius: 34px;
}

.slider.round:before {
  border-radius: 50%;
}

[data-theme="dark"] {
  background-color: #262626;
  color: white;
}

[data-theme="dark"] .slider {
  background-color: #4B4B4B;
}

#sidebar .menu-item a {
    color: white; /* Set the link color to white */
    text-decoration: none; /* Remove the underline */
    display: block; /* Make the link fill the container for better click area */
    padding: 10px 15px; /* Add some padding for spacing */
    transition: background-color 0.3s, color 0.3s; /* Smooth transition for hover effect */
}

#sidebar .menu-item a:hover, #sidebar .menu-item a:focus {
    background-color: #4b4b4b; /* Change the background color on hover/focus */
    color: #ffffff; /* Ensure text is white */
    text-decoration: none; /* Ensure no underline on hover/focus */
    border-radius: 4px; /* Optional: add rounding to match ChatGPT style */
}

/* Default light theme styles */
body {
  background-color: #fff;
  color: #000;
}

/* Dark theme styles */
body.dark {
  background-color: #262626;
  color: white;
}

/* Ensure links are also styled correctly */
body.dark a {
  color: white; /* Set link color to white in dark mode */
  text-decoration: none; /* Remove underline from links in dark mode */
}

#footer {
    background: #333;
    color: white;
    text-align: center;
    position: fixed;
    bottom: 0;
    width: 100%;
    padding: 5px 0; /* Reduced padding to decrease size */
    font-size: 0.8rem; /* Smaller font size */
    font-family: 'Roboto', 'Arial', sans-serif; /* ChatGPT-like fonts, make sure to have 'Roboto' font available */
    letter-spacing: 0.5px; /* Adjust letter spacing */
    border-top: 1px solid #444; /* Add a top border line */
    box-shadow: 0 -2px 4px rgba(0,0,0,0.1); /* Optional: Add a subtle shadow to the top of the footer */
    z-index: 10; /* Ensure footer is above other content */
}

body {
    padding-bottom: 40px; /* Add padding to the bottom of the body to prevent content from being hidden by the footer */
}


/* Hide scrollbar for Chrome, Safari and Opera */
::-webkit-scrollbar {
    display: none;
}

/* Hide scrollbar for Firefox */
body {
    scrollbar-width: none;
}

/* Hide scrollbar for IE, Edge */
body {
    -ms-overflow-style: none;
}

body, html {
  overflow: hidden; /* This will prevent scrolling */
  height: 100%; /* You might want to set a fixed height to your body and html */
}

.history-container {
    margin-top: 20px;
    padding: 10px;
}

.history-item {
    display: flex;
    justify-content: space-between;
    align-items: center;
    background-color: #f0f0f0;
    padding: 10px;
    margin-bottom: 10px;
    border-radius: 5px;
}

.history-item:hover .history-menu {
    visibility: visible;
}

.history-menu {
    visibility: hidden;
    cursor: pointer;
}

.no-history {
    display: none;
}

 .logo-image {
    border-radius: 50%; /* This creates a circular shape */
    width: 30px; /* Adjusted from 120px to 60px */
    height: 30px; /* Adjusted from 120px to 60px */
    object-fit: cover; /* This ensures the image covers the area without stretching */
}

form {
    background: rgba(255, 255, 255, 0.4);
    border-radius: 10px;
    padding: 20px;
    margin-top: 20px; /* Keeps form below the hamburger menu */
    margin-left: auto; /* Center the form horizontally */
    margin-right: auto; /* Center the form horizontally */
    margin-bottom: 2px; /* Add some space between the form and the footer */
    max-width: 600px; /* Adjust this value as desired to control the form's width */
    box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
    backdrop-filter: blur(10px);

    border: 1px solid rgba(255, 255, 255, 0.18);
}

.sidebar-toggle {
    font-size: 24px; /* Adjust icon size as needed */
    cursor: pointer;
    position: fixed;
    top: 15px;
    left: 15px;
    color: #070707; /* Icon color */
}

/* Adjustments for dark theme, if applicable */
[data-theme="dark"] .sidebar-toggle {
    color: #fff; /* Icon color in dark mode */
}

.menu-item i.fas {
    margin-right: 8px; /* Adjust the value as needed */
  }



    </style>
</head>
<body>
    <div class="sidebar-toggle" onclick="toggleSidebar()">
    <i class="fas fa-columns"></i> <!-- This is the Font Awesome icon for split terminal -->
</div>

    
    <div id="sidebar">
     <div  style="display: flex; align-items: center;">
     <img src="static/logo.png" alt="Logo" class="logo-image" style="width: 30px; height: auto; margin-right: 10px; margin-left: 10px;">
    <h3>ProATR</h3>
</div>
        <div class="menu-item"><a href="/analyze_transactions"><i class="fas fa-brain"></i>A.I - Analysis | Transactions</a></div>
        <div class="menu-item"><a href="/plugins"><i class="fas fa-plug"></i> Plugins </a></div>
           
    </div>
    </div>

 




    <div id="main-content">
    
      <label class="switch">
  <input type="checkbox" id="theme-toggle">
  <span class="slider round"></span>
</label>


        <div style="text-align: center; margin-bottom: 5px;">
    <img src="static/logo.png" alt="Logo" class="logo-image">
    <h3>Try to Upload Something</h3>
</div>


        <form method="post" enctype="multipart/form-data" onsubmit="showLoader()">
            <label for="file_category">Select file category:</label>
            <select name="file_category" id="file_category">
                <option value="bank_cash">Bank and Cash Files</option>
                <option value="transform">Transform Data File</option>
            </select><br><br>
            
            <div id="bank_file_group" style="display:none;">
                <label for="bank_file">Bank File</label><br>
                <input type="file" name="bank_file" id="bank_file">
            </div>
            <div id="cash_file_group" style="display:none;">
                <label for="cash_file">Cash File</label><br>
                <input type="file" name="cash_file" id="cash_file">
                
            </div>
            <div id="any_file_group" style="display:none;">
                <label for="file">MS Excel File</label><br>
                <input type="file" name="file" id="file">
            </div>
            
            <input type="submit" value="Upload" id="upload_button">
        </form>
        <!-- End of the Form -->
        <div id="loader">
            <img src="https://i.giphy.com/MydKZ8HdiPWALc0Lqf.webp" alt="Loading...">
        </div>
        <div id="comparison_results" style="display:none;">
            <h3>Comparison Results</h3>
            <p id="results_content"></p>
        </div>
    </div>
    <div id="footer">
        ProATR requires data transformation. Use the processed data for reconciliation.
    </div>
    <script>
        function toggleSidebar() {
    var sidebar = document.getElementById("sidebar");
    var icon = document.querySelector(".sidebar-toggle i"); // Select the icon within the sidebar-toggle div
    sidebar.classList.toggle("open");

    // Toggle icon class based on sidebar state
    if (sidebar.classList.contains("open")) {
        icon.classList.remove("fa-columns");
        icon.classList.add("fa-angle-right"); // Change to ">" symbol when sidebar is open
    } else {
        icon.classList.remove("fa-angle-right");
        icon.classList.add("fa-columns"); // Change back to split terminal icon when sidebar is closed
    }

    // Adjust main content margin if necessary
    if (sidebar.classList.contains("open")) {
        document.getElementById("main-content").style.marginLeft = "250px";
    } else {
        document.getElementById("main-content").style.marginLeft = "0";
    }
}


        // Start of the form interaction scripts
        document.addEventListener("DOMContentLoaded", function() {
            var fileCategory = document.getElementById('file_category');
            var bankFile = document.getElementById('bank_file');
            var cashFile = document.getElementById('cash_file');
            var anyFile = document.getElementById('file'); // For the Transform Data file
            var uploadButton = document.getElementById('upload_button');
            var compareButton = document.getElementById('compare_button');
            var bankFileGroup = document.getElementById('bank_file_group');
            var cashFileGroup = document.getElementById('cash_file_group');
            var anyFileGroup = document.getElementById('any_file_group');

            function updateFileInputs() {
                bankFileGroup.style.display = 'none';
                cashFileGroup.style.display = 'none';
                anyFileGroup.style.display = 'none';
                uploadButton.value = 'Upload';

                if (fileCategory.value === 'bank_cash') {
                    bankFileGroup.style.display = 'block';
                    cashFileGroup.style.display = 'block';
                } else if (fileCategory.value === 'transform') {
                    anyFileGroup.style.display = 'block';
                }
                updateButtonText();
            }

            function updateButtonText() {
                if (fileCategory.value === 'bank_cash' && bankFile.value && cashFile.value) {
                    uploadButton.value = 'Reconcile';
                } else if (fileCategory.value === 'transform' && anyFile.value) {
                    uploadButton.value = 'Transform Data';
                } else {
                    uploadButton.value = 'Upload';
                }
            }

            fileCategory.addEventListener('change', updateFileInputs);
            bankFile.addEventListener('change', updateButtonText);
            cashFile.addEventListener('change', updateButtonText);
            anyFile.addEventListener('change', updateButtonText);

            updateFileInputs();

            bankFile.addEventListener('change', updateCompareButtonVisibility);
            cashFile.addEventListener('change', updateCompareButtonVisibility);

            function updateCompareButtonVisibility() {
                compareButton.style.display = bankFile.value && cashFile.value ? 'block' : 'none';
            }

            updateCompareButtonVisibility();

            compareButton.addEventListener('click', function() {
                var formData = new FormData(document.querySelector('form'));
                fetch('/compare_transactions', {
                    method: 'POST',
                    body: formData
                })
                .then(response => response.json())
                .then(data => {
                    document.getElementById('results_content').textContent = data.comparisonResults;
                    document.getElementById('comparison_results').style.display = 'block';
                })
                .catch(error => console.error('Error:', error));
            });
        });

        function showLoader() {
            document.getElementById('loader').style.display = 'flex';
            setTimeout(function() {
                document.getElementById('loader').style.display = 'none';
                document.getElementById('myForm').submit();
            }, 20000000); // Adjust the time as per requirement
        }
        // End of the form interaction scripts
    </script>

 

    <script>
    document.addEventListener('DOMContentLoaded', () => {
  const themeToggle = document.getElementById('theme-toggle');
  const sidebarToggleBars = document.querySelectorAll('.sidebar-toggle div');

  // Apply the saved theme, if any, on page load
  const savedTheme = localStorage.getItem('theme') || 'light';
  document.body.className = savedTheme;  // Set the initial theme
  themeToggle.checked = savedTheme === 'dark'; // Update the toggle position
  updateSidebarToggleBarsColor(savedTheme); // Update the color of the sidebar toggle bars

  themeToggle.addEventListener('change', (e) => {
    const theme = e.target.checked ? 'dark' : 'light';
    document.body.className = theme;
    localStorage.setItem('theme', theme); // Save theme preference
    updateSidebarToggleBarsColor(theme); // Update the color of the sidebar toggle bars based on the theme
  });

  function updateSidebarToggleBarsColor(theme) {
    if (theme === 'dark') {
      sidebarToggleBars.forEach(bar => {
        bar.style.backgroundColor = '#fff'; // Change the color to white for dark mode
      });
    } else {
      sidebarToggleBars.forEach(bar => {
        bar.style.backgroundColor = '#070707'; // Change the color back to default for light mode
      });
    }
  }
});


   </script>




</body>
</html>

    '''


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

import pandas as pd
import openai

def analyze_transactions_with_openai(bank_file_path, cash_file_path):
    bank_df = pd.read_csv(bank_file_path)
    cash_df = pd.read_csv(cash_file_path)

    bank_transactions = bank_df.to_json(orient='records')
    cash_transactions = cash_df.to_json(orient='records')

    transactions_data = f"Bank transactions: {bank_transactions}\nCash transactions: {cash_transactions}"
    prompt = (
        "I have two sets of financial transaction data. One set is from a bank statement and the "
        "other is from a company's cash record. Analyze these transactions to identify any anomalies, "
        "such as transactions that appear only in one set but not the other, or values that don't match. "
        "Additionally, detect any patterns that could indicate errors or fraudulent activity. Summarize "
        "the findings clearly with headings for 'Anomalies Detected', 'Matched Transactions', and "
        "'Unmatched Transactions', and list the transactions under each category. Here are the data sets:\n\n"
        f"{transactions_data}\n\n"
        "Consider each transaction's date, amount, and description in your analysis."
    )

    try:
        response = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": "You are an accountant."},
                {"role": "user", "content": prompt}
            ]
        )
        analysis_result = response.choices[0].message['content']
        return analysis_result

    except Exception as e:
        return f"An error occurred: {str(e)}"

def heuristic_analysis(bank_df, cash_df):
    bank_df['Debit'] = bank_df['Debit'].replace('[\$,]', '', regex=True).astype(float)
    bank_df['Credit'] = bank_df['Credit'].replace('[\$,]', '', regex=True).astype(float)
    cash_df['Debit'] = cash_df['Debit'].replace('[\$,]', '', regex=True).astype(float)
    cash_df['Credit'] = cash_df['Credit'].replace('[\$,]', '', regex=True).astype(float)

    bank_df['Matched'] = False
    cash_df['Matched'] = False

    def match_transactions(df1, col1, df2, col2):
        for i, row1 in df1.iterrows():
            if not row1['Matched']:
                for j, row2 in df2.iterrows():
                    if not row2['Matched'] and row1[col1] == row2[col2]:
                        df1.at[i, 'Matched'] = True
                        df2.at[j, 'Matched'] = True
                        break

    match_transactions(cash_df, 'Debit', bank_df, 'Credit')
    match_transactions(cash_df, 'Credit', bank_df, 'Debit')

    matched_bank = bank_df[bank_df['Matched']]
    matched_cash = cash_df[cash_df['Matched']]
    unmatched_bank = bank_df[~bank_df['Matched']]
    unmatched_cash = cash_df[~cash_df['Matched']]
    
    matched_str = format_transactions(matched_bank, matched_cash)
    unmatched_str = format_transactions(unmatched_bank, unmatched_cash)
    
    return matched_str, unmatched_str

def format_transactions(bank_df, cash_df):
    def format_row(row):
        date = row['Date']
        transaction_id = row['Transaction ID']
        debit = f"${float(row['Debit']):,.2f} Debit" if not pd.isna(row['Debit']) else ""
        credit = f"${float(row['Credit']):,.2f} Credit" if not pd.isna(row['Credit']) else ""
        formatted_row = f"{date} - {transaction_id} - {debit}" if debit else f"{date} - {transaction_id} - {credit}"
        return formatted_row.strip()

    formatted_bank = "\n".join(format_row(row) for _, row in bank_df.iterrows())
    formatted_cash = "\n".join(format_row(row) for _, row in cash_df.iterrows())
    
    return f"Bank Transactions:\n{formatted_bank}\n\nCash Transactions:\n{formatted_cash}"

# Store the conversation context
conversation_context = []

@app.route('/analyze_transactions', methods=['POST'])
def analyze_transactions():
    bank_file = request.files.get('bank_file')
    cash_file = request.files.get('cash_file')

    if bank_file and cash_file and allowed_file(bank_file.filename) and allowed_file(cash_file.filename):
        bank_filename = secure_filename(bank_file.filename)
        cash_filename = secure_filename(cash_file.filename)
        bank_file_path = os.path.join(app.config['UPLOAD_FOLDER'], bank_filename)
        cash_file_path = os.path.join(app.config['UPLOAD_FOLDER'], cash_filename)
        bank_file.save(bank_file_path)
        cash_file.save(cash_file_path)

        analysis_result = analyze_transactions_with_openai(bank_file_path, cash_file_path)
        analysis_result = analysis_result.replace("\n", "<br>").replace("•", "<li>").replace("Matched Transactions:", "<h3>Matched Transactions:</h3><ul>").replace("Unmatched Transactions:", "</ul><h3>Unmatched Transactions:</h3><ul>") + "</ul>"

        # Save the analysis result in the conversation context
        conversation_context.append({"role": "assistant", "content": analysis_result})

        return jsonify({"analysis_result": analysis_result})
    else:
        return jsonify({"error": "Invalid file type or missing files"}), 400
    
@app.route('/process_vocal_request', methods=['POST'])
def process_vocal_request():
    data = request.get_json()
    request_text = data.get('request')

    if not request_text:
        return jsonify({'error': 'No request text provided'}), 400

    # Add the user request to the conversation context
    conversation_context.append({"role": "user", "content": request_text})

    try:
        response = openai.ChatCompletion.create(
            model="gpt-4",
            messages=conversation_context
        )
        response_text = response.choices[0].message['content']

        # Add the assistant response to the conversation context
        conversation_context.append({"role": "assistant", "content": response_text})

        return jsonify({'response': response_text})

    except Exception as e:
        return jsonify({'error': str(e)}), 500
    
@app.route('/process_chat_request', methods=['POST'])
def process_chat_request():
    data = request.get_json()
    chat_message = data.get('message')

    if not chat_message:
        return jsonify({'error': 'No message text provided'}), 400

    # Add the user message to the conversation context
    conversation_context.append({"role": "user", "content": chat_message})

    try:
        response = openai.ChatCompletion.create(
            model="gpt-4",
            messages=conversation_context
        )
        response_text = response.choices[0].message['content']

        # Add the assistant response to the conversation context
        conversation_context.append({"role": "assistant", "content": response_text})

        return jsonify({'response': response_text})

    except Exception as e:
        return jsonify({'error': str(e)}), 500
    
@app.route('/analyze_transactions', methods=['GET'])
def render_form():
    return render_template_string('''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Analyze Transactions</title>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;500;700&display=swap');
        @import url('https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0-beta3/css/all.min.css');

        :root {
            --background-color: #2a2a2a;
            --text-color: #e0e0e0;
            --form-background-color: rgba(128, 0, 0, 0.2);
            --button-background-color: rgba(128, 0, 0, 0.8);
            --button-hover-background-color: #00c6ff;
            --scrollbar-thumb-color: rgba(128, 0, 0, 0.6);
            --scrollbar-track-color: rgba(0, 0, 0, 0.1);
            --glass-border-color: rgba(255, 255, 255, 0.2);
            --glass-box-shadow: rgba(0, 0, 0, 0.2);
            --maroon-faint: rgba(128, 0, 0, 0.4);
            --neon-blue: #00c6ff;
            --dark-grey: #1c1c1c;
        }

        body {
            font-family: 'Roboto', sans-serif;
            display: flex;
            height: 100vh;
            margin: 0;
            background-color: var(--background-color);
            color: var(--text-color);
            overflow: hidden;
        }

        #left-panel, #right-panel {
            width: 50%;
            padding: 20px;
            box-sizing: border-box;
            display: flex;
            flex-direction: column;
            justify-content: center;
            align-items: center;
        }

        #left-panel {
            background-color: var(--background-color);
            border-right: 1px solid var(--glass-border-color);
        }

        #right-panel {
            background-color: var(--background-color);
        }

        #main-content {
            text-align: center;
            backdrop-filter: blur(10px);
            background-color: var(--form-background-color);
            padding: 40px;
            border-radius: 16px;
            box-shadow: 0 4px 30px var(--glass-box-shadow);
            border: 1px solid var(--glass-border-color);
            width: 80%;
            position: relative;
        }

        h2 {
            margin-bottom: 20px;
            color: white;
            text-shadow: 0 0 5px black;
        }

        form {
            display: flex;
            flex-direction: column;
            align-items: center;
        }

        .file-input-container {
            position: relative;
            display: flex;
            align-items: center;
            margin-bottom: 20px;
        }

        .file-input-container input[type="file"] {
            position: absolute;
            opacity: 0;
            width: 100%;
            height: 100%;
            cursor: pointer;
        }

        .file-input-label {
            display: flex;
            align-items: center;
            background-color: var(--button-background-color);
            color: white;
            padding: 10px 24px;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            transition: background-color 0.3s;
            box-shadow: 0 0 5px var(--button-background-color);
        }

        .file-input-label:hover {
            background-color: var(--button-hover-background-color);
        }

        .file-input-label i {
            margin-right: 10px;
        }

        .file-name {
            margin-left: 10px;
            font-size: 0.9em;
            color: var(--text-color);
        }

        .controls button {
            background-color: var(--button-background-color);
            color: white;
            padding: 8px 12px;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            transition: background-color 0.3s;
            box-shadow: 0 0 5px var(--button-background-color);
        }

        .controls button:hover {
            background-color: var(--button-hover-background-color);
        }

        #analyze-button {
            font-size: 24px;
            padding: 10px;
            display: flex;
            justify-content: center;
            align-items: center;
            width: 60px;
            height: 60px;
            margin-top: 20px;
        }

        .airplane {
            position: absolute;
            top: 50%;
            left: 50%;
            width: 50px;
            height: 50px;
            background: url('https://i.imgur.com/NFz3B8b.png') no-repeat center center / contain;
            transform: translate(-50%, -50%);
            animation: fly 2s linear forwards;
            display: none;
        }

        @keyframes fly {
            from {
                left: 50%;
            }
            to {
                left: 100%;
                opacity: 0;
            }
        }

        .modal {
            display: none;
            position: fixed;
            z-index: 1;
            left: 0;
            top: 0;
            width: 100%;
            height: 100%;
            overflow: auto;
            background-color: rgba(0, 0, 0, 0.4);
        }

        .modal-content {
            background-color: var(--form-background-color);
            margin: 15% auto;
            padding: 20px;
            border: 1px solid var(--scrollbar-track-color);
            width: 80%;
            max-width: 400px;
            border-radius: 16px;
        }

        .close {
            color: #aaa;
            float: right;
            font-size: 28px;
            font-weight: bold;
        }

        .close:hover,
        .close:focus {
            color: white;
            text-decoration: none;
            cursor: pointer;
        }

        .chat-button {
            background-color: var(--button-background-color);
            color: white;
            padding: 8px 12px;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            transition: background-color 0.3s;
            box-shadow: 0 0 5px var(--button-background-color);
        }

        .chat-button:hover {
            background-color: var(--button-hover-background-color);
        }

        /* Canvas Styling */
        #soundWaveCanvas {
            width: 90%;
            height: 80px;
            border-radius: 16px;
            margin-bottom: 20px;
            background-color: var(--form-background-color);
            box-shadow: 0 4px 30px var(--glass-box-shadow);
            border: 1px solid var(--glass-border-color);
        }

        .neon-loader {
            display: inline-block;
        }

        @keyframes dot-blink {
            0%, 20% {
                color: rgba(255, 255, 255, 0);
                text-shadow:
                0 0 5px var(--neon-blue),
                0 0 10px var(--neon-blue),
                0 0 20px var(--neon-blue),
                0 0 40px var(--neon-blue),
                0 0 80px var(--neon-blue),
                0 0 160px var(--neon-blue);
            }
            50%, 100% {
                color: var(--neon-blue);
                text-shadow: none;
            }
        }

        .dot:nth-child(1) {
            animation: dot-blink 1.5s infinite 0.5s;
        }

        .dot:nth-child(2) {
            animation: dot-blink 1.5s infinite 1s;
        }

        .dot:nth-child(3) {
            animation: dot-blink 1.5s infinite 1.5s;
        }

        .hidden {
            display: none;
        }

        .loading-outline {
            animation: outline-animation 1s infinite;
        }

        @keyframes outline-animation {
            0% {
                box-shadow: 0 0 5px var(--button-background-color), 0 0 10px var(--button-background-color), 0 0 20px var(--button-background-color), 0 0 40px var(--button-background-color);
            }
            50% {
                box-shadow: 0 0 5px var(--button-hover-background-color), 0 0 10px var(--button-hover-background-color), 0 0 20px var(--button-hover-background-color), 0 0 40px var(--button-hover-background-color);
            }
            100% {
                box-shadow: 0 0 5px var(--button-background-color), 0 0 10px var(--button-background-color), 0 0 20px var(--button-background-color), 0 0 40px var(--button-background-color);
            }
        }

        #output {
            width: 80%;
            height: 60%;
            background-color: var(--form-background-color);
            border-radius: 16px;
            padding: 20px;
            box-shadow: 0 4px 30px var(--glass-box-shadow);
            border: 1px solid var(--glass-border-color);
            overflow-y: auto;
            display: flex;
            flex-direction: column;
            justify-content: flex-start;
            align-items: center;
            margin-top: 20px;
        }

        #output::-webkit-scrollbar {
            width: 12px;
        }

        #output::-webkit-scrollbar-thumb {
            background-color: var(--scrollbar-thumb-color);
            border-radius: 10px;
            border: 3px solid var(--scrollbar-track-color);
        }

        #output::-webkit-scrollbar-track {
            background-color: var(--scrollbar-track-color);
        }

        .controls {
            margin-top: 20px;
            display: flex;
            gap: 10px;
        }

        .controls button {
            background-color: var(--button-background-color);
            color: white;
            padding: 8px 12px;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            transition: background-color 0.3s;
            box-shadow: 0 0 5px var(--button-background-color);
        }

        .controls button:hover {
            background-color: var (--button-hover-background-color);
        }

        .dock {
            position: absolute;
            top: 10px;
            left: 10px;
            display: flex;
            gap: 15px;
            padding: 10px 20px;
            background: rgba(255, 255, 255, 0.2);
            border-radius: 20px;
            backdrop-filter: blur(10px);
            box-shadow: 0 4px 30px var(--glass-box-shadow);
            border: 1px solid var(--glass-border-color);
        }

        .dock .icon {
            display: flex;
            justify-content: center;
            align-items: center;
            width: 40px;
            height: 40px;
            border-radius: 50%;
            transition: transform 0.3s, box-shadow 0.3s;
            cursor: pointer;
        }

        .dock .icon:hover {
            transform: scale(1.2);
            box-shadow: 0 0 10px var(--neon-blue);
        }

        .word-modal {
            display: none;
            position: fixed;
            z-index: 2;
            left: 50%;
            top: 50%;
            transform: translate(-50%, -50%);
            width: 80%;
            height: 80%;
            background-color: var(--dark-grey);
            border: 1px solid var(--glass-border-color);
            border-radius: 16px;
            padding: 20px;
            box-shadow: 0 4px 30px var(--glass-box-shadow);
            overflow: hidden;
        }

        .word-modal textarea {
            width: 100%;
            height: 80%;
            background-color: var(--background-color);
            color: var(--text-color);
            border: 1px solid var(--glass-border-color);
            border-radius: 8px;
            padding: 10px;
            box-sizing: border-box;
        }

        .close-btn {
            position: absolute;
            top: 10px;
            right: 10px;
            background-color: var(--button-background-color);
            border: none;
            color: white;
            padding: 5px 10px;
            cursor: pointer;
            border-radius: 4px;
            transition: background-color 0.3s;
        }

        .close-btn:hover {
            background-color: var(--button-hover-background-color);
        }

    </style>
</head>
<body>
    <div id="left-panel">
        <div class="dock">
            <div class="icon" onclick="goHome()">
                <i class="fas fa-home"></i>
            </div>
            <div class="icon" onclick="openWordModal()">
                <i class="fas fa-file-alt"></i>
            </div>
        </div>
        <div id="main-content">
            <h2 class="neon">Analyze Bank and Cash Transactions</h2>
            <form id="upload-form" method="post" enctype="multipart/form-data">
                <div class="file-input-container">
                    <label for="bank_file" class="file-input-label">
                        <i class="fas fa-upload"></i> Choose Bank File
                    </label>
                    <input type="file" id="bank_file" name="bank_file" onchange="updateFileName('bank_file_name', this)">
                    <span id="bank_file_name" class="file-name">No file chosen</span>
                </div>
                <div class="file-input-container">
                    <label for="cash_file" class="file-input-label">
                        <i class="fas fa-upload"></i> Choose Cash File
                    </label>
                    <input type="file" id="cash_file" name="cash_file" onchange="updateFileName('cash_file_name', this)">
                    <span id="cash_file_name" class="file-name">No file chosen</span>
                </div>
                <button type="button" id="analyze-button" class="controls"><i class="fas fa-paper-plane"></i></button>
                <p id="status-text" class="hidden">Waiting...</p>
            </form>
        </div>
    </div>
    <div id="right-panel">
        <canvas id="soundWaveCanvas"></canvas>
        <div id="output">
            <h2 class="neon">Analysis Results</h2>
            <p id="loading-text">Results will be displayed here.</p>
            <div class="controls">
                <button id="play"><i class="fas fa-play"></i></button>
                <button id="pause"><i class="fas fa-pause"></i></button>
                <button id="stop"><i class="fas fa-stop"></i></button>
                <button id="refresh"><i class="fas fa-sync-alt"></i></button>
                <button id="sound"><i class="fas fa-volume-up"></i></button>
                <button id="copy"><i class="fas fa-copy"></i></button>
                <button id="microphone"><i class="fas fa-microphone"></i></button>
                <button id="chat"><i class="fas fa-comments"></i></button>
            </div>
        </div>
    </div>
    <div class="airplane" id="airplane"></div>

    <div id="chatModal" class="modal">
        <div class="modal-content">
            <span class="close">&times;</span>
            <h2>Chat with AI</h2>
            <textarea id="chatInput" rows="4" style="width: 100%;"></textarea>
            <button class="chat-button" id="sendChat">Send</button>
        </div>
    </div>

    <div id="wordModal" class="word-modal">
        <button class="close-btn" onclick="closeWordModal()">✖</button>
        <h2>Microsoft Word</h2>
        <textarea id="wordContent"></textarea>
        <button class="chat-button" onclick="downloadWord()">Export</button>
    </div>

    <script>
        let isMuted = false;
        let isSpeaking = false;
        let speechSynthesisUtterance;
        let currentSpeechText = '';
        let currentSpeechIndex = 0;
        let isRecognitionActive = false;

        const canvas = document.getElementById('soundWaveCanvas');
        const canvasCtx = canvas.getContext('2d');

        let audioContext;
        let analyser;
        let microphoneStream;

        function createAudioContext() {
            audioContext = new (window.AudioContext || window.webkitAudioContext)();
            analyser = audioContext.createAnalyser();
            analyser.fftSize = 2048;
            const bufferLength = analyser.frequencyBinCount;
            const dataArray = new Uint8Array(bufferLength);

            function draw() {
                requestAnimationFrame(draw);

                analyser.getByteTimeDomainData(dataArray);

                canvasCtx.fillStyle = 'rgba(0, 0, 0, 0.1)';
                canvasCtx.fillRect(0, 0, canvas.width, canvas.height);

                canvasCtx.lineWidth = 2;
                canvasCtx.strokeStyle = 'rgb(0, 198, 255)';

                canvasCtx.beginPath();
                const sliceWidth = canvas.width * 1.0 / bufferLength;
                let x = 0;

                for (let i = 0; i < bufferLength; i++) {
                    const v = dataArray[i] / 128.0;
                    const y = v * canvas.height / 2;

                    if (i === 0) {
                        canvasCtx.moveTo(x, y);
                    } else {
                        canvasCtx.lineTo(x, y);
                    }

                    x += sliceWidth;
                }

                canvasCtx.lineTo(canvas.width, canvas.height / 2);
                canvasCtx.stroke();
            }

            draw();
        }

        function startMicrophone() {
            navigator.mediaDevices.getUserMedia({ audio: true })
                .then(stream => {
                    microphoneStream = audioContext.createMediaStreamSource(stream);
                    microphoneStream.connect(analyser);
                })
                .catch(err => {
                    console.error('Error accessing microphone: ' + err);
                });
        }

        function updateFileName(elementId, input) {
            document.getElementById(elementId).textContent = input.files[0] ? input.files[0].name : 'No file chosen';
        }

        document.getElementById('analyze-button').addEventListener('click', function() {
            if (!document.getElementById('bank_file').files.length || !document.getElementById('cash_file').files.length) {
                document.getElementById('loading-text').innerHTML = 'Please upload files';
                setTimeout(() => {
                    document.getElementById('loading-text').innerHTML = 'Results will be displayed here.';
                }, 2000);
                return;
            }

            const formData = new FormData();
            formData.append('bank_file', document.getElementById('bank_file').files[0]);
            formData.append('cash_file', document.getElementById('cash_file').files[0]);

            document.getElementById('analyze-button').innerHTML = '<i class="fas fa-stop"></i>';
            document.getElementById('status-text').classList.remove('hidden');
            document.getElementById('status-text').innerHTML = 'Waiting<span class="dot">.</span><span class="dot">.</span><span class="dot">.</span>';
            document.getElementById('loading-text').innerHTML = 'Processing Transactions<span class="dot">.</span><span class="dot">.</span><span class="dot">.</span>';

            fetch('/analyze_transactions', {
                method: 'POST',
                body: formData
            })
            .then(response => response.json())
            .then(data => {
                if (data.analysis_result) {
                    document.getElementById('loading-text').innerHTML = '<h2 class="neon">Findings</h2>' + data.analysis_result;
                    document.getElementById('status-text').innerHTML = 'Done <i class="fas fa-check"></i>';
                    setTimeout(() => {
                        document.getElementById('analyze-button').innerHTML = '<i class="fas fa-paper-plane"></i>';
                        document.getElementById('status-text').classList.add('hidden');
                    }, 2000);
                    currentSpeechText = data.analysis_result;
                    speakText(data.analysis_result);
                } else {
                    document.getElementById('loading-text').innerHTML = '<p>An error occurred: ' + data.error + '</p>';
                    document.getElementById('status-text').classList.add('hidden');
                    document.getElementById('analyze-button').innerHTML = '<i class="fas fa-paper-plane"></i>';
                }
            })
            .catch(error => {
                document.getElementById('loading-text').innerHTML = '<p>An error occurred: ' + error.message + '</p>';
                document.getElementById('status-text').classList.add('hidden');
                document.getElementById('analyze-button').innerHTML = '<i class="fas fa-paper-plane"></i>';
            });
        });

        function speakText(text) {
            if (isMuted || !text) return;

            if (isSpeaking && speechSynthesisUtterance) {
                window.speechSynthesis.pause();
                return;
            }

            speechSynthesisUtterance = new SpeechSynthesisUtterance(text);
            speechSynthesisUtterance.rate = 1;
            speechSynthesisUtterance.pitch = 1;
            speechSynthesisUtterance.volume = 1;

            const voices = window.speechSynthesis.getVoices();
            for (let voice of voices) {
                if (voice.name.includes('Google US English') || voice.name.includes('Google UK English Male') || voice.name.includes('Microsoft David - English (United States)') || voice.name.includes('Microsoft Zira - English (United States)')) {
                    speechSynthesisUtterance.voice = voice;
                    break;
                }
            }

            speechSynthesisUtterance.onstart = () => {
                isSpeaking = true;
                currentSpeechIndex = 0;
                visualizeSpeechSynthesis();
            };

            speechSynthesisUtterance.onend = () => {
                isSpeaking = false;
            };

            window.speechSynthesis.speak(speechSynthesisUtterance);
        }

        window.speechSynthesis.onvoiceschanged = () => {
            const results = document.getElementById('loading-text').innerText;
            speakText(results);
        };


        const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
        const recognition = new SpeechRecognition();

        recognition.onstart = function() {
            console.log('Voice recognition started. Try speaking into the microphone.');
        };

        recognition.onresult = function(event) {
            const current = event.resultIndex;
            const transcript = event.results[current][0].transcript;
            document.getElementById('loading-text').innerHTML = '<p>You said: ' + transcript + '</p>';
            processVocalRequest(transcript);
        };

        recognition.onerror = function(event) {
            console.log('Error occurred in recognition: ' + event.error);
        };

        recognition.onend = function() {
            if (isRecognitionActive) {
                recognition.start();
            }
        };

        document.getElementById('microphone').addEventListener('click', function() {
            if (!isRecognitionActive) {
                isRecognitionActive = true;
                recognition.start();
                if (!audioContext) {
                    createAudioContext();
                }
                startMicrophone();
                this.innerHTML = '<i class="fas fa-microphone"></i>';
            } else {
                isRecognitionActive = false;
                recognition.stop();
                if (microphoneStream) {
                    microphoneStream.disconnect();
                    microphoneStream = null;
                }
                this.innerHTML = '<i class="fas fa-microphone-slash"></i>';
            }
            this.classList.toggle('active');
        });

        function processVocalRequest(request) {
            fetch('/process_vocal_request', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ request: request }),
            })
            .then(response => response.json())
            .then(data => {
                if (data.response) {
                    document.getElementById('loading-text').innerHTML += '<p>Response: ' + data.response + '</p>';
                    currentSpeechText = data.response;
                    speakText(data.response);
                } else {
                    document.getElementById('loading-text').innerHTML += '<p>An error occurred: ' + data.error + '</p>';
                }
            })
            .catch(error => {
                document.getElementById('loading-text').innerHTML += '<p>An error occurred: ' + error.message + '</p>';
            });
        }

        document.getElementById('play').addEventListener('click', function() {
            if (isSpeaking) {
                window.speechSynthesis.resume();
            } else {
                speakText(currentSpeechText);
            }
        });

        document.getElementById('pause').addEventListener('click', function() {
            window.speechSynthesis.pause();
        });

        document.getElementById('stop').addEventListener('click', function() {
            window.speechSynthesis.cancel();
            isSpeaking = false;
        });

        document.getElementById('refresh').addEventListener('click', function() {
            location.reload();
        });

        document.getElementById('sound').addEventListener('click', function() {
            isMuted = !isMuted;
            this.innerHTML = isMuted ? '<i class="fas fa-volume-mute"></i>' : '<i class="fas fa-volume-up"></i>';
            if (isMuted) {
                window.speechSynthesis.cancel();
            } else {
                speakText(currentSpeechText);
            }
        });

        document.getElementById('copy').addEventListener('click', function() {
            const results = document.getElementById('loading-text').innerText;
            navigator.clipboard.writeText(results).then(() => {
                alert('Results copied to clipboard');
            });
        });

        var modal = document.getElementById("chatModal");
        var chatButton = document.getElementById("chat");
        var span = document.getElementsByClassName("close")[0];

        chatButton.onclick = function() {
            modal.style.display = "block";
        }

        span.onclick = function() {
            modal.style.display = "none";
        }

        window.onclick = function(event) {
            if (event.target == modal) {
                modal.style.display = "none";
            }
        }

        document.getElementById('sendChat').addEventListener('click', function() {
            const chatInput = document.getElementById('chatInput');
            const chatMessage = chatInput.value;

            chatInput.value = '';
            modal.style.display = "none";

            fetch('/process_chat_request', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ message: chatMessage }),
            })
            .then(response => response.json())
            .then(data => {
                if (data.response) {
                    document.getElementById('loading-text').innerHTML += '<p>Response: ' + data.response + '</p>';
                    currentSpeechText = data.response;
                    speakText(data.response);
                } else {
                    document.getElementById('loading-text').innerHTML += '<p>An error occurred: ' + data.error + '</p>';
                }
            })
            .catch(error => {
                document.getElementById('loading-text').innerHTML += '<p>An error occurred: ' + error.message + '</p>';
            });
        });

        function flyAirplane() {
            const airplane = document.getElementById('airplane');
            airplane.style.display = 'block';
            airplane.addEventListener('animationend', () => {
                airplane.style.display = 'none';
            });
        }

        function speakText(text) {
            if (isMuted || !text) return;

            if (isSpeaking && speechSynthesisUtterance) {
                window.speechSynthesis.pause();
                return;
            }

            speechSynthesisUtterance = new SpeechSynthesisUtterance(text);
            speechSynthesisUtterance.rate = 1;
            speechSynthesisUtterance.pitch = 1;
            speechSynthesisUtterance.volume = 1;

            const voices = window.speechSynthesis.getVoices();
            for (let voice of voices) {
                if (voice.name.includes('Google US English') || voice.name.includes('Google UK English Male') || voice.name.includes('Microsoft David - English (United States)') || voice.name.includes('Microsoft Zira - English (United States)')) {
                    speechSynthesisUtterance.voice = voice;
                    break;
                }
            }

            speechSynthesisUtterance.onstart = () => {
                isSpeaking = true;
                currentSpeechIndex = 0;
                visualizeSpeechSynthesis();
            };

            speechSynthesisUtterance.onend = () => {
                isSpeaking = false;
            };

            window.speechSynthesis.speak(speechSynthesisUtterance);
        }

        window.speechSynthesis.onvoiceschanged = () => {
            const results = document.getElementById('loading-text').innerText;
            speakText(results);
        };

        function visualizeSpeechSynthesis() {
            if (!audioContext) {
                createAudioContext();
            }
            const source = audioContext.createMediaElementSource(speechSynthesisUtterance);
            source.connect(analyser);
            source.connect(audioContext.destination);
        }

        // Function to navigate to the home page
        function goHome() {
            window.location.href = 'home.html'; // Change this to the actual path of your home page
        }

        function openWordModal() {
            document.getElementById('wordModal').style.display = 'block';
        }

        function closeWordModal() {
            document.getElementById('wordModal').style.display = 'none';
        }

        function downloadWord() {
            // Your logic to export content as Word document
        }

    </script>
</body>
</html>


                                                                  
    ''')

app.register_blueprint(proatr_bp, url_prefix='/proatr')




if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
