import importlib
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient


class FakeAggregateCursor:
    def __init__(self, docs, pipeline):
        self.docs = docs
        self.pipeline = pipeline

    def explain(self, method):
        return {"method": method, "pipeline": self.pipeline, "nDocs": len(self.docs)}

    def __iter__(self):
        return iter(self.docs)


class FakeCursor:
    def __init__(self, docs):
        self.docs = list(docs)
        self._skip = 0
        self._limit = None
        self.sort_spec = None

    def sort(self, key_or_keys, direction=None):
        if isinstance(key_or_keys, list):
            items = key_or_keys
        else:
            items = [(key_or_keys, direction or 1)]
        self.sort_spec = items
        for key, direction_value in items:
            self.docs = sorted(self.docs, key=lambda doc: doc.get(key, ""), reverse=direction_value == -1)
        return self

    def skip(self, n):
        self._skip = n
        return self

    def limit(self, n):
        self._limit = n
        return self

    def __iter__(self):
        docs = self.docs[self._skip:]
        if self._limit is not None:
            docs = docs[: self._limit]
        return iter(docs)


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = [] if docs is None else list(docs)
        self.related_collections = {}
        self.last_find = None
        self.last_aggregate_pipeline = None
        self.index_specs = []

    def create_index(self, *args, **kwargs):
        self.index_specs.append((args, kwargs))
        return None

    def insert_one(self, doc):
        doc = dict(doc)
        doc.setdefault("_id", object())
        self.docs.append(doc)
        return type("Result", (), {"inserted_id": doc["_id"]})()

    def find_one(self, query=None, projection=None):
        for doc in self.docs:
            if self._matches(doc, query or {}):
                record = dict(doc)
                if projection:
                    record = {k: v for k, v in record.items() if k in projection or projection.get(k) is not None}
                    if projection.get("_id") == 0:
                        record.pop("_id", None)
                return record
        return None

    def count_documents(self, query=None):
        return sum(1 for doc in self.docs if self._matches(doc, query or {}))

    def find(self, query=None, projection=None):
        items = []
        for doc in self.docs:
            if self._matches(doc, query or {}):
                record = dict(doc)
                if projection:
                    projected = {}
                    for key, value in record.items():
                        if key == "_id":
                            if projection.get("_id", 1) != 0:
                                projected[key] = value
                            continue
                        if isinstance(projection, dict) and key in projection:
                            projected[key] = value
                        elif isinstance(projection, dict) and projection.get(key) is not None:
                            projected[key] = value
                    record = projected
                items.append(record)
        cursor = FakeCursor(items)
        self.last_find = {"filter": query or {}, "projection": projection, "cursor": cursor}
        return cursor

    def update_one(self, filter_query, update):
        for doc in self.docs:
            if self._matches(doc, filter_query):
                if "$set" in update:
                    doc.update(update["$set"])
                if "$push" in update:
                    for key, value in update["$push"].items():
                        doc.setdefault(key, []).append(value)
                return type("Result", (), {"modified_count": 1})()
        return type("Result", (), {"modified_count": 0})()

    def aggregate(self, pipeline):
        self.last_aggregate_pipeline = pipeline
        if any("$setWindowFields" in stage for stage in pipeline) and any(
            "$lookup" in stage and stage["$lookup"].get("from") == "employees"
            for stage in pipeline
        ):
            return FakeAggregateCursor(self._aggregate_late_leaderboard(pipeline), pipeline)
        if any("_trend_offsets" in stage.get("$set", {}) for stage in pipeline):
            return FakeAggregateCursor(self._aggregate_department_trend(pipeline), pipeline)
        if any(
            "$lookup" in stage and stage["$lookup"].get("from") == "attendance_logs"
            for stage in pipeline
        ):
            return FakeAggregateCursor(self._aggregate_department_summary(pipeline), pipeline)
        if len(pipeline) > 1 and pipeline[1].get("$group", {}).get("_id") is None:
            return FakeAggregateCursor(self._aggregate_employee_monthly(pipeline), pipeline)

        docs = [dict(doc) for doc in self.docs]
        for stage in pipeline:
            if "$match" in stage:
                docs = [doc for doc in docs if self._matches(doc, stage["$match"])]
            elif "$group" in stage:
                group = stage["$group"]
                if group.get("_id") is None:
                    values = {k: 0 for k in group if k != "_id"}
                    for doc in docs:
                        for key, expr in group.items():
                            if key == "_id":
                                continue
                            if isinstance(expr, dict):
                                if "$sum" in expr:
                                    arg = expr["$sum"]
                                    if isinstance(arg, dict) and "$cond" in arg:
                                        condition = arg["$cond"][0]
                                        if condition == {"$gt": ["$late_minutes", 0]}:
                                            values[key] += int(doc.get("late_minutes", 0) > 0)
                                    elif isinstance(arg, str):
                                        values[key] += doc.get(arg, 0)
                                    else:
                                        values[key] += 1
                                elif "$avg" in expr:
                                    values[key] = values.get(key, 0) + doc.get(expr["$avg"], 0)
                    docs = [{"_id": None, **values}]
                else:
                    grouped = {}
                    group_key = group["_id"]
                    for doc in docs:
                        key = doc.get(group_key)
                        grouped.setdefault(key, {"_id": key})
                        for field, expr in group.items():
                            if field == "_id":
                                continue
                            if isinstance(expr, dict) and "$sum" in expr:
                                arg = expr["$sum"]
                                if isinstance(arg, dict) and "$cond" in arg:
                                    grouped[key].setdefault(field, 0)
                                    grouped[key][field] += int(self._matches(doc, {arg["$cond"][0][1]: {"$gt": 0}}))
                                else:
                                    grouped[key].setdefault(field, 0)
                                    grouped[key][field] += doc.get(arg, 0)
                    docs = list(grouped.values())
            elif "$sort" in stage:
                sort_spec = stage["$sort"]
                docs = sorted(docs, key=lambda row: tuple(row.get(k, 0) for k in sort_spec), reverse=all(v == -1 for v in sort_spec.values()))
        return FakeAggregateCursor(docs, pipeline)

    def _aggregate_employee_monthly(self, pipeline):
        query = pipeline[0]["$match"]
        docs = [doc for doc in self.docs if self._matches(doc, query)]
        totals = {
            "present_days": 0.0,
            "leave_days": 0,
            "late_count": 0,
            "total_late_minutes": 0,
            "total_overtime_minutes": 0,
        }
        for doc in docs:
            weekday = datetime.fromisoformat(doc["date"]).weekday()
            if doc.get("status") in {"PRESENT", "WFH", "ON_DUTY"} and weekday < 5:
                totals["present_days"] += 0.5 if doc.get("half_day", False) else 1
            totals["leave_days"] += int(doc.get("status") == "LEAVE")
            late = int(doc.get("late_minutes") or 0)
            totals["late_count"] += int(late > 0)
            totals["total_late_minutes"] += late
            totals["total_overtime_minutes"] += int(doc.get("overtime_minutes") or 0)
        return [{"_id": None, **totals}] if docs else []

    def _aggregate_department_summary(self, pipeline):
        employees = self.related_collections["employees"]
        attendance = self.related_collections["attendance_logs"]
        query = pipeline[0]["$match"]
        end_date = pipeline[1]["$lookup"]["pipeline"][0]["$match"]["$expr"]["$and"][2]["$lte"][1]
        start_date = pipeline[1]["$lookup"]["pipeline"][0]["$match"]["$expr"]["$and"][1]["$gte"][1]
        result = {}
        for employee in employees.docs:
            if not self._matches(employee, query):
                continue
            row = result.setdefault(employee["department"], {
                "_id": employee["department"],
                "headcount": 0,
                "present_days": 0.0,
                "avg_work_hours_total": 0.0,
                "avg_work_hours_count": 0,
                "late_count": 0,
                "total_late_minutes": 0,
                "leave_count": 0,
                "on_duty_count": 0,
            })
            row["headcount"] += 1
            for log in attendance.docs:
                if log.get("emp_code") != employee.get("emp_code") or not start_date <= log.get("date", "") <= end_date:
                    continue
                weekday = datetime.fromisoformat(log["date"]).weekday()
                if log.get("status") in {"PRESENT", "WFH", "ON_DUTY"} and weekday < 5:
                    row["present_days"] += 0.5 if log.get("half_day", False) else 1
                if log.get("status") in {"PRESENT", "WFH", "ON_DUTY"} and log.get("work_hours") is not None:
                    row["avg_work_hours_total"] += log["work_hours"]
                    row["avg_work_hours_count"] += 1
                late = int(log.get("late_minutes") or 0)
                row["late_count"] += int(late > 0)
                row["total_late_minutes"] += late
                row["leave_count"] += int(log.get("status") == "LEAVE")
                row["on_duty_count"] += int(log.get("status") == "ON_DUTY")
        return sorted(result.values(), key=lambda row: row["_id"])

    def _aggregate_late_leaderboard(self, pipeline):
        employees = self.related_collections["employees"]
        query = pipeline[0]["$match"]
        totals = {}
        for log in self.docs:
            if not self._matches(log, query):
                continue
            code = log.get("emp_code")
            row = totals.setdefault(code, {"total_late_minutes": 0, "late_count": 0})
            late = int(log.get("late_minutes") or 0)
            row["total_late_minutes"] += late
            row["late_count"] += int(late > 0)
        rows = []
        for code, totals_for_employee in totals.items():
            if totals_for_employee["total_late_minutes"] <= 0:
                continue
            employee = next((item for item in employees.docs if item.get("emp_code") == code), None)
            if employee is None:
                continue
            rows.append({"_id": code, **totals_for_employee, "employee": employee})
        department_match = next(
            (stage["$match"]["employee.department"] for stage in pipeline if "$match" in stage and "employee.department" in stage["$match"]),
            None,
        )
        if department_match is not None:
            rows = [row for row in rows if row["employee"].get("department") == department_match]
        rows.sort(key=lambda row: (-row["total_late_minutes"], row["_id"]))
        rank = 0
        previous_total = None
        for index, row in enumerate(rows, 1):
            if row["total_late_minutes"] != previous_total:
                rank = index
            row["rank"] = rank
            previous_total = row["total_late_minutes"]
        limit = next(stage["$match"]["rank"]["$lte"] for stage in pipeline if "$match" in stage and "rank" in stage["$match"])
        return [{
            "rank": row["rank"],
            "emp_code": row["_id"],
            "name": row["employee"]["name"],
            "department": row["employee"]["department"],
            "total_late_minutes": row["total_late_minutes"],
            "late_count": row["late_count"],
        } for row in rows if row["rank"] <= limit]

    def _aggregate_department_trend(self, pipeline):
        attendance = self.related_collections["attendance_logs"]
        date_stage = next(stage for stage in pipeline if "_trend_date" in stage.get("$set", {}))
        start = datetime.fromisoformat(
            date_stage["$set"]["_trend_date"]["$dateAdd"]["startDate"]["$dateFromString"]["dateString"]
        ).date()
        day_count = pipeline[1]["$set"]["_trend_offsets"]["$range"][1]
        department = pipeline[0]["$match"]["department"]
        employees = [doc for doc in self.docs if doc.get("department") == department]
        output = []
        rates = []
        for offset in range(day_count):
            current = start + timedelta(days=offset)
            date_string = current.isoformat()
            logs = [
                log for log in attendance.docs
                if log.get("date") == date_string
                and any(employee.get("emp_code") == log.get("emp_code") for employee in employees)
            ]
            headcount = sum(employee.get("joined_on", "") <= date_string for employee in employees)
            present = sum(
                0.5 if log.get("half_day", False) else 1
                for log in logs if log.get("status") in {"PRESENT", "WFH", "ON_DUTY"}
            )
            late_count = sum(int(log.get("late_minutes") or 0) > 0 for log in logs)
            working_day = current.weekday() < 5
            rate = present / headcount if working_day and headcount else None
            rates.append(rate)
            window_rates = [value for value in rates[-7:] if value is not None]
            output.append({
                "date": date_string,
                "is_working_day": working_day,
                "headcount": headcount,
                "present_count": round(present, 4),
                "late_count": late_count,
                "attendance_rate": round(rate, 4) if rate is not None else None,
                "moving_avg_7d": round(sum(window_rates) / len(window_rates), 4) if window_rates else None,
            })
        return output

    @staticmethod
    def _matches(doc, query):
        if not query:
            return True
        for key, value in query.items():
            if isinstance(value, dict):
                for op, expected in value.items():
                    if op == "$gte" and not (doc.get(key) >= expected):
                        return False
                    if op == "$lte" and not (doc.get(key) <= expected):
                        return False
                    if op == "$gt" and not (doc.get(key) > expected):
                        return False
                    if op == "$in" and doc.get(key) not in expected:
                        return False
                    if op == "$ne" and doc.get(key) == expected:
                        return False
            else:
                if doc.get(key) != value:
                    return False
        return True


