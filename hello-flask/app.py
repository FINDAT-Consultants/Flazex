from flask import Flask, render_template_string, request, send_from_directory, url_for
import pandas as pd
import os
from werkzeug.utils import secure_filename

# ----------------------------------------------------------------------
# Basic configuration
# ----------------------------------------------------------------------
BASE_DIR   = os.path.abspath(os.path.dirname(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.config.update(
    UPLOAD_FOLDER     = UPLOAD_DIR,
    ALLOWED_EXTENSIONS = {"csv"},
)

# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in app.config["ALLOWED_EXTENSIONS"]


def money_to_float(series: pd.Series) -> pd.Series:
    """Strip $ and commas and convert to float; leave NaNs untouched."""
    return (
        series.replace("[\\$,]", "", regex=True)
        .replace("", pd.NA)
        .astype(float)
    )


def process_files(bank_file_path: str, cash_file_path: str):
    # ---------- Load & clean -------------------------------------------------
    bank_df = pd.read_csv(bank_file_path)
    cash_df = pd.read_csv(cash_file_path)

    for df in (bank_df, cash_df):
        df["Debit"]  = money_to_float(df["Debit"])
        df["Credit"] = money_to_float(df["Credit"])
        df["Balance_float"] = money_to_float(df["Balance"])

    closing_balance_bank = bank_df["Balance_float"].iloc[-1]
    closing_balance_cash = cash_df["Balance_float"].iloc[-1]

    # ---------- Matching -----------------------------------------------------
    bank_df["Matched"] = False
    cash_df["Matched"] = False

    def match(df_src, col_src, df_tgt, col_tgt):
        for i, r1 in df_src[df_src[col_src].notna() & ~df_src["Matched"]].iterrows():
            j = df_tgt[
                (df_tgt[col_tgt] == r1[col_src])
                & df_tgt[col_tgt].notna()
                & ~df_tgt["Matched"]
            ].first_valid_index()
            if j is not None:
                df_src.at[i, "Matched"] = True
                df_tgt.at[j, "Matched"] = True

    match(cash_df, "Debit",  bank_df, "Credit")
    match(cash_df, "Credit", bank_df, "Debit")

    # ---------- Unmatched totals --------------------------------------------
    deposit_in_transit          = cash_df.loc[~cash_df["Matched"], "Debit"].sum()
    outstanding_checks          = cash_df.loc[~cash_df["Matched"], "Credit"].sum()
    receivable_collected_by_bank = bank_df.loc[~bank_df["Matched"], "Credit"].sum()
    service_charges             = bank_df.loc[~bank_df["Matched"], "Debit"].sum()

    # ---------- Adjusted balances -------------------------------------------
    adjusted_bank_balance = closing_balance_bank + deposit_in_transit - outstanding_checks
    adjusted_cash_balance = closing_balance_cash + receivable_collected_by_bank - service_charges

    # ---------- Save Excel with unmatched -----------------------------------
    output_excel_path = os.path.join(UPLOAD_DIR, "reconciliation_output.xlsx")
    with pd.ExcelWriter(output_excel_path) as writer:
        cash_df.loc[~cash_df["Matched"]].to_excel(writer, sheet_name="Unmatched Cash")
        bank_df.loc[~bank_df["Matched"]].to_excel(writer, sheet_name="Unmatched Bank")

    # ---------- Rendered strings --------------------------------------------
    bank_statement_output = (
        f"Bank Statement\n"
        f"- Balance as per bank statement: ${closing_balance_bank:,.2f}\n"
        f"- Add: Deposit in transit:       ${deposit_in_transit:,.2f}\n"
        f"- Deduct: Outstanding checks:    ${outstanding_checks:,.2f}\n"
        f"- Adjusted bank balance:         ${adjusted_bank_balance:,.2f}"
    )

    cash_ledger_output = (
        f"Cash Ledger\n"
        f"- Balance as per cash record:        ${closing_balance_cash:,.2f}\n"
        f"- Add: Receivable collected by bank: ${receivable_collected_by_bank:,.2f}\n"
        f"- Deduct: Service charges:           ${service_charges:,.2f}\n"
        f"- Adjusted cash balance:             ${adjusted_cash_balance:,.2f}"
    )

    return bank_statement_output, cash_ledger_output


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------
@app.route("/", methods=["GET", "POST"])
def upload_file():
    if request.method == "POST":
        bank_file = request.files.get("bank_file")
        cash_file = request.files.get("cash_file")

        if not (bank_file and cash_file):
            return "Both files are required", 400
        if not (allowed_file(bank_file.filename) and allowed_file(cash_file.filename)):
            return "Only .csv files are allowed", 400

        bank_path = os.path.join(UPLOAD_DIR, secure_filename(bank_file.filename))
        cash_path = os.path.join(UPLOAD_DIR, secure_filename(cash_file.filename))
        bank_file.save(bank_path)
        cash_file.save(cash_path)

        bank_out, cash_out = process_files(bank_path, cash_path)

        return render_template_string(
            TEMPLATE_RESULT,
            bank_statement_output=bank_out,
            cash_ledger_output=cash_out,
            download_url=url_for("download_file"),
        )

    return TEMPLATE_FORM


@app.route("/download")
def download_file():
    return send_from_directory(UPLOAD_DIR, "reconciliation_output.xlsx", as_attachment=True)


# ----------------------------------------------------------------------
# HTML templates (kept as raw strings to avoid Jinja quoting headaches)
# ----------------------------------------------------------------------
TEMPLATE_FORM = r"""
<!doctype html>
<html lang="en"><head><title>Upload Files</title>
<style>
/* (same CSS you already had) */
</style></head><body>
<div class="container">
  <h1>Upload Bank and Cash Files</h1>
  <form method="post" enctype="multipart/form-data">
    <input type="file" name="bank_file"><br>
    <input type="file" name="cash_file"><br>
    <input type="submit" value="Upload">
  </form>
</div>
<div class="footer">Make sure you use the appropriate CSV upload templates.</div>
</body></html>
"""

TEMPLATE_RESULT = r"""
<!doctype html><html lang="en"><head>
<meta charset="UTF-8"><title>Reconciliation Report</title>
<link rel="stylesheet" href="https://stackpath.bootstrapcdn.com/bootstrap/4.3.1/css/bootstrap.min.css">
<style>
/* (same CSS you already had) */
</style></head><body>
<div class="container">
  <h1 class="header">Bank and Cash Reconciliation</h1>
  <pre class="output">{{ bank_statement_output }}</pre>
  <pre class="output">{{ cash_ledger_output }}</pre>
  <a class="btn btn-primary" href="{{ download_url }}">Download Excel Report</a>
</div>
<div class="footer">© 2025 FINDAT. All rights reserved.</div>
</body></html>
"""

# ----------------------------------------------------------------------
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0")
