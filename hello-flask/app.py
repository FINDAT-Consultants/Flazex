from pathlib import Path
from flask import (
    Flask,
    render_template_string,
    request,
    send_from_directory,
    url_for,
)
from werkzeug.utils import secure_filename
import pandas as pd

# ---------------------------------------------------------------------------
# Configuration                                                             --
# ---------------------------------------------------------------------------
BASE_DIR      = Path(__file__).resolve().parent
UPLOAD_DIR    = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

ALLOWED_EXTENSIONS = {"csv"}

app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = UPLOAD_DIR


# ---------------------------------------------------------------------------
# Helper functions                                                          --
# ---------------------------------------------------------------------------
def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def money_to_float(series: pd.Series) -> pd.Series:
    """Convert '$1,234.56'-style strings to floats; keep NaNs intact."""
    return (
        series.replace(r"[,\$]", "", regex=True)
        .replace("", pd.NA)
        .astype(float)
    )


def process_files(bank_file: Path, cash_file: Path):
    # ---------------- Load & clean -----------------------------------------
    bank_df = pd.read_csv(bank_file)
    cash_df = pd.read_csv(cash_file)

    for df in (bank_df, cash_df):
        df["Debit"]         = money_to_float(df["Debit"])
        df["Credit"]        = money_to_float(df["Credit"])
        df["Balance_float"] = money_to_float(df["Balance"])

    closing_balance_bank = bank_df["Balance_float"].iloc[-1]
    closing_balance_cash = cash_df["Balance_float"].iloc[-1]

    # ---------------- Match opposite entries -------------------------------
    bank_df["Matched"] = False
    cash_df["Matched"] = False

    def _match(src, col_src, tgt, col_tgt):
        for i, row in src.loc[src[col_src].notna() & ~src["Matched"]].iterrows():
            j = tgt.loc[
                (tgt[col_tgt] == row[col_src]) & tgt[col_tgt].notna() & ~tgt["Matched"]
            ].first_valid_index()
            if j is not None:
                src.at[i, "Matched"] = True
                tgt.at[j, "Matched"] = True

    _match(cash_df, "Debit",  bank_df, "Credit")
    _match(cash_df, "Credit", bank_df, "Debit")

    # ---------------- Unmatched totals -------------------------------------
    deposit_in_transit           = cash_df.loc[~cash_df["Matched"], "Debit"].sum()
    outstanding_checks           = cash_df.loc[~cash_df["Matched"], "Credit"].sum()
    receivable_collected_by_bank = bank_df.loc[~bank_df["Matched"], "Credit"].sum()
    service_charges              = bank_df.loc[~bank_df["Matched"], "Debit"].sum()

    # ---------------- Adjusted balances ------------------------------------
    adjusted_bank_balance = (
        closing_balance_bank + deposit_in_transit - outstanding_checks
    )
    adjusted_cash_balance = (
        closing_balance_cash + receivable_collected_by_bank - service_charges
    )

    # ---------------- Save Excel with unmatched ----------------------------
    output_excel = UPLOAD_DIR / "reconciliation_output.xlsx"
    with pd.ExcelWriter(output_excel) as writer:
        cash_df.loc[~cash_df["Matched"]].to_excel(writer, sheet_name="Unmatched Cash")
        bank_df.loc[~bank_df["Matched"]].to_excel(writer, sheet_name="Unmatched Bank")

    # ---------------- Nicely formatted text blocks -------------------------
    bank_statement_text = f"""Bank Statement
- Balance as per bank statement: ${closing_balance_bank:,.2f}
- Add: Deposit in transit:       ${deposit_in_transit:,.2f}
- Deduct: Outstanding checks:    ${outstanding_checks:,.2f}
- Adjusted bank balance:         ${adjusted_bank_balance:,.2f}"""

    cash_ledger_text = f"""Cash Ledger
- Balance as per cash record:        ${closing_balance_cash:,.2f}
- Add: Receivable collected by bank: ${receivable_collected_by_bank:,.2f}
- Deduct: Service charges:           ${service_charges:,.2f}
- Adjusted cash balance:             ${adjusted_cash_balance:,.2f}"""

    return bank_statement_text, cash_ledger_text, output_excel.name