class FakeDatabase:
    def __init__(self):
        self.employees = FakeCollection()
        self.attendance_logs = FakeCollection()
        self.last_command = None
        self.employees.related_collections = {
            "employees": self.employees,
            "attendance_logs": self.attendance_logs,
        }
        self.attendance_logs.related_collections = {
            "employees": self.employees,
            "attendance_logs": self.attendance_logs,
        }

    def command(self, command, *args, **kwargs):
        self.last_command = command
        return {
            "fake_explain_command": command["explain"],
            "verbosity": command["verbosity"],
        }


class FakeAdmin:
    @staticmethod
    def command(*args, **kwargs):
        return {"ok": 1}


class FakeClient:
    admin = FakeAdmin()


@pytest.fixture
def app_module(monkeypatch):
    monkeypatch.setenv("MONGO_URI", "mongodb://localhost:27017")
    monkeypatch.setenv("MONGO_DB", "test_attendance_db")
    import app.main as main
    importlib.reload(main)
    main.client = FakeClient()
    main.db = FakeDatabase()
    return main


@pytest.fixture
def client(app_module):
    return TestClient(app_module.app)


@pytest.fixture
def employee_factory():
    def factory(**overrides):
        payload = {
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2024-01-01",
        }
        payload.update(overrides)
        return payload

    return factory


