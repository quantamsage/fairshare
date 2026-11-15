from tests.conftest import register_and_login


def api(client, bill_id):
    resp = client.get(f"/api/bill/{bill_id}")
    assert resp.status_code == 200
    return resp.get_json()


def person_id(data, name):
    return next(p["id"] for p in data["people"] if p["name"] == name)


def test_bills_list_page_renders(user, bill_id):
    page = user.get("/").get_data(as_text=True)
    assert "Naivasha weekend" in page and "KSH 0.00" in page


def test_same_name_twice_is_one_person(user, bill_id):
    user.post(f"/bill/{bill_id}/add", data={"name": "Amina", "amount": "3000"})
    user.post(f"/bill/{bill_id}/add", data={"name": " amina ", "amount": "1500"})
    user.post(f"/bill/{bill_id}/people", data={"name": "Brian", "weight": "1"})
    data = api(user, bill_id)
    assert [p["name"] for p in data["people"]] == ["Amina", "Brian"]
    assert data["people"][0]["paid"] == 4500
    assert data["to_pay"] == [{"from": "Brian", "to": "Amina", "amount": 2250.0}]


def test_uneven_shares(user, bill_id):
    user.post(f"/bill/{bill_id}/add", data={"name": "Amina", "amount": "7000", "description": "Dinner"})
    user.post(f"/bill/{bill_id}/people", data={"name": "Brian", "weight": "0.5"})
    user.post(f"/bill/{bill_id}/people", data={"name": "Chebet", "weight": "2"})
    data = api(user, bill_id)
    shares = {p["name"]: p["share"] for p in data["people"]}
    assert shares == {"Amina": 2000.0, "Brian": 1000.0, "Chebet": 4000.0}

    amina = person_id(data, "Amina")
    user.post(f"/bill/{bill_id}/people/{amina}", data={"weight": "0"})
    shares = {p["name"]: p["share"] for p in api(user, bill_id)["people"]}
    assert shares == {"Amina": 0.0, "Brian": 1400.0, "Chebet": 5600.0}


def test_all_shares_zero_is_refused(user, bill_id):
    user.post(f"/bill/{bill_id}/add", data={"name": "Amina", "amount": "100"})
    amina = person_id(api(user, bill_id), "Amina")
    resp = user.post(f"/bill/{bill_id}/people/{amina}", data={"weight": "0"}, follow_redirects=True)
    assert "needs a share above 0" in resp.get_data(as_text=True)
    assert api(user, bill_id)["people"][0]["weight"] == 1.0


def test_settle_up_flow(user, bill_id):
    user.post(f"/bill/{bill_id}/add", data={"name": "Amina", "amount": "9000"})
    user.post(f"/bill/{bill_id}/people", data={"name": "Brian"})
    user.post(f"/bill/{bill_id}/people", data={"name": "Chebet"})
    data = api(user, bill_id)
    a, b, c = (person_id(data, n) for n in ("Amina", "Brian", "Chebet"))
    assert not data["settled"] and len(data["to_pay"]) == 2

    # A stale or tampered amount is refused.
    resp = user.post(f"/bill/{bill_id}/settle", data={"from_id": b, "to_id": a, "amount": 1}, follow_redirects=True)
    assert "amounts changed" in resp.get_data(as_text=True)

    user.post(f"/bill/{bill_id}/settle", data={"from_id": b, "to_id": a, "amount": 300000})
    data = api(user, bill_id)
    assert data["to_pay"] == [{"from": "Chebet", "to": "Amina", "amount": 3000.0}]
    assert data["paid"][0]["from"] == "Brian"

    user.post(f"/bill/{bill_id}/settle", data={"from_id": c, "to_id": a, "amount": 300000})
    data = api(user, bill_id)
    assert data["settled"] and data["to_pay"] == []
    assert "Settled" in user.get(f"/bill/{bill_id}").get_data(as_text=True)

    # Undo puts the transfer back on the list.
    user.post(f"/bill/{bill_id}/settle/1/undo")
    assert not api(user, bill_id)["settled"]


