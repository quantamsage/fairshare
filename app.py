"""FairShare: split group bills fairly, including uneven shares, and track who has paid up."""
import csv
import io
import os
from decimal import Decimal, InvalidOperation

import psycopg2
from flask import Flask, Response, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import LoginManager, UserMixin, current_user, login_required, login_user, logout_user
from werkzeug.security import check_password_hash, generate_password_hash

from db import get_db, init_db
from splitting import Transfer, balances, format_cents, is_settled, suggest_transfers, to_cents

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

CURRENCIES = {
    "KSH": "Kenyan Shilling", "USD": "US Dollar", "EUR": "Euro",
    "GBP": "British Pound", "UGX": "Ugandan Shilling", "TZS": "Tanzanian Shilling",
}
MAX_NAME = 60
MAX_AMOUNT_CENTS = 10_000_000_000 - 1  # fits NUMERIC(12, 2)


class User(UserMixin):
    def __init__(self, id, username):
        self.id = id
        self.username = username


# ── Data access ───────────────────────────────────────────────────────────────

def owned_bill(cur, bill_id):
    """The bill if it belongs to the logged-in user, otherwise 404 (never reveal other users' bills)."""
    cur.execute("SELECT id, name, currency, created FROM bills WHERE id=%s AND user_id=%s",
                (bill_id, current_user.id))
    bill = cur.fetchone()
    if bill is None:
        abort(404)
    return bill


def load_bill(cur, bill_id):
    """Everything the bill page, CSV export and JSON API need, computed in one place."""
    bill = owned_bill(cur, bill_id)
    cur.execute("SELECT id, name, weight FROM people WHERE bill_id=%s ORDER BY id", (bill_id,))
    people = cur.fetchall()
    cur.execute(
        "SELECT p.id, p.person_id, p.amount, p.description, pe.name AS payer FROM payments p "
        "JOIN people pe ON pe.id = p.person_id WHERE p.bill_id=%s ORDER BY p.id", (bill_id,))
    payments = cur.fetchall()
    cur.execute(
        "SELECT s.id, s.from_person, s.to_person, s.amount, s.paid_at, f.name AS from_name, t.name AS to_name "
        "FROM settlements s JOIN people f ON f.id = s.from_person JOIN people t ON t.id = s.to_person "
        "WHERE s.bill_id=%s ORDER BY s.id", (bill_id,))
    settled = cur.fetchall()

    names = {p["id"]: p["name"] for p in people}
    paid = {p["id"]: 0 for p in people}
    for pay in payments:
        paid[pay["person_id"]] += to_cents(pay["amount"])
    weights = {p["id"]: Decimal(p["weight"]) for p in people}
    total = sum(paid.values())
    try:
        shares, bal = balances(paid, weights, [
            Transfer(s["from_person"], s["to_person"], to_cents(s["amount"])) for s in settled])
        weight_error = None
    except ValueError as e:
        shares, bal, weight_error = {p: 0 for p in paid}, {p: 0 for p in paid}, str(e)
    transfers = [] if weight_error else suggest_transfers(bal)

    return {
        "bill": bill,
        "people": [{
            "id": p["id"], "name": p["name"], "weight": Decimal(p["weight"]).normalize(),
            "paid": paid[p["id"]], "share": shares[p["id"]], "balance": bal[p["id"]],
            "has_activity": paid[p["id"]] > 0 or any(p["id"] in (s["from_person"], s["to_person"]) for s in settled),
        } for p in people],
        "payments": [{**pay, "cents": to_cents(pay["amount"])} for pay in payments],
        "settled": [{**s, "cents": to_cents(s["amount"])} for s in settled],
        "transfers": [{"from_id": t.from_id, "to_id": t.to_id, "from": names[t.from_id],
                       "to": names[t.to_id], "amount": t.amount} for t in transfers],
        "total": total,
        "is_settled": not weight_error and is_settled(total, bal),
        "weight_error": weight_error,
    }