def test_health_endpoint(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_frontend_is_served_by_fastapi(client):
    page = client.get("/")
    script = client.get("/static/app.js")
    stylesheet = client.get("/static/styles.css")

    assert page.status_code == 200
    assert "HROne | People & Attendance" in page.text
    assert script.status_code == 200
    assert "/employees?page=" in script.text
    assert '"/attendance/punch-in"' in script.text
    assert stylesheet.status_code == 200


def test_employee_create_list_and_validation(client, employee_factory):
    create = client.post("/employees", json=employee_factory())
    assert create.status_code == 201
    body = create.json()
    assert body["emp_code"] == "EMP0001"
    assert isinstance(body["created_at"], int)

    second = client.post("/employees", json=employee_factory(emp_code="EMP0002", email="two@example.com"))
    assert second.status_code == 201

    list_response = client.get("/employees", params={"page": 1, "page_size": 10})
    assert list_response.status_code == 200
    payload = list_response.json()
    assert payload["total"] == 2
    assert payload["page"] == 1
    assert payload["page_size"] == 10
    assert [item["emp_code"] for item in payload["items"]] == ["EMP0001", "EMP0002"]

    invalid = client.post("/employees", json={
        "emp_code": "BAD",
        "name": "Bad",
        "email": "bad@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2024-02-03",
    })
    assert invalid.status_code == 422


def test_punch_in_duplicate_and_concurrent_conflict(client, employee_factory):
    client.post("/employees", json=employee_factory(emp_code="EMP1001"))
    punch_in = {"emp_code": "EMP1001", "punched_at": 1718189400000, "status": "PRESENT"}
    assert client.post("/attendance/punch-in", json=punch_in).status_code == 201
    assert client.post("/attendance/punch-in", json=punch_in).status_code == 409


def test_punch_out_and_regularization_history(client, employee_factory, app_module):
    client.post("/employees", json=employee_factory(emp_code="EMP1002"))

    ist = app_module.IST
    punch_in_ms = int(datetime(2024, 6, 10, 9, 20, tzinfo=ist).timestamp() * 1000)
    punch_out_ms = int(datetime(2024, 6, 10, 18, 15, tzinfo=ist).timestamp() * 1000)
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP1002", "punched_at": punch_in_ms, "status": "PRESENT"}).status_code == 201
    out = client.post("/attendance/punch-out", json={"emp_code": "EMP1002", "punched_at": punch_out_ms})
    assert out.status_code == 200
    body = out.json()
    assert body["date"] == "2024-06-10"
    assert body["late_minutes"] == 0
    assert body["work_hours"] == 8.92
    assert body["half_day"] is False

    patch = client.patch(
        "/attendance/EMP1002/2024-06-10",
        json={"status": "LEAVE", "reason": "Approved leave", "regularized_by": "HR"},
    )
    assert patch.status_code == 200
    updated = patch.json()
    assert updated["status"] == "LEAVE"
    assert updated["punch_in"] is None
    assert updated["punch_out"] is None
    assert updated["history"][-1]["changes"]["status"]["to"] == "LEAVE"

    noop = client.patch(
        "/attendance/EMP1002/2024-06-10",
        json={"status": "LEAVE", "reason": "Approved leave", "regularized_by": "HR"},
    )
    assert noop.status_code == 422


def test_recent_dates_and_overnight_shift(client, employee_factory, app_module):
    client.post("/employees", json=employee_factory(emp_code="EMP1003", shift_start="22:00", shift_end="06:00"))
    punch_in_ms = int(datetime(2024, 6, 11, 2, 0, tzinfo=app_module.IST).timestamp() * 1000)
    punch_out_ms = int(datetime(2024, 6, 11, 8, 40, tzinfo=app_module.IST).timestamp() * 1000)
    response = client.post("/attendance/punch-in", json={"emp_code": "EMP1003", "punched_at": punch_in_ms, "status": "PRESENT"})
    assert response.status_code == 201
    assert response.json()["date"] == "2024-06-10"
    out = client.post("/attendance/punch-out", json={"emp_code": "EMP1003", "punched_at": punch_out_ms})
    assert out.status_code == 200
    body = out.json()
    assert body["work_hours"] == 6.67
    assert body["half_day"] is False


def test_attendance_list_pagination_and_filters(client, employee_factory):
    for code, dept in [("EMP1101", "Engineering"), ("EMP1102", "Support")]:
        client.post("/employees", json=employee_factory(emp_code=code, department=dept, email=f"{code.lower()}@example.com"))

    for code, date_ in [("EMP1101", "2024-06-05"), ("EMP1101", "2024-06-06"), ("EMP1102", "2024-06-05")]:
        punch_in_ms = int(datetime.fromisoformat(f"{date_}T09:30:00+00:00").timestamp() * 1000)
        punch_out_ms = int(datetime.fromisoformat(f"{date_}T18:30:00+00:00").timestamp() * 1000)
        client.post("/attendance/punch-in", json={"emp_code": code, "punched_at": punch_in_ms, "status": "PRESENT"})
        client.post("/attendance/punch-out", json={"emp_code": code, "punched_at": punch_out_ms})

    attendance = client.get("/attendance", params={"page": 1, "page_size": 2, "emp_code": "EMP1101"})
    assert attendance.status_code == 200
    payload = attendance.json()
    assert payload["total"] == 2
    assert payload["page"] == 1
    assert payload["page_size"] == 2
    assert [item["emp_code"] for item in payload["items"]] == ["EMP1101", "EMP1101"]


def test_attendance_list_validates_status_dates_and_range(client, employee_factory, app_module):
    client.post("/employees", json=employee_factory(emp_code="EMP1501"))
    for day, status in [("2024-06-10", "PRESENT"), ("2024-06-11", "WFH")]:
        punched_at = datetime.fromisoformat(f"{day}T09:30:00+05:30")
        punched_at_ms = int(punched_at.timestamp() * 1000)
        response = client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP1501", "punched_at": punched_at_ms, "status": status},
        )
        assert response.status_code == 201

    invalid_filters = [
        {"status": "INVALID"},
        {"status": ""},
        {"date_from": "2024-6-10"},
        {"date_from": "2024-02-30"},
        {"date_to": "not-a-date"},
        {"date_from": "2024-06-11", "date_to": "2024-06-10"},
    ]
    for params in invalid_filters:
        response = client.get("/attendance", params=params)
        assert response.status_code == 422, params

    valid = client.get(
        "/attendance",
        params={"date_from": "2024-06-11", "date_to": "2024-06-11", "status": "WFH"},
    )
    assert valid.status_code == 200
    assert valid.json()["total"] == 1
    assert [(item["date"], item["status"]) for item in valid.json()["items"]] == [
        ("2024-06-11", "WFH"),
    ]