def test_people_with_activity_cannot_be_removed(user, bill_id):
    user.post(f"/bill/{bill_id}/add", data={"name": "Amina", "amount": "100"})
    user.post(f"/bill/{bill_id}/people", data={"name": "Brian"})
    data = api(user, bill_id)
    user.post(f"/bill/{bill_id}/people/{person_id(data, 'Amina')}/delete")
    user.post(f"/bill/{bill_id}/people/{person_id(data, 'Brian')}/delete")
    assert [p["name"] for p in api(user, bill_id)["people"]] == ["Amina"]


def test_edit_payment_can_change_payer(user, bill_id):
    user.post(f"/bill/{bill_id}/add", data={"name": "Amina", "amount": "100"})
    pay_id = api(user, bill_id)["payments"][0]["id"]
    user.post(f"/bill/{bill_id}/edit/{pay_id}", data={"name": "Brian", "amount": "250.5", "description": "Fuel"})
    pay = api(user, bill_id)["payments"][0]
    assert (pay["payer"], pay["amount"], pay["description"]) == ("Brian", 250.5, "Fuel")


def test_bad_input_is_rejected(user, bill_id):
    for amount in ["0", "-5", "abc", "99999999999"]:
        resp = user.post(f"/bill/{bill_id}/add", data={"name": "Amina", "amount": amount}, follow_redirects=True)
        assert "Amount" in resp.get_data(as_text=True) or "too large" in resp.get_data(as_text=True)
    assert api(user, bill_id)["payments"] == []
    user.post(f"/bill/{bill_id}/people", data={"name": "Brian", "weight": "-1"})
    assert api(user, bill_id)["people"] == []


def test_csv_export_neutralises_formulas(user, bill_id):
    user.post(f"/bill/{bill_id}/add", data={"name": "=HYPERLINK(1)", "amount": "100"})
    body = user.get(f"/bill/{bill_id}/export").get_data(as_text=True)
    assert "'=HYPERLINK(1)" in body and "Fair share (KSH)" in body


def test_users_cannot_see_each_others_bills(client, bill_id):
    client.post("/logout")
    register_and_login(client, "intruder")
    assert client.get(f"/api/bill/{bill_id}").status_code == 404
    resp = client.post(f"/bill/{bill_id}/add", data={"name": "X", "amount": "1"})
    assert resp.status_code == 302 and resp.headers["Location"] == "/"
    client.post(f"/bill/{bill_id}/delete")
    client.post("/logout")
    register_and_login(client)
    assert api(client, bill_id)["payments"] == []


def test_login_required(client):
    assert client.get("/").status_code == 302


def test_migrates_old_payer_names(app):
    """Databases from the old version stored a name on each payment."""
    from db import get_db, init_db

    with get_db() as conn, conn.cursor() as cur:
        cur.execute("DROP TABLE settlements, payments, people CASCADE")
        cur.execute("CREATE TABLE payments (id SERIAL PRIMARY KEY, bill_id INTEGER NOT NULL REFERENCES bills(id), "
                    "name TEXT NOT NULL, amount NUMERIC(12,2) NOT NULL)")
        cur.execute("INSERT INTO users (username, password) VALUES ('old', 'x') RETURNING id")
        uid = cur.fetchone()["id"]
        cur.execute("INSERT INTO bills (user_id, name) VALUES (%s, 'Old trip') RETURNING id", (uid,))
        bid = cur.fetchone()["id"]
        cur.execute("INSERT INTO payments (bill_id, name, amount) VALUES (%s,'Amina',10),(%s,'amina ',5),(%s,'Brian',3)",
                    (bid, bid, bid))
    init_db()
    with get_db() as conn, conn.cursor() as cur:
        cur.execute("SELECT name FROM people ORDER BY id")
        assert [r["name"] for r in cur.fetchall()] == ["Amina", "Brian"]
        cur.execute("SELECT pe.name, sum(p.amount) AS total FROM payments p JOIN people pe ON pe.id=p.person_id "
                    "GROUP BY pe.name ORDER BY pe.name")
        assert [(r["name"], float(r["total"])) for r in cur.fetchall()] == [("Amina", 15.0), ("Brian", 3.0)]
