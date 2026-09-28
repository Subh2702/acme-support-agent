from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db.models import Customer, Order, OrderStatus, PaymentMethod, Refund
from app.db.seed import TOTAL_CUSTOMERS, TOTAL_ORDERS, demo_credentials, seed
from app.db.session import Database
from app.main import create_app
from tests.conftest import FIXED_NOW, make_settings


def _snapshot(db: Database) -> list[tuple]:
    with db.session() as s:
        return [
            (o.order_number, o.customer.email, o.status, o.total_paise, o.ship_pin, o.created_at)
            for o in s.scalars(select(Order).order_by(Order.order_number))
        ]


def test_seed_counts_and_idempotency(db: Database) -> None:
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Customer)) == TOTAL_CUSTOMERS
        assert s.scalar(select(func.count()).select_from(Order)) == TOTAL_ORDERS
        statuses = set(s.scalars(select(Order.status)))
    assert statuses == set(OrderStatus.ALL)
    assert seed(db, FIXED_NOW) is False  # second call is a no-op
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Order)) == TOTAL_ORDERS


def test_seed_is_deterministic() -> None:
    a, b = Database("sqlite://"), Database("sqlite://")
    for d in (a, b):
        d.create_all()
        seed(d, FIXED_NOW)
    assert _snapshot(a) == _snapshot(b)


def test_seed_money_and_price_range(db: Database) -> None:
    with db.session() as s:
        orders = list(s.scalars(select(Order)))
        for order in orders:
            assert isinstance(order.total_paise, int)
            assert order.total_paise == sum(i.unit_price_paise * i.quantity for i in order.items)
            assert min(i.unit_price_paise for i in order.items) >= 29900
            assert max(i.unit_price_paise for i in order.items) <= 4500000
            assert len(order.ship_pin) == 6 and order.ship_pin[0] != "0"
        assert {o.payment_method for o in orders} == set(PaymentMethod.ALL)


def test_seed_edge_case_fixtures(db: Database) -> None:
    with db.session() as s:

        def get(n: str) -> Order:
            return s.scalars(select(Order).where(Order.order_number == n)).one()

        assert FIXED_NOW - get("AO-10004").delivered_at == timedelta(days=29)
        assert FIXED_NOW - get("AO-10005").delivered_at == timedelta(days=31)
        assert get("AO-10008").status == OrderStatus.REFUNDED
        assert len(get("AO-10008").refunds) == 1
        assert get("AO-10010").payment_method == PaymentMethod.COD
        assert get("AO-10007").total_paise > 1_000_000
        assert get("AO-10002").status == OrderStatus.PROCESSING
        assert get("AO-10020").customer.email != get("AO-10001").customer.email
        for order in s.scalars(select(Order)):
            if order.shipped_at:
                assert order.shipped_at >= order.created_at
            if order.delivered_at:
                assert order.shipped_at and order.delivered_at >= order.shipped_at
                assert order.delivered_at <= FIXED_NOW
        refunded = s.scalars(select(Order).where(Order.status == OrderStatus.REFUNDED)).all()
        assert all(o.refunds for o in refunded)
        assert s.scalar(select(func.count()).select_from(Refund)) == len(refunded)


def test_demo_credentials_match_data(db: Database) -> None:
    creds = demo_credentials()
    assert len(creds) == 3
    with db.session() as s:
        for c in creds:
            order = s.scalars(select(Order).where(Order.order_number == c["order_number"])).one()
            assert order.customer.email == c["email"]
            assert order.ship_pin == c["pin_code"]


def test_health_endpoints() -> None:
    app = create_app(make_settings())
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        ready = client.get("/readyz")
        assert ready.status_code == 200
        assert ready.json()["checks"]["database"] == "ok"


def test_postgres_url_normalised() -> None:
    s = make_settings(database_url="postgres://u:p@host:5432/db")
    assert s.database_url == "postgresql+psycopg://u:p@host:5432/db"
    assert not s.is_sqlite