def test_department_trend_rejects_malformed_dates_and_accepts_valid_range(client, employee_factory):
    client.post("/employees", json=employee_factory(emp_code="EMP1502", department="Engineering"))

    for params in [
        {"from": "2024-2-01", "to": "2024-02-07"},
        {"from": "2024-02-30", "to": "2024-03-01"},
        {"from": "2024-03-01", "to": "2024-02-29"},
        {"from": "2024-02-01", "to": "bad"},
    ]:
        response = client.get("/analytics/departments/Engineering/trend", params=params)
        assert response.status_code == 422, params

    valid = client.get(
        "/analytics/departments/Engineering/trend",
        params={"from": "2024-02-01", "to": "2024-02-07"},
    )
    assert valid.status_code == 200
    assert len(valid.json()["items"]) == 7
    assert valid.json()["items"][0]["date"] == "2024-02-01"
    assert valid.json()["items"][-1]["date"] == "2024-02-07"


def test_punch_out_before_punch_in_is_422_but_missing_record_is_404(client, employee_factory, app_module):
    client.post("/employees", json=employee_factory(emp_code="EMP1503"))
    client.post("/employees", json=employee_factory(emp_code="EMP1504"))
    punch_in = datetime(2024, 6, 10, 9, 30, tzinfo=app_module.IST)
    punch_in_ms = int(punch_in.timestamp() * 1000)
    created = client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP1503", "punched_at": punch_in_ms, "status": "PRESENT"},
    )
    assert created.status_code == 201

    before_punch_in_ms = int((punch_in - timedelta(minutes=1)).timestamp() * 1000)
    invalid = client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP1503", "punched_at": before_punch_in_ms},
    )
    assert invalid.status_code == 422

    missing = client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP1504", "punched_at": punch_in_ms},
    )
    assert missing.status_code == 404

    valid_punch_out_ms = int(datetime(2024, 6, 10, 18, 30, tzinfo=app_module.IST).timestamp() * 1000)
    valid = client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP1503", "punched_at": valid_punch_out_ms},
    )
    assert valid.status_code == 200
    assert valid.json()["punch_out"] == valid_punch_out_ms


