import base64
import csv
import glob
import hashlib
import json
import os
from datetime import date as date_type, datetime

import google.generativeai as genai
import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, url_for

load_dotenv()

app = Flask(__name__)

ENV_DOMAIN            = os.getenv("FAKTUROWNIA_DOMAIN", "")
ENV_TOKEN             = os.getenv("FAKTUROWNIA_TOKEN", "")
GDRIVE_API_KEY        = os.getenv("GOOGLE_DRIVE_API_KEY", "")
GDRIVE_ROOT_FOLDER    = os.getenv("GOOGLE_DRIVE_ROOT_FOLDER", "")
GDRIVE_API            = "https://www.googleapis.com/drive/v3/files"
GEMINI_API_KEY        = os.getenv("GEMINI_API_KEY", "")
OCR_DATA_FILE         = os.path.join(os.path.dirname(__file__), "faktury_ocr.json")
TRANSACTIONS_CACHE    = os.path.join(os.path.dirname(__file__), "transakcje_cache.json")
INVOICES_COST_CACHE   = os.path.join(os.path.dirname(__file__), "faktury_kosztowe_cache.json")
INVOICES_INCOME_CACHE = os.path.join(os.path.dirname(__file__), "faktury_przychodowe_cache.json")
MANUAL_ACTIONS_FILE   = os.path.join(os.path.dirname(__file__), "manual_actions.json")

STATUS_LABELS = {
    "issued": "Wystawiona",
    "sent": "Wysłana",
    "paid": "Opłacona",
    "partial": "Częściowo opłacona",
    "unpaid": "Nieopłacona",
    "overdue": "Przeterminowana",
    "rejected": "Odrzucona",
    "canceled": "Anulowana",
    "draft": "Szkic",
}


def fetch_and_build(invoice_type):
    domain, token = ENV_DOMAIN, ENV_TOKEN
    error, invoices = None, []

    if not domain or not token:
        return invoices, [], None, None, "Brak konfiguracji w pliku .env"

    try:
        url = f"https://{domain}.fakturownia.pl/invoices.json"
        params = {"api_token": token, "per_page": 100, "page": 1}
        if invoice_type == "cost":
            params["income"] = "no"
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        raw = resp.json()

        for inv in raw:
            issue_date = inv.get("issue_date") or ""
            # Format date as dd/mm for display, keep full date for sorting
            if len(issue_date) == 10:
                day_month = f"{issue_date[8:10]}/{issue_date[5:7]}"
            else:
                day_month = issue_date
            full_date = issue_date  # YYYY-MM-DD for data-order

            net   = float(inv.get("price_net",   0) or 0)
            gross = float(inv.get("price_gross", 0) or 0)
            currency = inv.get("currency", "PLN") or "PLN"
            gov_id     = inv.get("gov_id") or ""
            gov_status = inv.get("gov_status") or ""

            invoices.append({
                "number":   inv.get("number", "—"),
                "seller":   inv.get("seller_name", "—"),
                "buyer":    inv.get("buyer_name", "—"),
                "date":      day_month,
                "full_date": full_date,
                "month":     issue_date[:7],  # "YYYY-MM" for JS filter
                "net":      net,
                "gross":    gross,
                "vat":      round(gross - net, 2),
                "currency": currency,
                "status":   STATUS_LABELS.get(inv.get("status", ""), inv.get("status", "—")),
                "ksef":     bool(gov_id and gov_status in ("ok", "demo_ok")),
            })

    except requests.HTTPError as e:
        error = f"Błąd API: {e.response.status_code} — sprawdź domenę i token."
    except Exception as e:
        error = f"Błąd połączenia: {e}"

    months    = sorted({i["month"] for i in invoices if i["month"]}, reverse=True)
    total_net   = sum(i["net"]   for i in invoices)
    total_gross = sum(i["gross"] for i in invoices)
    total_vat   = sum(i["vat"]   for i in invoices)

    return invoices, months, total_net, total_gross, total_vat, error