def get_or_create_person(cur, bill_id, name):
    cur.execute("SELECT id FROM people WHERE bill_id=%s AND lower(name)=lower(%s)", (bill_id, name))
    row = cur.fetchone()
    if row:
        return row["id"]
    cur.execute("INSERT INTO people (bill_id, name) VALUES (%s, %s) RETURNING id", (bill_id, name))
    return cur.fetchone()["id"]


# ── Form parsing ──────────────────────────────────────────────────────────────

def clean_name(raw):
    name = " ".join((raw or "").split())
    if not name:
        raise ValueError("Name is required.")
    if len(name) > MAX_NAME:
        raise ValueError(f"Names can be at most {MAX_NAME} characters.")
    return name


def clean_amount(raw):
    try:
        cents = to_cents(raw or "")
    except ValueError:
        raise ValueError("Amount must be a number, like 1500 or 249.50.")
    if cents <= 0:
        raise ValueError("Amount must be more than 0.")
    if cents > MAX_AMOUNT_CENTS:
        raise ValueError("That amount is too large.")
    return Decimal(cents) / 100


def clean_weight(raw):
    try:
        weight = Decimal((raw or "").strip())
    except InvalidOperation:
        raise ValueError("Share must be a number, like 1, 0.5 or 2.")
    if not weight.is_finite() or weight < 0 or weight > 100:
        raise ValueError("Share must be between 0 and 100.")
    if weight != weight.quantize(Decimal("0.01")):
        raise ValueError("Use at most two decimal places for a share.")
    return weight


def as_int(raw):
    try:
        return int(raw)
    except (TypeError, ValueError):
        abort(400)


# ── App ───────────────────────────────────────────────────────────────────────

