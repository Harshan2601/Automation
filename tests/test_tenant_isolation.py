from app import db


def test_customer_scoped_post_lookup(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    db_path = data_dir / "app.db"
    monkeypatch.setattr(db, "DATA_DIR", data_dir)
    monkeypatch.setattr(db, "DATABASE_PATH", db_path)
    db.init_db()
    first = db.create_customer(telegram_user_id=1001, display_name="Customer A")
    second = db.create_customer(telegram_user_id=1002, display_name="Customer B")

    post = db.create_post(first["id"], "voice transcript", "A proposed post about a project.", str(data_dir / "photos" / first["id"] / "a.jpg"))
    assert db.get_post_for_customer(post["id"], first["id"]) is not None
    assert db.get_post_for_customer(post["id"], second["id"]) is None
    assert db.get_pending_post(second["id"]) is None


def test_publisher_queue_excludes_unapproved_posts(monkeypatch, tmp_path):
    from datetime import datetime, timedelta, timezone

    data_dir = tmp_path / "data"
    monkeypatch.setattr(db, "DATA_DIR", data_dir)
    monkeypatch.setattr(db, "DATABASE_PATH", data_dir / "app.db")
    db.init_db()
    customer = db.create_customer(telegram_user_id=2001, display_name="Approval Test")
    post = db.create_post(customer["id"], "transcript", "A proposed post with enough content.", str(data_dir / "photos" / "photo.jpg"))

    # Draft status alone must never enter the publisher queue.
    assert all(item["id"] != post["id"] for item in db.get_due_posts())

    scheduled_time = datetime.now(timezone.utc) - timedelta(minutes=1)
    approved = db.approve_and_schedule(post["id"], customer["id"], scheduled_time)
    assert approved is not None
    due = db.get_due_posts()
    assert any(item["id"] == post["id"] and item["approved_at"] for item in due)

    # Atomic claim changes the state so another scheduler cycle cannot claim it again.
    assert db.claim_post_for_publishing(post["id"], customer["id"])
    assert not db.claim_post_for_publishing(post["id"], customer["id"])
