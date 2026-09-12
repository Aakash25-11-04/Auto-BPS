"""Baseline seeding (always) and optional demo fixtures (--demo-data only).

Baseline seeding creates the User accounts needed to exercise RBAC in a live
demo — these are system accounts, not maintenance records, so they are not
gated by the data policy. Demo maintenance tasks are a completely separate,
clearly-labelled path: every row seed_demo_tasks() inserts carries
source="demo" and is never created unless the operator explicitly opts in.
"""
import datetime as dt

from sqlalchemy.orm import Session

import auth
import models
from audit import log

# Demo passwords, printed to console on every seed run (see
# seed_baseline_users below) so the app is demoable out of the box. These are
# NOT stored in plaintext anywhere — only their bcrypt hash goes into the
# database. Change them (via /api/auth/change-password) before any real
# deployment; they are intentionally simple and public in this source file.
DEMO_PASSWORDS = {
    "eng.desk": "Eng@Railways1",
    "td.desk": "Td@Railways1",
    "snt.desk": "Snt@Railways1",
    "coa.controller": "Coa@Railways1",
    "admin": "Admin@Railways1",
}

BASELINE_USERS = [
    {"user_id": "eng.desk", "name": "Engineering Desk Operator", "role": "ENG", "department": "ENG"},
    {"user_id": "td.desk", "name": "Traction Distribution Desk Operator", "role": "TD", "department": "TD"},
    {"user_id": "snt.desk", "name": "S&T Desk Operator", "role": "SNT", "department": "SNT"},
    {"user_id": "coa.controller", "name": "Control Office Controller", "role": "COA", "department": "COA"},
    {"user_id": "admin", "name": "System Administrator", "role": "ADMIN", "department": ""},
]

DEMO_TASKS = [
    {"department": "ENG", "asset_id": "ENG-TRK-1042", "defect_type": "rail_fracture", "severity": 5,
     "overdue_days": 45, "required_duration_hours": 6, "safety_critical": True, "interlocking_critical": False},
    {"department": "ENG", "asset_id": "ENG-TRK-1077", "defect_type": "ballast_degradation", "severity": 2,
     "overdue_days": 5, "required_duration_hours": 3, "safety_critical": False, "interlocking_critical": False},
    {"department": "TD", "asset_id": "TD-OHE-0087", "defect_type": "ohe_insulator_fault", "severity": 3,
     "overdue_days": 10, "required_duration_hours": 4, "safety_critical": False, "interlocking_critical": False},
    {"department": "TD", "asset_id": "TD-OHE-0102", "defect_type": "feeder_cable_wear", "severity": 4,
     "overdue_days": 20, "required_duration_hours": 5, "safety_critical": True, "interlocking_critical": False},
    {"department": "SNT", "asset_id": "SNT-SIG-0231", "defect_type": "signal_relay_fault", "severity": 4,
     "overdue_days": 12, "required_duration_hours": 3, "safety_critical": False, "interlocking_critical": True},
    {"department": "SNT", "asset_id": "SNT-SIG-0255", "defect_type": "point_machine_test", "severity": 2,
     "overdue_days": 2, "required_duration_hours": 2, "safety_critical": False, "interlocking_critical": False},
]

DEMO_ASSET_CRITICALITY = {
    "ENG-TRK-1042": 5, "ENG-TRK-1077": 2, "TD-OHE-0087": 3,
    "TD-OHE-0102": 4, "SNT-SIG-0231": 4, "SNT-SIG-0255": 2,
}


def seed_baseline_users(db: Session) -> int:
    created = 0
    for u in BASELINE_USERS:
        existing = db.query(models.User).filter_by(user_id=u["user_id"]).first()
        if not existing:
            db.add(models.User(**u, active=True, password_hash=auth.hash_password(DEMO_PASSWORDS[u["user_id"]])))
            created += 1
        elif not existing.password_hash:
            # backstop for a DB created before password_hash existed
            existing.password_hash = auth.hash_password(DEMO_PASSWORDS[u["user_id"]])
    if created:
        db.commit()
    else:
        db.commit()

    print("\n" + "=" * 60)
    print("ABPS demo accounts (change these before any real deployment):")
    for u in BASELINE_USERS:
        print(f"  {u['user_id']:<16} role={u['role']:<6} password={DEMO_PASSWORDS[u['user_id']]}")
    print("=" * 60 + "\n")

    return created


def seed_demo_tasks(db: Session, corridor_id: str, user_id: str = "system") -> dict:
    """Inserts clearly-labelled (source='demo') fixture tasks against a given
    corridor. Only ever called when the operator opts in with --demo-data."""
    import priority_engine

    for asset_id, crit in DEMO_ASSET_CRITICALITY.items():
        if not db.query(models.AssetCriticality).filter_by(asset_id=asset_id).first():
            db.add(models.AssetCriticality(asset_id=asset_id, criticality=crit))
    db.commit()

    created = []
    for i, t in enumerate(DEMO_TASKS):
        task_id = f"task-demo-{t['department']}-{i:03d}"
        if db.query(models.MaintenanceTask).filter_by(task_id=task_id).first():
            continue
        task = models.MaintenanceTask(
            task_id=task_id,
            corridor_id=corridor_id,
            status="submitted",
            source="demo",
            source_ref="seed_demo.DEMO_TASKS",
            created_by=user_id,
            mutually_exclusive_with="",
            **t,
        )
        score, reason = priority_engine.score_task(db, task)
        task.priority_score = score
        task.priority_reason = reason
        db.add(task)
        created.append(task_id)
    db.commit()
    log(db, "demo_data_seeded", user_id, {"corridor_id": corridor_id, "task_ids": created})
    return {"corridor_id": corridor_id, "created_task_ids": created}