# ---------------------------------------------------------------------------
# Routes                                                                    --
# ---------------------------------------------------------------------------
@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        bank_file = request.files.get("bank_file")
        cash_file = request.files.get("cash_file")

        # --- Validation -----------------------------------------------------
        if not (bank_file and cash_file):
            return "Both files are required.", 400
        if not (allowed_file(bank_file.filename) and allowed_file(cash_file.filename)):
            return "Only .csv files are allowed.", 400

        # --- Persist uploads ------------------------------------------------
        bank_path = UPLOAD_DIR / secure_filename(bank_file.filename)
        cash_path = UPLOAD_DIR / secure_filename(cash_file.filename)
        bank_file.save(bank_path)
        cash_file.save(cash_path)

        # --- Reconcile ------------------------------------------------------
        bank_out, cash_out, excel_filename = process_files(bank_path, cash_path)

        return render_template_string(
            TEMPLATE_RESULT,
            bank_statement_output=bank_out,
            cash_ledger_output=cash_out,
            download_url=url_for("download_file", filename=excel_filename),
        )

    # GET request → upload form
    return render_template_string(TEMPLATE_FORM)


@app.route("/download/<path:filename>")
def download_file(filename):
    """Serve the generated Excel file."""
    return send_from_directory(
        app.config["UPLOAD_FOLDER"], filename, as_attachment=True
    )


# ---------------------------------------------------------------------------
# HTML templates (inline for a single-file demo, could live in /templates)  --
# ---------------------------------------------------------------------------
TEMPLATE_FORM = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Bank & Cash Reconciliation</title>
  <link rel="stylesheet"
        href="https://stackpath.bootstrapcdn.com/bootstrap/4.3.1/css/bootstrap.min.css">
  <style>
    body{
      height:100vh;display:flex;align-items:center;justify-content:center;
      background:linear-gradient(135deg,#1e3c72 0%,#2a5298 100%);
      font-family:'Segoe UI',Tahoma,Geneva,Verdana,sans-serif;color:#fff
    }
    .card{background:rgba(255,255,255,0.1);backdrop-filter:blur(6px);
          border-radius:20px;padding:40px;box-shadow:0 8px 32px 0 rgba(31,38,135,.37)}
    input[type=file]{color:#fff}
  </style>
</head>
<body>
  <div class="card text-center">
    <h1 class="mb-4">Upload Bank & Cash CSVs</h1>
    <form method="post" enctype="multipart/form-data">
      <div class="form-group">
        <input type="file" name="bank_file" class="form-control-file" required>
      </div>
      <div class="form-group">
        <input type="file" name="cash_file" class="form-control-file" required>
      </div>
      <button type="submit" class="btn btn-light btn-lg mt-3">Upload & Reconcile</button>
    </form>
    <small class="d-block mt-3">Accepted format: .csv</small>
  </div>
</body>
</html>
"""

TEMPLATE_RESULT = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Reconciliation Report</title>
  <link rel="stylesheet"
        href="https://stackpath.bootstrapcdn.com/bootstrap/4.3.1/css/bootstrap.min.css">
  <style>
    body{
      min-height:100vh;background:linear-gradient(135deg,#1e3c72 0%,#2a5298 100%);
      font-family:'Segoe UI',Tahoma,Geneva,Verdana,sans-serif;color:#fff;padding-top:50px
    }
    .container{max-width:800px}
    .report-block{
       background:rgba(255,255,255,0.1);backdrop-filter:blur(6px);
       border-radius:20px;padding:30px;margin-bottom:30px;
       box-shadow:0 8px 32px 0 rgba(31,38,135,.37)
    }
    pre{white-space:pre-wrap;font-size:1rem}
  </style>
</head>
<body>
  <div class="container text-center">
    <h1 class="mb-5">Bank & Cash Reconciliation</h1>

    <div class="report-block">
      <pre>{{ bank_statement_output }}</pre>
    </div>

    <div class="report-block">
      <pre>{{ cash_ledger_output }}</pre>
    </div>

    <a href="{{ download_url }}" class="btn btn-light btn-lg">
      Download Excel Report
    </a>
  </div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Entry point                                                               --
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Listening on 0.0.0.0 makes it reachable via EC2’s public IP / domain
    app.run(host="0.0.0.0", port=5000, debug=False)