def test_monthly_and_department_analytics(client, employee_factory, app_module):
    for person in [
        employee_factory(emp_code="EMP1201", department="Engineering", joined_on="2024-01-01"),
        employee_factory(emp_code="EMP1202", department="Engineering", joined_on="2024-01-01"),
        employee_factory(emp_code="EMP1203", department="Support", joined_on="2024-06-10"),
    ]:
        client.post("/employees", json=person)

    def punch(emp_code, date_string, punch_in_hm, punch_out_hm):
        punch_in_ms = int(datetime.fromisoformat(f"{date_string}T{punch_in_hm}:00").replace(tzinfo=app_module.IST).timestamp() * 1000)
        punch_out_ms = int(datetime.fromisoformat(f"{date_string}T{punch_out_hm}:00").replace(tzinfo=app_module.IST).timestamp() * 1000)
        client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": punch_in_ms, "status": "PRESENT"})
        client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": punch_out_ms})

    punch("EMP1201", "2024-06-03", "09:30", "18:30")
    punch("EMP1202", "2024-06-03", "10:15", "19:00")
    punch("EMP1201", "2024-06-04", "10:15", "14:00")
    punch("EMP1203", "2024-06-12", "09:00", "13:00")

    monthly = client.get("/analytics/employees/EMP1201/monthly", params={"month": "2024-06"})
    assert monthly.status_code == 200
    assert monthly.json()["late_count"] == 1
    assert monthly.json()["present_days"] == 1.5

    summary = client.get("/analytics/departments/summary", params={"month": "2024-06", "department": "Engineering"})
    assert summary.status_code == 200
    row = summary.json()["items"][0]
    assert row["department"] == "Engineering"
    assert row["headcount"] == 2
    assert row["present_days"] >= 2.5

    leaderboard = client.get("/analytics/leaderboard/late", params={"month": "2024-06", "limit": 10})
    assert leaderboard.status_code == 200
    assert len(leaderboard.json()["items"]) >= 1

    trend = client.get("/analytics/departments/Engineering/trend", params={"from": "2024-06-01", "to": "2024-06-07"})
    assert trend.status_code == 200
    assert len(trend.json()["items"]) == 7


