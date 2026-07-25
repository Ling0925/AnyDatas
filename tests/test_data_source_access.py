from __future__ import annotations

from app.db import DEFAULT_USER_ID, DEFAULT_WORKSPACE_ID, connect, decode_json, record_audit, record_notification
from app.report_subscriptions import notify_report_subscribers
from app.runner import create_run


def upload_source(client, sample_csv_bytes):
    response = client.post(
        "/data-sources",
        data={"name": "private warehouse sales"},
        files={"file": ("sales.csv", sample_csv_bytes, "text/csv")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with connect() as conn:
        return conn.execute("SELECT * FROM data_sources WHERE name = 'private warehouse sales'").fetchone()


def create_project(client, source_id: str):
    response = client.post(
        "/projects",
        data={
            "name": "private warehouse analysis",
            "language": "sql",
            "data_source_id": source_id,
            "script": "SELECT * FROM data LIMIT 1;",
            "parameters_json": "{}",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with connect() as conn:
        return conn.execute("SELECT * FROM projects WHERE name = 'private warehouse analysis'").fetchone()


def add_member(client, email: str, name: str, role: str):
    response = client.post(
        "/workspace/members",
        data={"email": email, "name": name, "role": role},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with connect() as conn:
        return conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()


def become_member(client, user_id: str):
    client.cookies.set("anydatas_user_id", user_id)
    client.cookies.set("anydatas_workspace_id", DEFAULT_WORKSPACE_ID)


def become_owner(client):
    become_member(client, DEFAULT_USER_ID)


def test_private_data_source_grants_gate_views_runs_reports_and_derived_events(client, sample_csv_bytes):
    source = upload_source(client, sample_csv_bytes)
    project = create_project(client, source["id"])
    run_id = create_run(project["id"], "manual")
    report_response = client.post(
        "/reports",
        data={
            "project_id": project["id"],
            "title": "Private warehouse report",
            "description": "",
            "visibility": "workspace",
        },
        follow_redirects=False,
    )
    assert report_response.status_code == 303
    report_id = report_response.headers["location"].rsplit("/", 1)[-1]

    viewer = add_member(client, "source-viewer@example.com", "Source Viewer", "viewer")
    analyst = add_member(client, "source-analyst@example.com", "Source Analyst", "analyst")
    manager = add_member(client, "source-manager@example.com", "Source Manager", "analyst")
    outsider = add_member(client, "source-outsider@example.com", "Source Outsider", "analyst")

    assert client.post(
        f"/data-sources/{source['id']}/visibility",
        data={"visibility": "private"},
        follow_redirects=False,
    ).status_code == 303
    for member, permission in ((viewer, "view"), (analyst, "query"), (manager, "manage")):
        response = client.post(
            f"/data-sources/{source['id']}/grants",
            data={"user_id": member["id"], "permission": permission},
            follow_redirects=False,
        )
        assert response.status_code == 303

    with connect() as conn:
        source_row = conn.execute("SELECT * FROM data_sources WHERE id = ?", (source["id"],)).fetchone()
        grants = conn.execute(
            "SELECT user_id, permission FROM data_source_access_grants WHERE data_source_id = ? ORDER BY permission",
            (source["id"],),
        ).fetchall()
        record_notification(
            conn,
            DEFAULT_WORKSPACE_ID,
            "run.failed",
            "Private warehouse run failed",
            "Private warehouse detail",
            "error",
            "run",
            run_id,
        )
        record_audit(
            conn,
            "data_source.checked",
            "data_source",
            source["id"],
            {"name": source["name"]},
            DEFAULT_WORKSPACE_ID,
        )
    assert source_row["created_by_user_id"] == DEFAULT_USER_ID
    assert source_row["visibility"] == "private"
    assert {(grant["user_id"], grant["permission"]) for grant in grants} == {
        (viewer["id"], "view"),
        (analyst["id"], "query"),
        (manager["id"], "manage"),
    }

    become_member(client, outsider["id"])
    outsider_home = client.get("/")
    assert source["name"] not in outsider_home.text
    assert project["name"] not in outsider_home.text
    assert client.get(f"/data-sources/{source['id']}").status_code == 404
    assert client.get(f"/runs/{run_id}").status_code == 404
    assert client.get(f"/reports/{report_id}").status_code == 404
    assert client.post(f"/projects/{project['id']}/run", follow_redirects=False).status_code == 404
    assert "Private warehouse run failed" not in client.get("/api/notifications").text
    assert source["id"] not in client.get("/api/audit-events").text

    become_member(client, viewer["id"])
    viewer_source = client.get(f"/data-sources/{source['id']}")
    assert viewer_source.status_code == 200
    assert "view access" in viewer_source.text
    assert "Save Schema" not in viewer_source.text
    viewer_home = client.get("/")
    assert source["name"] in viewer_home.text
    assert project["name"] not in viewer_home.text
    assert client.get(f"/runs/{run_id}").status_code == 404

    become_member(client, analyst["id"])
    analyst_source = client.get(f"/data-sources/{source['id']}")
    assert analyst_source.status_code == 200
    assert "query access" in analyst_source.text
    assert "Save Schema" not in analyst_source.text
    assert project["name"] in client.get("/").text
    assert client.get(f"/runs/{run_id}").status_code == 200
    assert client.get(f"/reports/{report_id}").status_code == 200
    assert client.post(f"/reports/{report_id}/subscriptions", follow_redirects=False).status_code == 303
    assert client.post(
        f"/data-sources/{source['id']}/schema",
        data={
            "field_names": ["date", "revenue", "region"],
            "field_types": ["date", "number", "text"],
            "descriptions": ["", "", ""],
        },
        follow_redirects=False,
    ).status_code == 403
    assert "Private warehouse run failed" in client.get("/api/notifications").text

    with connect() as conn:
        record_notification(
            conn,
            DEFAULT_WORKSPACE_ID,
            "report.refresh_succeeded",
            "Private warehouse report refreshed",
            "Private report detail",
            "success",
            "report",
            report_id,
            analyst["id"],
        )

    become_member(client, manager["id"])
    manager_source = client.get(f"/data-sources/{source['id']}")
    assert manager_source.status_code == 200
    assert "Save Schema" in manager_source.text
    assert f'action="/data-sources/{source["id"]}/visibility"' in manager_source.text
    assert client.post(
        f"/data-sources/{source['id']}/grants",
        data={"user_id": outsider["id"], "permission": "view"},
        follow_redirects=False,
    ).status_code == 303

    become_owner(client)
    assert client.post(
        f"/data-sources/{source['id']}/grants/{analyst['id']}/delete",
        follow_redirects=False,
    ).status_code == 303
    become_member(client, analyst["id"])
    assert client.get(f"/data-sources/{source['id']}").status_code == 404
    assert client.get(f"/runs/{run_id}").status_code == 404
    with connect() as conn:
        subscription = conn.execute(
            "SELECT 1 FROM report_subscriptions WHERE report_id = ? AND user_id = ?",
            (report_id, analyst["id"]),
        ).fetchone()
        notification = conn.execute(
            "SELECT 1 FROM notifications WHERE resource_type = 'report' AND resource_id = ? AND recipient_user_id = ?",
            (report_id, analyst["id"]),
        ).fetchone()
    assert subscription is None
    assert notification is None


def test_data_source_access_rejects_invalid_permissions_and_viewer_escalation(client, sample_csv_bytes):
    source = upload_source(client, sample_csv_bytes)
    viewer = add_member(client, "permission-viewer@example.com", "Permission Viewer", "viewer")

    assert client.post(
        f"/data-sources/{source['id']}/grants",
        data={"user_id": viewer["id"], "permission": "view"},
        follow_redirects=False,
    ).status_code == 303
    assert client.post(
        f"/data-sources/{source['id']}/visibility",
        data={"visibility": "private"},
        follow_redirects=False,
    ).status_code == 303
    rejected = client.post(
        f"/data-sources/{source['id']}/grants",
        data={"user_id": viewer["id"], "permission": "query"},
        follow_redirects=False,
    )
    assert rejected.status_code == 303
    assert "Viewer%20members%20can%20receive%20view%20access%20only" in rejected.headers["location"]
    invalid = client.post(
        f"/data-sources/{source['id']}/grants",
        data={"user_id": viewer["id"], "permission": "owner"},
        follow_redirects=False,
    )
    assert invalid.status_code == 303
    assert "Unsupported%20data%20source%20permission" in invalid.headers["location"]


def test_legacy_workspace_source_is_claimed_by_the_analyst_who_makes_it_private(client, sample_csv_bytes):
    source = upload_source(client, sample_csv_bytes)
    analyst = add_member(client, "legacy-source-analyst@example.com", "Legacy Source Analyst", "analyst")
    with connect() as conn:
        conn.execute("UPDATE data_sources SET created_by_user_id = NULL WHERE id = ?", (source["id"],))

    become_member(client, analyst["id"])
    response = client.post(
        f"/data-sources/{source['id']}/visibility",
        data={"visibility": "private"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    with connect() as conn:
        updated_source = conn.execute("SELECT * FROM data_sources WHERE id = ?", (source["id"],)).fetchone()
    assert updated_source["visibility"] == "private"
    assert updated_source["created_by_user_id"] == analyst["id"]
    assert client.get(f"/data-sources/{source['id']}").status_code == 200


def test_data_source_classification_is_validated_and_audited(client, sample_csv_bytes):
    source = upload_source(client, sample_csv_bytes)

    invalid = client.post(
        f"/data-sources/{source['id']}/classification",
        data={"classification": "secret"},
        follow_redirects=False,
    )
    assert invalid.status_code == 303
    assert "Data%20classification%20must%20be" in invalid.headers["location"]

    updated = client.post(
        f"/data-sources/{source['id']}/classification",
        data={"classification": "confidential"},
        follow_redirects=False,
    )
    assert updated.status_code == 303
    with connect() as conn:
        source_row = conn.execute("SELECT classification FROM data_sources WHERE id = ?", (source["id"],)).fetchone()
        audit = conn.execute(
            "SELECT * FROM audit_events WHERE action = 'data_source.classification_updated' AND resource_id = ?",
            (source["id"],),
        ).fetchone()

    assert source_row["classification"] == "confidential"
    assert decode_json(audit["detail_json"], {}) == {"previous": "internal", "classification": "confidential"}


def test_restricted_source_exports_require_manage_access(client, sample_csv_bytes):
    source = upload_source(client, sample_csv_bytes)
    project = create_project(client, source["id"])
    run_response = client.post(f"/projects/{project['id']}/run", follow_redirects=False)
    assert run_response.status_code == 303
    run_id = run_response.headers["location"].rsplit("/", 1)[-1]
    report_response = client.post(
        "/reports",
        data={"project_id": project["id"], "title": "Restricted result report", "description": "", "visibility": "workspace"},
        follow_redirects=False,
    )
    assert report_response.status_code == 303
    report_id = report_response.headers["location"].rsplit("/", 1)[-1]
    analyst = add_member(client, "restricted-query@example.com", "Restricted Query", "analyst")
    assert client.post(
        f"/data-sources/{source['id']}/classification",
        data={"classification": "restricted"},
        follow_redirects=False,
    ).status_code == 303

    become_member(client, analyst["id"])
    run_page = client.get(f"/runs/{run_id}")
    report_page = client.get(f"/reports/{report_id}")
    assert run_page.status_code == 200
    assert report_page.status_code == 200
    assert "Download CSV" not in run_page.text
    assert "Downloads require manage access" in run_page.text
    assert "Downloads require manage access" in report_page.text
    assert client.post(
        f"/data-sources/{source['id']}/classification",
        data={"classification": "public"},
        follow_redirects=False,
    ).status_code == 403
    assert client.get(f"/runs/{run_id}/result.csv").status_code == 403
    assert client.get(f"/runs/{run_id}/result.json").status_code == 403
    assert client.get(f"/reports/{report_id}/snapshot.csv").status_code == 403
    assert client.get(f"/reports/{report_id}/snapshot.json").status_code == 403
    assert client.get(f"/reports/{report_id}/snapshot.xlsx").status_code == 403
    assert client.get(f"/reports/{report_id}/snapshot.png").status_code == 403
    assert client.get(f"/reports/{report_id}/snapshot.pdf").status_code == 403

    become_owner(client)
    assert client.get(f"/runs/{run_id}/result.csv").status_code == 200
    assert client.get(f"/runs/{run_id}/result.json").status_code == 200
    assert client.get(f"/reports/{report_id}/snapshot.csv").status_code == 200
    assert client.get(f"/reports/{report_id}/snapshot.json").status_code == 200
    assert client.get(f"/reports/{report_id}/snapshot.xlsx").status_code == 200
    assert client.get(f"/reports/{report_id}/snapshot.png").status_code == 200
    assert client.get(f"/reports/{report_id}/snapshot.pdf").status_code == 200
    with connect() as conn:
        run_exports = conn.execute(
            "SELECT detail_json FROM audit_events WHERE action = 'run.exported' AND resource_id = ? ORDER BY created_at",
            (run_id,),
        ).fetchall()
        report_exports = conn.execute(
            "SELECT detail_json FROM audit_events WHERE action = 'report.exported' AND resource_id = ? ORDER BY created_at",
            (report_id,),
        ).fetchall()

    assert [decode_json(event["detail_json"], {})["classification"] for event in run_exports] == ["restricted", "restricted"]
    assert [decode_json(event["detail_json"], {})["classification"] for event in report_exports] == [
        "restricted",
        "restricted",
        "restricted",
        "restricted",
        "restricted",
    ]


def test_report_delivery_rechecks_data_source_permission_for_a_stale_subscription(client, sample_csv_bytes):
    source = upload_source(client, sample_csv_bytes)
    project = create_project(client, source["id"])
    run_id = create_run(project["id"], "manual")
    report_response = client.post(
        "/reports",
        data={"project_id": project["id"], "title": "Delivery access report", "description": "", "visibility": "workspace"},
        follow_redirects=False,
    )
    assert report_response.status_code == 303
    report_id = report_response.headers["location"].rsplit("/", 1)[-1]
    analyst = add_member(client, "delivery-access-analyst@example.com", "Delivery Access Analyst", "analyst")
    assert client.post(
        f"/data-sources/{source['id']}/visibility",
        data={"visibility": "private"},
        follow_redirects=False,
    ).status_code == 303
    assert client.post(
        f"/data-sources/{source['id']}/grants",
        data={"user_id": analyst["id"], "permission": "query"},
        follow_redirects=False,
    ).status_code == 303

    become_member(client, analyst["id"])
    assert client.post(f"/reports/{report_id}/subscriptions", follow_redirects=False).status_code == 303
    become_owner(client)
    assert client.post(
        f"/data-sources/{source['id']}/grants/{analyst['id']}/delete",
        follow_redirects=False,
    ).status_code == 303

    with connect() as conn:
        report = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        conn.execute(
            "DELETE FROM report_subscriptions WHERE report_id = ? AND user_id = ?",
            (report_id, DEFAULT_USER_ID),
        )
        conn.execute(
            "INSERT INTO report_subscriptions (report_id, user_id, workspace_id, created_at) VALUES (?, ?, ?, ?)",
            (report_id, analyst["id"], DEFAULT_WORKSPACE_ID, "2026-07-11T00:00:00+00:00"),
        )
        delivered = notify_report_subscribers(
            conn,
            report,
            {"id": run_id, "status": "succeeded", "error": ""},
        )
        notification = conn.execute(
            "SELECT 1 FROM notifications WHERE resource_type = 'report' AND resource_id = ? AND recipient_user_id = ?",
            (report_id, analyst["id"]),
        ).fetchone()

    assert delivered == 0
    assert notification is None