def create_app(init=True):
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "dev-only-do-not-use-in-production")
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
    app.jinja_env.filters["money"] = format_cents

    login_manager = LoginManager(app)
    login_manager.login_view = "login"
    login_manager.login_message = "Please log in to see your bills."

    if init:
        init_db()

    @login_manager.user_loader
    def load_user(user_id):
        with get_db() as conn, conn.cursor() as cur:
            cur.execute("SELECT id, username FROM users WHERE id=%s", (user_id,))
            row = cur.fetchone()
        return User(row["id"], row["username"]) if row else None

    @app.errorhandler(404)
    def not_found(_):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Not found"}), 404
        flash("That bill doesn't exist, or it isn't yours.")
        return redirect(url_for("index"))

    # ── Auth ──

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            if not username or len(password) < 8:
                flash("Choose a username and a password of at least 8 characters.")
                return redirect(url_for("register"))
            try:
                with get_db() as conn, conn.cursor() as cur:
                    cur.execute("INSERT INTO users (username, password) VALUES (%s, %s)",
                                (username, generate_password_hash(password)))
            except psycopg2.errors.UniqueViolation:
                flash("That username is taken.")
                return redirect(url_for("register"))
            flash("Account created. Please log in.")
            return redirect(url_for("login"))
        return render_template("register.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            with get_db() as conn, conn.cursor() as cur:
                cur.execute("SELECT id, username, password FROM users WHERE username=%s", (username,))
                row = cur.fetchone()
            if row and check_password_hash(row["password"], request.form.get("password", "")):
                login_user(User(row["id"], row["username"]))
                return redirect(url_for("index"))
            flash("Wrong username or password.")
        return render_template("login.html")

    @app.route("/logout", methods=["POST"])
    @login_required
    def logout():
        logout_user()
        return redirect(url_for("login"))

    # ── Bills ──

    @app.route("/")
    @login_required
    def index():
        with get_db() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT b.id, b.name, b.currency, b.created, "
                "  COALESCE((SELECT sum(amount) FROM payments WHERE bill_id=b.id), 0) AS total, "
                "  (SELECT count(*) FROM people WHERE bill_id=b.id) AS people "
                "FROM bills b WHERE b.user_id=%s ORDER BY b.created DESC, b.id DESC",
                (current_user.id,))
            bills = cur.fetchall()
        return render_template("index.html", bills=[{**b, "total": to_cents(b["total"])} for b in bills])

    @app.route("/bill/new", methods=["GET", "POST"])
    @login_required
    def new_bill():
        if request.method == "POST":
            name = " ".join(request.form.get("name", "").split())
            currency = request.form.get("currency", "KSH")
            if not name or len(name) > 100:
                flash("Give the bill a name (up to 100 characters).")
                return redirect(url_for("new_bill"))
            if currency not in CURRENCIES:
                flash("Pick a currency from the list.")
                return redirect(url_for("new_bill"))
            with get_db() as conn, conn.cursor() as cur:
                cur.execute("INSERT INTO bills (user_id, name, currency) VALUES (%s, %s, %s) RETURNING id",
                            (current_user.id, name, currency))
                bill_id = cur.fetchone()["id"]
            return redirect(url_for("bill", bill_id=bill_id))
        return render_template("new_bill.html", currencies=CURRENCIES)

    @app.route("/bill/<int:bill_id>")
    @login_required
    def bill(bill_id):
        with get_db() as conn, conn.cursor() as cur:
            data = load_bill(cur, bill_id)
        # Longest bar in the paid-vs-share chart; the others are drawn relative to it.
        max_bar = max([p["paid"] for p in data["people"]] + [p["share"] for p in data["people"]] + [1])
        return render_template("bill.html", max_bar=max_bar, **data)

    @app.route("/bill/<int:bill_id>/delete", methods=["POST"])
    @login_required
    def delete_bill(bill_id):
        with get_db() as conn, conn.cursor() as cur:
            owned_bill(cur, bill_id)
            # Explicit order so this also works on databases created before ON DELETE CASCADE.
            for table in ("settlements", "payments", "people"):
                cur.execute(f"DELETE FROM {table} WHERE bill_id=%s", (bill_id,))
            cur.execute("DELETE FROM bills WHERE id=%s", (bill_id,))
        flash("Bill deleted.")
        return redirect(url_for("index"))

    # ── People ──

    @app.route("/bill/<int:bill_id>/people", methods=["POST"])
    @login_required
    def add_person(bill_id):
        try:
            name = clean_name(request.form.get("name"))
            weight = clean_weight(request.form.get("weight", "1"))
        except ValueError as e:
            flash(str(e))
            return redirect(url_for("bill", bill_id=bill_id))
        with get_db() as conn, conn.cursor() as cur:
            owned_bill(cur, bill_id)
            cur.execute("SELECT 1 FROM people WHERE bill_id=%s AND lower(name)=lower(%s)", (bill_id, name))
            if cur.fetchone():
                flash(f"{name} is already on this bill.")
            else:
                cur.execute("INSERT INTO people (bill_id, name, weight) VALUES (%s, %s, %s)",
                            (bill_id, name, weight))
                flash(f"Added {name}.")
        return redirect(url_for("bill", bill_id=bill_id))

    @app.route("/bill/<int:bill_id>/people/<int:person_id>", methods=["POST"])
    @login_required
    def update_person(bill_id, person_id):
        try:
            weight = clean_weight(request.form.get("weight"))
        except ValueError as e:
            flash(str(e))
            return redirect(url_for("bill", bill_id=bill_id))
        with get_db() as conn, conn.cursor() as cur:
            owned_bill(cur, bill_id)
            cur.execute("UPDATE people SET weight=%s WHERE id=%s AND bill_id=%s RETURNING name",
                        (weight, person_id, bill_id))
            row = cur.fetchone()
            if row is None:
                abort(404)
            # Shares can't all be zero while money has been spent.
            cur.execute("SELECT COALESCE(sum(weight), 0) AS w FROM people WHERE bill_id=%s", (bill_id,))
            if cur.fetchone()["w"] == 0:
                conn.rollback()
                flash("At least one person needs a share above 0.")
                return redirect(url_for("bill", bill_id=bill_id))
        flash(f"Updated {row['name']}'s share.")
        return redirect(url_for("bill", bill_id=bill_id))

    @app.route("/bill/<int:bill_id>/people/<int:person_id>/delete", methods=["POST"])
    @login_required
    def delete_person(bill_id, person_id):
        with get_db() as conn, conn.cursor() as cur:
            owned_bill(cur, bill_id)
            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM payments WHERE person_id=%(p)s) "
                "    OR EXISTS (SELECT 1 FROM settlements WHERE from_person=%(p)s OR to_person=%(p)s) AS busy",
                {"p": person_id})
            if cur.fetchone()["busy"]:
                flash("Remove this person's payments and settled transfers first.")
            else:
                cur.execute("DELETE FROM people WHERE id=%s AND bill_id=%s", (person_id, bill_id))
                flash("Person removed.")
        return redirect(url_for("bill", bill_id=bill_id))

    # ── Payments ──

    @app.route("/bill/<int:bill_id>/add", methods=["POST"])
    @login_required
    def add_payment(bill_id):
        try:
            name = clean_name(request.form.get("name"))
            amount = clean_amount(request.form.get("amount"))
        except ValueError as e:
            flash(str(e))
            return redirect(url_for("bill", bill_id=bill_id))
        description = " ".join(request.form.get("description", "").split())[:120]
        with get_db() as conn, conn.cursor() as cur:
            owned_bill(cur, bill_id)
            person_id = get_or_create_person(cur, bill_id, name)
            cur.execute("INSERT INTO payments (bill_id, person_id, amount, description) VALUES (%s, %s, %s, %s)",
                        (bill_id, person_id, amount, description))
        flash(f"Added a payment by {name}.")
        return redirect(url_for("bill", bill_id=bill_id))

    @app.route("/bill/<int:bill_id>/edit/<int:payment_id>", methods=["GET", "POST"])
    @login_required
    def edit_payment(bill_id, payment_id):
        with get_db() as conn, conn.cursor() as cur:
            bill_row = owned_bill(cur, bill_id)
            cur.execute("SELECT p.id, p.amount, p.description, pe.name FROM payments p "
                        "JOIN people pe ON pe.id = p.person_id WHERE p.id=%s AND p.bill_id=%s",
                        (payment_id, bill_id))
            payment = cur.fetchone()
            if payment is None:
                abort(404)
            if request.method == "POST":
                try:
                    name = clean_name(request.form.get("name"))
                    amount = clean_amount(request.form.get("amount"))
                except ValueError as e:
                    flash(str(e))
                    return redirect(url_for("edit_payment", bill_id=bill_id, payment_id=payment_id))
                description = " ".join(request.form.get("description", "").split())[:120]
                person_id = get_or_create_person(cur, bill_id, name)
                cur.execute("UPDATE payments SET person_id=%s, amount=%s, description=%s WHERE id=%s",
                            (person_id, amount, description, payment_id))
                flash("Payment updated.")
                return redirect(url_for("bill", bill_id=bill_id))
        return render_template("edit_payment.html", bill=bill_row,
                               payment={**payment, "amount": f"{payment['amount']:.2f}"})

    @app.route("/bill/<int:bill_id>/delete/<int:payment_id>", methods=["POST"])
    @login_required
    def delete_payment(bill_id, payment_id):
        with get_db() as conn, conn.cursor() as cur:
            owned_bill(cur, bill_id)
            cur.execute("DELETE FROM payments WHERE id=%s AND bill_id=%s", (payment_id, bill_id))
        flash("Payment removed.")
        return redirect(url_for("bill", bill_id=bill_id))

    # ── Settle up ──

    @app.route("/bill/<int:bill_id>/settle", methods=["POST"])
    @login_required
    def mark_paid(bill_id):
        """Record one suggested transfer as paid. The server re-checks it against the
        current suggestions, so a stale page can't record a wrong amount."""
        wanted = (as_int(request.form.get("from_id")), as_int(request.form.get("to_id")),
                  as_int(request.form.get("amount")))
        with get_db() as conn, conn.cursor() as cur:
            data = load_bill(cur, bill_id)
            if not any((t["from_id"], t["to_id"], t["amount"]) == wanted for t in data["transfers"]):
                flash("The amounts changed since you loaded the page. Check the updated list and try again.")
                return redirect(url_for("bill", bill_id=bill_id))
            cur.execute("INSERT INTO settlements (bill_id, from_person, to_person, amount) VALUES (%s, %s, %s, %s)",
                        (bill_id, wanted[0], wanted[1], Decimal(wanted[2]) / 100))
        flash("Marked as paid.")
        return redirect(url_for("bill", bill_id=bill_id))

    @app.route("/bill/<int:bill_id>/settle/<int:settlement_id>/undo", methods=["POST"])
    @login_required
    def undo_paid(bill_id, settlement_id):
        with get_db() as conn, conn.cursor() as cur:
            owned_bill(cur, bill_id)
            cur.execute("DELETE FROM settlements WHERE id=%s AND bill_id=%s", (settlement_id, bill_id))
        flash("Transfer marked as not paid.")
        return redirect(url_for("bill", bill_id=bill_id))

    # ── Export and API ──

    @app.route("/bill/<int:bill_id>/export")
    @login_required
    def export_csv(bill_id):
        with get_db() as conn, conn.cursor() as cur:
            data = load_bill(cur, bill_id)
        b, cur_code = data["bill"], data["bill"]["currency"]
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["Person", "Share weight", f"Paid ({cur_code})", f"Fair share ({cur_code})", f"Balance ({cur_code})"])
        for p in data["people"]:
            w.writerow([safe_cell(p["name"]), p["weight"], format_cents(p["paid"]).replace(",", ""),
                        format_cents(p["share"]).replace(",", ""), format_cents(p["balance"]).replace(",", "")])
        w.writerow([])
        w.writerow(["Payments"])
        for pay in data["payments"]:
            w.writerow([safe_cell(pay["payer"]), safe_cell(pay["description"]), format_cents(pay["cents"]).replace(",", "")])
        w.writerow([])
        w.writerow(["Still to pay"])
        for t in data["transfers"]:
            w.writerow([f"{safe_cell(t['from'])} pays {safe_cell(t['to'])}", format_cents(t["amount"]).replace(",", "")])
        w.writerow([])
        w.writerow(["Already paid"])
        for s in data["settled"]:
            w.writerow([f"{safe_cell(s['from_name'])} paid {safe_cell(s['to_name'])}",
                        format_cents(s["cents"]).replace(",", ""), s["paid_at"].strftime("%Y-%m-%d")])
        filename = "".join(c if c.isalnum() else "_" for c in b["name"]) or "bill"
        return Response(out.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{filename}.csv"'})

    @app.route("/api/bill/<int:bill_id>")
    @login_required
    def api_bill(bill_id):
        with get_db() as conn, conn.cursor() as cur:
            data = load_bill(cur, bill_id)
        money = lambda c: float(Decimal(c) / 100)  # noqa: E731
        return jsonify({
            "bill": {"id": data["bill"]["id"], "name": data["bill"]["name"], "currency": data["bill"]["currency"]},
            "total": money(data["total"]),
            "settled": data["is_settled"],
            "people": [{"id": p["id"], "name": p["name"], "weight": float(p["weight"]), "paid": money(p["paid"]),
                        "share": money(p["share"]), "balance": money(p["balance"])} for p in data["people"]],
            "payments": [{"id": p["id"], "payer": p["payer"], "amount": money(p["cents"]),
                          "description": p["description"]} for p in data["payments"]],
            "to_pay": [{"from": t["from"], "to": t["to"], "amount": money(t["amount"])} for t in data["transfers"]],
            "paid": [{"from": s["from_name"], "to": s["to_name"], "amount": money(s["cents"]),
                      "paid_at": s["paid_at"].isoformat()} for s in data["settled"]],
        })

    return app


def safe_cell(text):
    """Stop spreadsheet apps treating a name like '=HYPERLINK(...)' as a formula."""
    text = str(text)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


if __name__ == "__main__":
    create_app().run(debug=os.environ.get("FLASK_DEBUG") == "1")