def test_rounding_and_explain_endpoint(client, employee_factory):
    client.post("/employees", json=employee_factory(emp_code="EMP1301"))
    punch_in_ms = int(datetime(2024, 8, 12, 9, 30, tzinfo=timezone.utc).timestamp() * 1000)
    punch_out_ms = int(datetime(2024, 8, 12, 17, 10, tzinfo=timezone.utc).timestamp() * 1000)
    client.post("/attendance/punch-in", json={"emp_code": "EMP1301", "punched_at": punch_in_ms, "status": "PRESENT"})
    result = client.post("/attendance/punch-out", json={"emp_code": "EMP1301", "punched_at": punch_out_ms})
    assert result.status_code == 200
    assert abs(result.json()["work_hours"] - 7.67) < 1e-8

    response = client.get("/admin/explain/late_leaderboard", params={"month": "2024-08"})
    assert response.status_code == 200
    body = response.json()
    assert body["endpoint"] == "late_leaderboard"
    assert body["collection"] == "attendance_logs"
    assert "explain" in body


def test_explain_matches_endpoint_queries_and_aggregations(client, employee_factory, app_module):
    client.post("/employees", json=employee_factory(emp_code="EMP1401", department="Engineering"))

    list_params = {
        "emp_code": "EMP1401",
        "date_from": "2024-06-01",
        "date_to": "2024-06-30",
        "status": "PRESENT",
        "page": 2,
        "page_size": 7,
    }
    assert client.get("/attendance", params=list_params).status_code == 200
    list_operation = app_module.db.attendance_logs.last_find
    list_explain = client.get("/admin/explain/attendance_list", params=list_params)
    assert list_explain.status_code == 200
    explain_command = app_module.db.last_command
    explained_find = explain_command["explain"]
    assert explain_command["verbosity"] == "executionStats"
    assert explained_find["filter"] == list_operation["filter"]
    assert explained_find["projection"] == list_operation["projection"]
    assert explained_find["sort"] == dict(list_operation["cursor"].sort_spec)
    assert explained_find["skip"] == list_operation["cursor"]._skip
    assert explained_find["limit"] == list_operation["cursor"]._limit

    analytics = [
        (
            "/analytics/employees/EMP1401/monthly",
            {"month": "2024-06"},
            "/admin/explain/employee_monthly",
            {"emp_code": "EMP1401", "month": "2024-06"},
            "attendance_logs",
        ),
        (
            "/analytics/departments/summary",
            {"month": "2024-06", "department": "Engineering"},
            "/admin/explain/department_summary",
            {"month": "2024-06", "department": "Engineering"},
            "employees",
        ),
        (
            "/analytics/leaderboard/late",
            {"month": "2024-06", "limit": 4, "department": "Engineering"},
            "/admin/explain/late_leaderboard",
            {"month": "2024-06", "limit": 4, "department": "Engineering"},
            "attendance_logs",
        ),
        (
            "/analytics/departments/Engineering/trend",
            {"from": "2024-06-01", "to": "2024-06-07"},
            "/admin/explain/department_trend",
            {"department": "Engineering", "from": "2024-06-01", "to": "2024-06-07"},
            "employees",
        ),
    ]
    for route, params, explain_route, explain_params, collection_name in analytics:
        assert client.get(route, params=params).status_code == 200
        endpoint_pipeline = getattr(app_module.db, collection_name).last_aggregate_pipeline
        response = client.get(explain_route, params=explain_params)
        assert response.status_code == 200
        explain_command = app_module.db.last_command
        assert explain_command["verbosity"] == "executionStats"
        assert explain_command["explain"]["aggregate"] == collection_name
        assert explain_command["explain"]["pipeline"] == endpoint_pipeline


