from flask import Flask, render_template_string, request, send_from_directory
import pandas as pd
import os
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads/'
app.config['ALLOWED_EXTENSIONS'] = {'csv'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

def process_files(bank_file_path, cash_file_path):
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

    return bank_statement_output, cash_ledger_output
   # Cleaning data
    bank_df['Debit'] = bank_df['Debit'].replace('[\$,]', '', regex=True).astype(float)
    bank_df['Credit'] = bank_df['Credit'].replace('[\$,]', '', regex=True).astype(float)
    cash_df['Debit'] = cash_df['Debit'].replace('[\$,]', '', regex=True).astype(float)
    cash_df['Credit'] = cash_df['Credit'].replace('[\$,]', '', regex=True).astype(float)

    # Extracting closing balances
    closing_balance_bank = bank_df.iloc[-1]['Balance']
    closing_balance_cash = cash_df.iloc[-1]['Balance']

    # Converting closing balances to float
    closing_balance_bank_float = float(closing_balance_bank.replace('$', '').replace(',', ''))
    closing_balance_cash_float = float(closing_balance_cash.replace('$', '').replace(',', ''))

    # Identifying unmatched transactions
    unmatched_cash_debits = cash_df[cash_df['Debit'].notna() & ~cash_df['Debit'].isin(bank_df['Credit'].dropna())]
    unmatched_bank_credits = bank_df[bank_df['Credit'].notna() & ~bank_df['Credit'].isin(cash_df['Debit'].dropna())]
    unmatched_cash_credits = cash_df[cash_df['Credit'].notna() & ~cash_df['Credit'].isin(bank_df['Debit'].dropna())]
    unmatched_bank_debits = bank_df[bank_df['Debit'].notna() & ~bank_df['Debit'].isin(cash_df['Credit'].dropna())]

    # Calculating totals for unmatched transactions
    deposit_in_transit = unmatched_cash_debits['Debit'].sum()
    outstanding_checks = unmatched_cash_credits['Credit'].sum()
    receivable_collected_by_bank = unmatched_bank_credits['Credit'].sum()
    service_charges = unmatched_bank_debits['Debit'].sum()

    # Calculating adjusted balances
    adjusted_bank_balance = closing_balance_bank_float + deposit_in_transit - outstanding_checks
    adjusted_cash_balance = closing_balance_cash_float + receivable_collected_by_bank - service_charges


    # Format the output as per your new code
    bank_statement_output = f"""
    Bank Statement
    - Balance as per bank statement: ${closing_balance_bank}
    - Add: Deposit in transit: ${deposit_in_transit:,.2f}
    - Deduct: Outstanding checks: ${outstanding_checks:,.2f}
    - Adjusted bank balance: ${adjusted_bank_balance:,.2f}
    """

    cash_ledger_output = f"""
    Cash Ledger
    - Balance as per Cash record: ${closing_balance_cash}
    - Add: Receivable collected by bank: ${receivable_collected_by_bank:,.2f}
    - Interest earned: $0.00
    - Deduction: NSF check: $0.00
    - Service charges: ${service_charges:,.2f}
    - Error on check: $0.00
    - Adjusted cash balance: ${adjusted_cash_balance:,.2f}
    """

    return bank_statement_output, cash_ledger_output

@app.route('/', methods=['GET', 'POST'])
def upload_file():
    if request.method == 'POST':
        # File upload handling code
        bank_file = request.files['bank_file']
        cash_file = request.files['cash_file']

        if bank_file and allowed_file(bank_file.filename) and cash_file and allowed_file(cash_file.filename):
            bank_filename = secure_filename(bank_file.filename)
            cash_filename = secure_filename(cash_file.filename)
            bank_file_path = os.path.join(app.config['UPLOAD_FOLDER'], bank_filename)
            cash_file_path = os.path.join(app.config['UPLOAD_FOLDER'], cash_filename)

            bank_file.save(bank_file_path)
            cash_file.save(cash_file_path)

            # Process the files and get output
            bank_statement_output, cash_ledger_output = process_files(bank_file_path, cash_file_path)

            return render_template_string(''' <!doctype html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Reconciliation Report</title>
            <link href="https://stackpath.bootstrapcdn.com/bootstrap/4.3.1/css/bootstrap.min.css" rel="stylesheet">
            <style>
                body {
                    font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                    background-image: url('https://c4.wallpaperflare.com/wallpaper/160/754/422/glassy-abstract-tiles-4k-wallpaper-preview.jpg'); /* Replace with your image URL */
                    background-size: cover;
                    background-attachment: fixed;
                }
                .container {
                    max-width: 900px;
                    margin-top: 50px;
                    background-color: rgba(255, 255, 255, 0.8);
                    padding: 20px;
                    border-radius: 8px;
                    box-shadow: 0 0 10px rgba(0,0,0,0.1);
                }
                .header {
                    color: #333;
                    margin-bottom: 30px;
                }
                .footer {
                    text-align: center;
                    margin-top: 30px;
                    font-size: 0.9em;
                    color: #fff; /* Updated color to white */
                }
                .output {
                    font-family: 'Courier New', Courier, monospace;
                }
            </style>
        </head>
        <body>
            <div class="container">
                <h1 class="header">Bank and Cash Reconciliation</h1>
                <div class="report">
                    <div class="output">
                        <pre>{{ bank_statement_output }}</pre>
                        <pre>{{ cash_ledger_output }}</pre>
                        <a href="{{ url_for('download_file') }}" class="btn btn-primary">Download Excel Report</a>

                    </div>
                </div>
            </div>
            <div class="footer">
                © 2023 FINDAT. All rights reserved.
            </div>
            <script src="https://code.jquery.com/jquery-3.3.1.slim.min.js"></script>
            <script src="https://cdnjs.cloudflare.com/ajax/libs/popper.js/1.14.7/umd/popper.min.js"></script>
            <script src="https://stackpath.bootstrapcdn.com/bootstrap/4.3.1/js/bootstrap.min.js"></script>
        </body>
        </html>
        ''', bank_statement_output=bank_statement_output, cash_ledger_output=cash_ledger_output)

    return '''

   <!doctype html>
<html>
<head>
    <title>Upload Files</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            background-color: #f4f4f4;
            display: flex;
            justify-content: center;
            align-items: center;
            height: 100vh;
            margin: 0;
        }

        .container {
            text-align: center;
            background: rgba(0, 0, 0, 0.6); /* Dark black transparent background */
            color: #fff; /* White text color */
            border-radius: 10px;
            padding: 40px;
            box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
            backdrop-filter: blur(5px); /* Glassy effect */
        }

        h1 {
            margin-bottom: 20px;
        }

        input[type=file] {
            margin: 10px 0;
            color: #fff; /* White text color for file inputs */
        }

        input[type=submit] {
            border: none;
            outline: none;
            padding: 10px 20px;
            border-radius: 5px;
            background-color: #007bff;
            color: white;
            cursor: pointer;
            transition: background-color 0.3s ease;
        }

        input[type=submit]:hover {
            background-color: #0056b3;
        }

        .footer {
            position: absolute;
            bottom: 10px;
            width: 100%;
            text-align: center;
            font-size: 0.9em;
            color: #333; /* White text color for footer */
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>Upload Bank and Cash Files</h1>
        <form method="post" enctype="multipart/form-data">
            <input type="file" name="bank_file"><br>
            <input type="file" name="cash_file"><br>
            <input type="submit" value="Upload">
        </form>
    </div>
    <div class="footer">
        Make sure you use the appropriate CSV Upload Templates to get the desired output
    </div>
</body>
</html>

    '''

@app.route('/download')
def download_file():
    # Make sure the filename matches what you are saving in process_files
    filename = 'reconciliation_output.xlsx'
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename, as_attachment=True)


if __name__ == '__main__':
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    app.run(debug=True)