def _invoices_cache_path(invoice_type):
    return INVOICES_COST_CACHE if invoice_type == "cost" else INVOICES_INCOME_CACHE


def _save_invoices_cache(invoice_type, invoices, months, total_net, total_gross, total_vat, error):
    with open(_invoices_cache_path(invoice_type), "w", encoding="utf-8") as f:
        json.dump({
            "updated":     datetime.utcnow().isoformat(),
            "invoices":    invoices,
            "months":      months,
            "total_net":   total_net,
            "total_gross": total_gross,
            "total_vat":   total_vat,
            "error":       error,
        }, f, ensure_ascii=False, indent=2)


def _load_invoices_cache(invoice_type):
    path = _invoices_cache_path(invoice_type)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_manual_actions():
    """Return dict {tx_id: 'skip' | 'dysk'}."""
    if not os.path.exists(MANUAL_ACTIONS_FILE):
        return {}
    with open(MANUAL_ACTIONS_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_manual_actions(actions):
    with open(MANUAL_ACTIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(actions, f, ensure_ascii=False, indent=2)


@app.route("/")
def home():
    return redirect(url_for("kosztowe"))


@app.route("/kosztowe")
def kosztowe():
    cache     = _load_invoices_cache("cost")
    sel_month = request.args.get("month", "")
    if cache:
        all_inv = cache["invoices"]
        months  = cache["months"]
        updated = cache.get("updated", "")
        error   = cache.get("error")
        if not sel_month and months:
            sel_month = months[0]
        invoices    = [i for i in all_inv if not sel_month or i.get("month") == sel_month]
        total_net   = sum(i["net"]   for i in invoices)
        total_gross = sum(i["gross"] for i in invoices)
        total_vat   = sum(i["vat"]   for i in invoices)
    else:
        invoices, months, total_net, total_gross, total_vat, error, updated = [], [], 0, 0, 0, None, None
    return render_template("invoices.html",
        invoice_type="cost", active="kosztowe",
        invoices=invoices, months=months,
        total_net=total_net, total_gross=total_gross, total_vat=total_vat,
        error=error, cache_updated=updated, sel_month=sel_month)


@app.route("/kosztowe/odswiez", methods=["POST"])
def kosztowe_odswiez():
    result = fetch_and_build("cost")
    _save_invoices_cache("cost", *result)
    return redirect(url_for("kosztowe"))


@app.route("/przychodowe")
def przychodowe():
    cache     = _load_invoices_cache("income")
    sel_month = request.args.get("month", "")
    if cache:
        all_inv = cache["invoices"]
        months  = cache["months"]
        updated = cache.get("updated", "")
        error   = cache.get("error")
        if not sel_month and months:
            sel_month = months[0]
        invoices    = [i for i in all_inv if not sel_month or i.get("month") == sel_month]
        total_net   = sum(i["net"]   for i in invoices)
        total_gross = sum(i["gross"] for i in invoices)
        total_vat   = sum(i["vat"]   for i in invoices)
    else:
        invoices, months, total_net, total_gross, total_vat, error, updated = [], [], 0, 0, 0, None, None
    return render_template("invoices.html",
        invoice_type="income", active="przychodowe",
        invoices=invoices, months=months,
        total_net=total_net, total_gross=total_gross, total_vat=total_vat,
        error=error, cache_updated=updated, sel_month=sel_month)


@app.route("/przychodowe/odswiez", methods=["POST"])
def przychodowe_odswiez():
    result = fetch_and_build("income")
    _save_invoices_cache("income", *result)
    return redirect(url_for("przychodowe"))


IMPORT_DIR    = os.path.join(os.path.dirname(__file__), "importy_alior")
KEYWORDS_FILE = os.path.join(os.path.dirname(__file__), "grey_keywords.json")

DEFAULT_KEYWORDS = [
    "POBRANIE OPŁATY/PROWIZJI",
    "składka",
    "Urząd Skarbowy",
    "wypłata",
]


def load_keywords():
    if not os.path.exists(KEYWORDS_FILE):
        save_keywords(DEFAULT_KEYWORDS)
        return list(DEFAULT_KEYWORDS)
    with open(KEYWORDS_FILE) as f:
        return json.load(f)


def save_keywords(keywords):
    with open(KEYWORDS_FILE, "w") as f:
        json.dump(keywords, f, ensure_ascii=False, indent=2)


def row_class(tx, keywords):
    """Return CSS class for row coloring. Grey overrides red/green."""
    cls = ""
    if tx["typ"] == "cost" and not tx["document"]:
        cls = "row-red"
    elif tx["typ"] == "income" and not tx["document"]:
        cls = "row-green"
    details_lower = tx["details"].lower()
    if any(kw.lower() in details_lower for kw in keywords):
        cls = "row-grey"
    return cls


def fetch_invoices_for_matching():
    """Return invoices for transaction matching, preferring on-disk cache."""
    all_invoices = []
    for itype in ("income", "cost"):
        cache = _load_invoices_cache(itype)
        if cache:
            for inv in cache.get("invoices", []):
                all_invoices.append({
                    "number":    (inv.get("number") or "").strip(),
                    "gross":     float(inv.get("gross") or 0),
                    "currency":  (inv.get("currency") or "PLN").strip(),
                    "full_date": (inv.get("full_date") or "").strip(),
                    "ksef":      inv.get("ksef", False),
                })
    if all_invoices:
        return all_invoices

    # No cache yet — fall back to live API
    domain, token = ENV_DOMAIN, ENV_TOKEN
    if not domain or not token:
        return []
    for income_param in (None, "no"):
        try:
            params = {"api_token": token, "per_page": 100, "page": 1}
            if income_param == "no":
                params["income"] = "no"
            resp = requests.get(
                f"https://{domain}.fakturownia.pl/invoices.json",
                params=params, timeout=10
            )
            resp.raise_for_status()
            for inv in resp.json():
                gov_id     = inv.get("gov_id") or ""
                gov_status = inv.get("gov_status") or ""
                all_invoices.append({
                    "number":    (inv.get("number") or "").strip(),
                    "gross":     float(inv.get("price_gross", 0) or 0),
                    "currency":  (inv.get("currency") or "PLN").strip(),
                    "full_date": (inv.get("issue_date") or "").strip(),
                    "ksef":      bool(gov_id and gov_status in ("ok", "demo_ok")),
                })
        except Exception:
            pass
    return all_invoices


def find_matching_invoice(tx, invoices):
    tx_amount    = abs(tx["amount"])
    tx_amount_op = abs(tx.get("amount_op", tx["amount"]))
    tx_cur_op    = tx.get("currency_op", "") or tx.get("currency", "PLN")
    details      = tx["details"].lower()
    try:
        tx_date = date_type.fromisoformat(tx["full_date"])
    except (ValueError, TypeError):
        tx_date = None

    # Priority 1: invoice number found literally in transaction details
    for inv in invoices:
        num = inv["number"]
        if num and num.lower() in details:
            return inv

    # Priority 2: amount match (±0.02) + date within ±14 days
    # For foreign-currency invoices compare against the original operation amount
    if tx_date:
        for inv in invoices:
            inv_cur = inv.get("currency", "PLN") or "PLN"
            if inv_cur != "PLN" and inv_cur == tx_cur_op:
                cmp = tx_amount_op   # e.g. USD invoice vs USD operation amount
            else:
                cmp = tx_amount      # PLN comparison
            if abs(inv["gross"] - cmp) <= 0.02:
                try:
                    inv_date = date_type.fromisoformat(inv["full_date"])
                    if abs((tx_date - inv_date).days) <= 14:
                        return inv
                except (ValueError, TypeError):
                    pass
    return None


def parse_amount(s):
    try:
        return float(s.replace(" ", "").replace(",", "."))
    except (ValueError, AttributeError):
        return 0.0


def alior_date_to_iso(d):
    """DD-MM-YYYY → YYYY-MM-DD"""
    if d and len(d) == 10 and d[2] == "-":
        return f"{d[6:10]}-{d[3:5]}-{d[0:2]}"
    return d


def load_transactions(invoices=None):
    seen = set()
    transactions = []

    for path in glob.glob(os.path.join(IMPORT_DIR, "*.csv")):
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            reader = csv.reader(f, delimiter=";")
            rows = list(reader)

        # Row 0 = criteria info, Row 1 = headers, Row 2+ = data
        if len(rows) < 3:
            continue

        for row in rows[2:]:
            if len(row) < 9:
                continue

            (date_tr, date_book, sender, recipient,
             details, amount_op, currency_op,
             amount_acc, currency_acc, *rest) = row + [""] * 11

            account_sender   = rest[0] if len(rest) > 0 else ""
            account_recipient = rest[1] if len(rest) > 1 else ""

            # Deduplicate by hashing all key fields
            key = hashlib.md5(
                "|".join([date_tr, sender, recipient, details,
                          amount_op, currency_op]).encode()
            ).hexdigest()
            if key in seen:
                continue
            seen.add(key)

            iso_date = alior_date_to_iso(date_tr.strip())
            display_date = f"{iso_date[8:10]}/{iso_date[5:7]}/{iso_date[0:4]}" if len(iso_date) == 10 else date_tr

            amount_acc_f = parse_amount(amount_acc)
            amount_op_f  = parse_amount(amount_op)
            # Show foreign currency only when different from account currency
            foreign = f"{amount_op_f:+.2f} {currency_op.strip()}" if currency_op.strip() != currency_acc.strip() else ""

            typ = "income" if amount_acc_f >= 0 else "cost"
            # Kontrahent: for incoming show sender, for outgoing show recipient
            _sender    = sender.strip()
            _recipient = recipient.strip()
            if typ == "income":
                kontrahent = _sender or _recipient
            else:
                kontrahent = _recipient or _sender

            transactions.append({
                "tx_id":       key,
                "date":        display_date,
                "full_date":   iso_date,
                "month":       iso_date[:7],
                "kontrahent":  kontrahent,
                "details":     details.strip(),
                "typ":         typ,
                "amount":      amount_acc_f,
                "currency":    currency_acc.strip(),
                "amount_op":   amount_op_f,
                "currency_op": currency_op.strip(),
                "foreign":     foreign,
                "document":    "",   # filled in after matching
            })

    transactions.sort(key=lambda t: t["full_date"], reverse=True)

    # Match each transaction against invoices
    if invoices:
        for tx in transactions:
            matched = find_matching_invoice(tx, invoices)
            if matched and matched["ksef"]:
                tx["document"] = "KSeF"

    months = sorted({t["month"] for t in transactions if t["month"]}, reverse=True)
    return transactions, months


def _build_transactions():
    """Full rebuild: read CSVs + match Fakturownia + match OCR. Returns (transactions, months)."""
    invoices = fetch_invoices_for_matching()
    transactions, months = load_transactions(invoices)

    ocr_entries = load_ocr_data()
    if ocr_entries:
        ocr_for_match = [
            {
                "number":    (e.get("number") or "").strip(),
                "gross":     float(e.get("gross") or 0),
                "full_date": (e.get("date") or "").strip(),
                "currency":  (e.get("currency") or "PLN").strip(),
            }
            for e in ocr_entries
        ]
        for tx in transactions:
            if not tx["document"] and find_matching_invoice(tx, ocr_for_match):
                tx["document"] = "dysk"

    return transactions, months


def _save_transactions_cache(transactions, months):
    with open(TRANSACTIONS_CACHE, "w", encoding="utf-8") as f:
        json.dump({
            "updated":      datetime.utcnow().isoformat(),
            "transactions": transactions,
            "months":       months,
        }, f, ensure_ascii=False, indent=2)


def _load_transactions_cache():
    if not os.path.exists(TRANSACTIONS_CACHE):
        return None
    with open(TRANSACTIONS_CACHE, encoding="utf-8") as f:
        return json.load(f)


@app.route("/transakcje")
def transakcje():
    cache = _load_transactions_cache()

    sel_month = request.args.get("month", "")
    sel_type  = request.args.get("type",  "")
    sel_doc   = request.args.get("doc",   "")

    if cache:
        all_txs = cache["transactions"]
        months  = cache["months"]
        updated = cache.get("updated", "")

        if not sel_month and months:
            sel_month = months[0]

        # Compute derived fields on all transactions before filtering
        keywords       = load_keywords()
        manual_actions = load_manual_actions()
        for tx in all_txs:
            tx["row_class"]     = row_class(tx, keywords)
            manual              = manual_actions.get(tx.get("tx_id", ""), "")
            tx["manual_action"] = manual
            tx["skipped"]       = (tx["row_class"] == "row-grey") or (manual == "skip")

        # Server-side filtering
        txs = all_txs
        if sel_month:
            txs = [t for t in txs if t.get("month") == sel_month]
        if sel_type:
            txs = [t for t in txs if t.get("typ") == sel_type]
        if sel_doc == "1":
            txs = [t for t in txs if t.get("document") or t.get("skipped")]
        elif sel_doc == "0":
            txs = [t for t in txs if not t.get("document") and not t.get("skipped")]

        total_amount = sum(t.get("amount", 0) for t in txs)
    else:
        txs, months, updated = [], [], None
        total_amount = 0

    return render_template("transactions.html",
        active="transakcje",
        transactions=txs,
        months=months,
        cache_updated=updated,
        sel_month=sel_month,
        sel_type=sel_type,
        sel_doc=sel_doc,
        total_amount=total_amount)


@app.route("/transakcje/odswiez", methods=["POST"])
def transakcje_odswiez():
    transactions, months = _build_transactions()
    _save_transactions_cache(transactions, months)
    return redirect(url_for("transakcje"))


@app.route("/transakcje/akcja", methods=["POST"])
def toggle_akcja():
    data   = request.get_json(force=True) or {}
    tx_id  = data.get("tx_id", "")
    action = data.get("action", "")
    if not tx_id or action not in ("skip", "dysk"):
        return jsonify({"error": "invalid params"}), 400
    actions  = load_manual_actions()
    previous = actions.get(tx_id, "")
    if previous == action:
        del actions[tx_id]
        result = "removed"
    else:
        actions[tx_id] = action
        result = "added"
    save_manual_actions(actions)
    return jsonify({"result": result, "action": action, "previous": previous, "tx_id": tx_id})


@app.route("/ustawienia", methods=["GET", "POST"])
def ustawienia():
    keywords = load_keywords()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            kw = request.form.get("keyword", "").strip()
            if kw and kw not in keywords:
                keywords.append(kw)
                save_keywords(keywords)
        elif action == "delete":
            kw = request.form.get("keyword", "")
            keywords = [k for k in keywords if k != kw]
            save_keywords(keywords)
        return redirect(url_for("ustawienia"))
    return render_template("settings.html", active="ustawienia", keywords=keywords)


# ── Google Drive ──────────────────────────────────────────────────────────────

def _gdrive_list(parent_id, api_key, folders_only=False):
    """Return list of items inside a Drive folder (one page, up to 1000)."""
    q = f"'{parent_id}' in parents and trashed=false"
    if folders_only:
        q += " and mimeType='application/vnd.google-apps.folder'"
    params = {
        "q": q,
        "fields": "files(id,name,mimeType,createdTime,modifiedTime,size,webViewLink)",
        "pageSize": 1000,
        "key": api_key,
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
    }
    resp = requests.get(GDRIVE_API, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json().get("files", [])


def _gdrive_find_folder(parent_id, name, api_key):
    """Find a subfolder by exact name; return its id or None."""
    folders = _gdrive_list(parent_id, api_key, folders_only=True)
    for f in folders:
        if f["name"] == name:
            return f["id"]
    return None


def _gdrive_files_recursive(folder_id, api_key, path=""):
    """Recursively collect all non-folder files with their relative path."""
    items = _gdrive_list(folder_id, api_key)
    files = []
    for item in items:
        item_path = f"{path}/{item['name']}" if path else item["name"]
        if item["mimeType"] == "application/vnd.google-apps.folder":
            files.extend(_gdrive_files_recursive(item["id"], api_key, item_path))
        else:
            files.append({
                "id":        item["id"],
                "name":      item["name"],
                "path":      item_path,
                "created":   item.get("createdTime", "")[:10],
                "modified":  item.get("modifiedTime", "")[:10],
                "size":      item.get("size", ""),
                "link":      item.get("webViewLink", ""),
                "mime":      item.get("mimeType", ""),
            })
    return files


def load_drive_files(year, month):
    """Navigate root → year → 'MM/YYYY' → list all files recursively."""
    api_key = GDRIVE_API_KEY
    root    = GDRIVE_ROOT_FOLDER
    if not api_key or not root:
        return [], "Brak klucza API lub ID folderu w pliku .env"

    try:
        # 1. Year folder
        year_id = _gdrive_find_folder(root, str(year), api_key)
        if not year_id:
            return [], f"Nie znaleziono folderu roku '{year}'"

        # 2. Month folder — try "MM/YYYY" then "MM.YYYY" then "MM-YYYY" then "MM"
        month_str = f"{month:02d}"
        candidates = [
            f"{month_str}/{year}",
            f"{month_str}.{year}",
            f"{month_str}-{year}",
            month_str,
        ]
        month_id = None
        for name in candidates:
            month_id = _gdrive_find_folder(year_id, name, api_key)
            if month_id:
                break
        if not month_id:
            return [], f"Nie znaleziono folderu miesiąca w '{year}' (próbowano: {', '.join(candidates)})"

        # 3. Recursive file list
        files = _gdrive_files_recursive(month_id, api_key)
        return files, None

    except requests.HTTPError as e:
        code = e.response.status_code
        if code == 403:
            return [], "Brak dostępu (403) — sprawdź czy folder jest publiczny i czy klucz API ma uprawnienia Drive."
        return [], f"Błąd API Google Drive: {code}"
    except Exception as e:
        return [], f"Błąd: {e}"


def drive_month_options():
    today = date_type.today()
    opts = []
    y, m = today.year, today.month
    for _ in range(24):
        m -= 1
        if m == 0:
            m, y = 12, y - 1
        opts.append((y, m))
    return opts


@app.route("/faktury-na-dysku")
def faktury_na_dysku():
    opts    = drive_month_options()
    def_y, def_m = opts[0]
    sel_year  = int(request.args.get("year",  def_y))
    sel_month = int(request.args.get("month", def_m))

    files, error = load_drive_files(sel_year, sel_month)
    processed_ids = {e["file_id"] for e in load_ocr_data()}
    return render_template("drive.html",
        active="faktury-na-dysku",
        files=files,
        error=error,
        month_options=opts,
        sel_year=sel_year,
        sel_month=sel_month,
        api_configured=bool(GDRIVE_API_KEY),
        processed_ids=processed_ids,
    )


# ── OCR / LLM invoice extraction ─────────────────────────────────────────────

def download_drive_file(file_id, api_key):
    """Download a Drive file as raw bytes.

    API-key download works only for publicly accessible files.
    Falls back to the direct Drive download URL when the API returns 403.
    """
    # Attempt 1: Drive API (works when file is publicly accessible)
    api_resp = requests.get(
        f"https://www.googleapis.com/drive/v3/files/{file_id}",
        params={"alt": "media", "key": api_key, "supportsAllDrives": "true"},
        timeout=30,
    )
    if api_resp.status_code == 200:
        return api_resp.content

    # Attempt 2: public export URL (works when shared "Anyone with the link")
    dl_resp = requests.get(
        "https://drive.google.com/uc",
        params={"export": "download", "id": file_id, "confirm": "t"},
        timeout=30,
        allow_redirects=True,
    )
    if dl_resp.status_code == 200:
        ct = dl_resp.headers.get("content-type", "")
        if "text/html" in ct:
            raise Exception(
                f"Nie można pobrać pliku (HTTP {api_resp.status_code}). "
                "Sprawdź czy plik jest udostępniony publicznie z możliwością pobierania."
            )
        return dl_resp.content

    api_resp.raise_for_status()  # raise original API error


def extract_invoice_data(pdf_bytes, gemini_key):
    """Send PDF bytes to Gemini and return extracted invoice fields as dict."""
    genai.configure(api_key=gemini_key)
    model = genai.GenerativeModel("gemini-2.5-flash")
    prompt = (
        "Wyciągnij dane z tej faktury. Zwróć WYŁĄCZNIE obiekt JSON (bez markdown, bez komentarzy) "
        "z polami: number (numer faktury), date (data wystawienia YYYY-MM-DD), "
        "seller (nazwa sprzedawcy), buyer (nazwa nabywcy), "
        "net (kwota netto, liczba), vat_amount (kwota VAT, liczba), "
        "gross (kwota brutto, liczba), currency (waluta np. PLN), "
        "vat_rate (stawka VAT np. '23%' lub 'zw'). "
        "Jeśli pole jest niedostępne wpisz null."
    )
    response = model.generate_content([
        {"mime_type": "application/pdf", "data": pdf_bytes},
        prompt,
    ])
    raw = response.text.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


def load_ocr_data():
    if not os.path.exists(OCR_DATA_FILE):
        return []
    with open(OCR_DATA_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_ocr_entry(file_id, filename, data):
    """Append entry; return False if file_id already recorded."""
    entries = load_ocr_data()
    if any(e.get("file_id") == file_id for e in entries):
        return False
    entries.append({
        "file_id":   file_id,
        "filename":  filename,
        "extracted": datetime.utcnow().isoformat(),
        **data,
    })
    with open(OCR_DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
    return True


@app.route("/przetworz-faktury", methods=["POST"])
def przetworz_faktury():
    if not GEMINI_API_KEY:
        return jsonify({"error": "Brak klucza GEMINI_API_KEY w .env"}), 400
    payload = request.get_json(force=True) or {}
    files = payload.get("files", [])
    results = []
    for f in files:
        file_id  = f.get("id", "")
        filename = f.get("name", "")
        try:
            if any(e.get("file_id") == file_id for e in load_ocr_data()):
                results.append({"id": file_id, "name": filename, "status": "skip", "msg": "Już przetworzone"})
                continue
            pdf_bytes = download_drive_file(file_id, GDRIVE_API_KEY)
            data = extract_invoice_data(pdf_bytes, GEMINI_API_KEY)
            save_ocr_entry(file_id, filename, data)
            results.append({"id": file_id, "name": filename, "status": "ok"})
        except Exception as e:
            results.append({"id": file_id, "name": filename, "status": "error", "msg": str(e)})
    return jsonify({"results": results})


@app.route("/faktury-google")
def faktury_google():
    entries    = load_ocr_data()
    all_months = sorted({e.get("date", "")[:7] for e in entries if e.get("date", "")}, reverse=True)
    sel_month  = request.args.get("month", "")
    if not sel_month and all_months:
        sel_month = all_months[0]
    filtered = [e for e in entries if not sel_month or (e.get("date") or "")[:7] == sel_month]
    total_net   = sum(float(e.get("net")        or 0) for e in filtered)
    total_vat   = sum(float(e.get("vat_amount") or 0) for e in filtered)
    total_gross = sum(float(e.get("gross")      or 0) for e in filtered)
    return render_template("invoices_google.html",
        active="faktury-google",
        entries=filtered,
        months=all_months,
        sel_month=sel_month,
        total_net=total_net,
        total_vat=total_vat,
        total_gross=total_gross)


@app.route("/faktury-google/usun", methods=["POST"])
def faktury_google_usun():
    file_id = request.form.get("file_id", "")
    if file_id:
        entries = [e for e in load_ocr_data() if e.get("file_id") != file_id]
        with open(OCR_DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
    return redirect(url_for("faktury_google"))


if __name__ == "__main__":
    app.run(debug=True)