def test_explain_validation_and_index_declarations(client, app_module):
    assert client.get("/admin/explain/not_an_endpoint").status_code == 422
    assert client.get("/admin/explain/employee_monthly").status_code == 422
    assert client.get("/admin/explain/department_trend").status_code == 422
    assert client.get("/admin/explain/attendance_list", params={"page_size": 101}).status_code == 422

    app_module.ensure_indexes()
    first_declarations = list(app_module.db.attendance_logs.index_specs)
    app_module.ensure_indexes()
    second_declarations = app_module.db.attendance_logs.index_specs[len(first_declarations):]
    assert second_declarations == first_declarations
    unique_attendance_indexes = [
        kwargs
        for args, kwargs in first_declarations
        if kwargs.get("unique") and args[0] == [("emp_code", 1), ("date", 1)]
    ]
    assert len(unique_attendance_indexes) == 1
    assert unique_attendance_indexes[0]["name"] == "ux_attendance_logs_emp_code_date"


@pytest.mark.integration
def test_real_mongodb_integration_if_configured():
    mongo_uri = os.getenv("MONGO_URI")
    if not mongo_uri:
        pytest.skip("Integration test requires MONGO_URI to point at a test MongoDB instance")
    client = MongoClient(mongo_uri)
    db = client[os.getenv("MONGO_DB", "attendance_test")]
    db.attendance_logs.drop() if "attendance_logs" in db.list_collection_names() else None
    db.employees.drop() if "employees" in db.list_collection_names() else None
    import app.main as main
    importlib.reload(main)
    main.client = client
    main.db = db
    main.ensure_indexes()
    with TestClient(main.app) as tc:
        payload = {
            "emp_code": "EMP9001",
            "name": "Integration Tester",
            "email": "integration@example.com",
            "department": "QA",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2024-01-01",
        }
        assert tc.post("/employees", json=payload).status_code == 201
        punch_in_ms = int(datetime(2024, 6, 14, 9, 30, tzinfo=timezone.utc).timestamp() * 1000)
        assert tc.post("/attendance/punch-in", json={"emp_code": "EMP9001", "punched_at": punch_in_ms, "status": "PRESENT"}).status_code == 201
        summary = tc.get("/analytics/employees/EMP9001/monthly", params={"month": "2024-06"})
        assert summary.status_code == 200
    db.attendance_logs.drop() if "attendance_logs" in db.list_collection_names() else None
    db.employees.drop() if "employees" in db.list_collection_names() else None
