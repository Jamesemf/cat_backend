"""End-to-end HTTP test of the claim + review + notification flow.

Claims are no longer decided on submission — they land pending and a moderator
approves them — so this walks the whole path: submit, see that nothing was
granted, approve as an admin, then check ownership actually landed.

Costs a few vision API calls (one per submitted claim photo). Run against a
throwaway copy of the DB:

  DATABASE_URL=sqlite:///./test_claims.db uvicorn app.main:app --port 8001
  python scripts/test_claims_http.py http://localhost:8001
"""

import sqlite3
import sys
import uuid
from pathlib import Path

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8001"
DB = sys.argv[2] if len(sys.argv) > 2 else "test_claims.db"

results: list[tuple[str, bool, str]] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    results.append((label, cond, detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))


def register(client: httpx.Client, name: str) -> tuple[str, str]:
    email = f"{name}-{uuid.uuid4().hex[:8]}@test.local"
    r = client.post(
        f"{BASE}/auth/register",
        json={"email": email, "password": "test-password-1", "display_name": name},
    )
    r.raise_for_status()
    return r.json()["access_token"], email


def make_admin(email: str) -> None:
    """is_admin is set by hand in the database — there's no endpoint for it."""
    conn = sqlite3.connect(DB)
    conn.execute("UPDATE users SET is_admin = 1 WHERE email = ?", (email,))
    conn.commit()
    conn.close()


def main() -> int:
    # Pick a cat whose photo files still exist on disk (a single photo reused
    # three times is fine: the claim needs 3 uploads, not 3 distinct originals).
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        """
        SELECT c.id, c.name, s.photo_path FROM cats c
        JOIN sightings s ON s.cat_id = c.id
        WHERE c.primary_color IS NOT NULL
        ORDER BY c.sighting_count DESC, s.spotted_at DESC
        """
    ).fetchall()
    conn.close()
    by_cat: dict[int, tuple[str, list[str]]] = {}
    for cid, cname, p in rows:
        if Path(p).is_file():
            by_cat.setdefault(cid, (cname, []))[1].append(p)
    cat_id, cat_name, photo_paths = None, None, []
    for cid, (cname, paths) in by_cat.items():
        cat_id, cat_name, photo_paths = cid, cname, (paths * 3)[:3]
        break
    if cat_id is None:
        print("No cat with an existing photo file; cannot run.")
        return 1
    print(f"Testing against cat {cat_id} ({cat_name}) with photos {photo_paths}")

    with httpx.Client(timeout=120) as client:
        owner_token, _ = register(client, "Owner")
        rival_token, _ = register(client, "Rival")
        admin_token, admin_email = register(client, "Mod")
        make_admin(admin_email)
        owner_h = {"Authorization": f"Bearer {owner_token}"}
        rival_h = {"Authorization": f"Bearer {rival_token}"}
        admin_h = {"Authorization": f"Bearer {admin_token}"}

        # 1. Claim with the cat's own photos -> pending, not decided
        files = [
            ("photos", (Path(p).name, Path(p).read_bytes(), "image/jpeg"))
            for p in photo_paths
        ]
        data = {
            "likes_petting": "true",
            "accepts_treats": "false",
            "age_years": "4",
            "indoor_outdoor": "both",
            "real_name": "Sir Reginald Whiskers",
            "fun_fact": "Once followed the postman for a full mile.",
        }
        r = client.post(f"{BASE}/cats/{cat_id}/claim", headers=owner_h, files=files, data=data)
        body = r.json()
        check(
            "claim lands pending, not verified",
            r.status_code == 201 and body.get("status") == "pending",
            f"status={r.status_code} body={body}",
        )

        # 2. Nothing has been granted yet.
        detail = client.get(f"{BASE}/cats/{cat_id}").json()
        check(
            "no owner card while pending",
            detail.get("owner") is None,
            f"owner={detail.get('owner')}",
        )
        check(
            "cat keeps its nickname while pending",
            detail.get("name") != data["real_name"],
            f"name={detail.get('name')}",
        )
        notifs = client.get(f"{BASE}/notifications", headers=owner_h).json()
        check(
            "claimant told it's under review",
            any(n["type"] == "claim_pending" for n in notifs),
            f"types={[n['type'] for n in notifs]}",
        )

        # 3. The claim is in the moderator's queue, with both sides to compare.
        queue = client.get(f"{BASE}/moderation/claims", headers=admin_h).json()
        mine = [q for q in queue if q["cat_id"] == cat_id]
        check("claim appears in the review queue", len(mine) == 1, f"queue={len(queue)}")
        check(
            "queue carries evidence and the cat's own features",
            bool(mine) and bool(mine[0]["photos"]) and bool(mine[0]["cat_features"]),
        )
        check(
            "queue is admin-only",
            client.get(f"{BASE}/moderation/claims", headers=owner_h).status_code == 403,
        )

        # 4. A rival may also file — pending claims don't block each other, the
        #    moderator picks. (It's the approval that closes the cat off.)
        files2 = [
            ("photos", (Path(p).name, Path(p).read_bytes(), "image/jpeg"))
            for p in photo_paths
        ]
        r = client.post(f"{BASE}/cats/{cat_id}/claim", headers=rival_h, files=files2, data=data)
        check("rival may also file while undecided", r.status_code == 201, f"status={r.status_code}")

        # 5. Approve the owner's claim.
        claim_id = mine[0]["claim_id"]
        r = client.post(f"{BASE}/moderation/claims/{claim_id}/approve", headers=admin_h)
        check(
            "approve grants ownership",
            r.status_code == 200 and r.json()["status"] == "verified",
            f"status={r.status_code} body={r.text[:200]}",
        )

        # 6. Only now does the owner card appear and the real name stick.
        detail = client.get(f"{BASE}/cats/{cat_id}").json()
        owner = detail.get("owner")
        check(
            "GET /cats/{id} shows owner card after approval",
            bool(owner)
            and owner.get("fun_fact") == data["fun_fact"]
            and owner.get("real_name") == data["real_name"],
            f"owner={owner}",
        )
        check(
            "real name replaces cat nickname",
            detail.get("name") == data["real_name"],
            f"name={detail.get('name')}",
        )

        # 7. The rival's competing claim was closed out by that decision.
        rival_notifs = client.get(f"{BASE}/notifications", headers=rival_h).json()
        check(
            "rival's rival claim auto-rejected",
            any(n["type"] == "claim_rejected" for n in rival_notifs),
            f"types={[n['type'] for n in rival_notifs]}",
        )
        r = client.post(f"{BASE}/cats/{cat_id}/claim", headers=rival_h, files=[
            ("photos", (Path(p).name, Path(p).read_bytes(), "image/jpeg"))
            for p in photo_paths
        ], data=data)
        check("claiming an owned cat -> 409", r.status_code == 409, f"status={r.status_code}")

        # 8. Claim status endpoint
        r = client.get(f"{BASE}/cats/{cat_id}/claim", headers=rival_h)
        s = r.json()
        check(
            "claim status: owner public, rival can_claim false",
            s.get("owner") is not None and s.get("can_claim") is False,
        )

        # 9. Owner card update
        r = client.put(
            f"{BASE}/cats/{cat_id}/claim", headers=owner_h, json={"age_years": 5}
        )
        check("owner card update", r.status_code == 200 and r.json()["age_years"] == 5)
        r = client.put(
            f"{BASE}/cats/{cat_id}/claim", headers=rival_h, json={"age_years": 9}
        )
        check("rival card update -> 403", r.status_code == 403)

        # 10. Sighting linked to the claimed cat notifies the owner
        r = client.post(
            f"{BASE}/sightings",
            headers=rival_h,
            json={
                "cat_id": cat_id,
                "photo_path": photo_paths[0],
                "latitude": 51.5,
                "longitude": -0.12,
            },
        )
        check("rival logs sighting of claimed cat", r.status_code == 201, f"{r.status_code} {r.text[:200]}")

        r = client.get(f"{BASE}/notifications", headers=owner_h)
        notifs = r.json()
        sighting_notifs = [n for n in notifs if n["type"] == "sighting"]
        check(
            "owner has sighting notification",
            len(sighting_notifs) >= 1 and sighting_notifs[0]["cat_id"] == cat_id,
            f"count={len(notifs)}",
        )

        r = client.get(f"{BASE}/notifications/unread-count", headers=owner_h)
        unread_before = r.json()["count"]
        check("unread count > 0", unread_before >= 1, f"count={unread_before}")

        r = client.post(
            f"{BASE}/notifications/mark-read", headers=owner_h, json={"all": True}
        )
        r2 = client.get(f"{BASE}/notifications/unread-count", headers=owner_h)
        check("mark all read", r.json()["updated"] >= 1 and r2.json()["count"] == 0)

        # 11. Push token register/remove
        r = client.post(
            f"{BASE}/notifications/push-token",
            headers=owner_h,
            json={"token": "ExponentPushToken[test-123]", "platform": "ios"},
        )
        check("push token register", r.status_code == 204)
        r = client.request(
            "DELETE",
            f"{BASE}/notifications/push-token",
            headers=owner_h,
            json={"token": "ExponentPushToken[test-123]"},
        )
        check("push token remove", r.status_code == 204)

        # 12. Owner sighting of own cat -> no self-notification
        r = client.post(
            f"{BASE}/sightings",
            headers=owner_h,
            json={
                "cat_id": cat_id,
                "photo_path": photo_paths[0],
                "latitude": 51.5,
                "longitude": -0.12,
            },
        )
        r2 = client.get(f"{BASE}/notifications/unread-count", headers=owner_h)
        check("no self-notification for own sighting", r2.json()["count"] == 0)

        # 13. Revoke claim -> cat claimable again
        r = client.delete(f"{BASE}/cats/{cat_id}/claim", headers=owner_h)
        r2 = client.get(f"{BASE}/cats/{cat_id}/claim", headers=rival_h)
        check(
            "revoke frees the cat",
            r.status_code == 204 and r2.json()["owner"] is None and r2.json()["can_claim"] is True,
        )

        # 14. Registration takes the same route: no cat exists until approval.
        reg_token, _ = register(client, "Newcomer")
        reg_h = {"Authorization": f"Bearer {reg_token}"}
        before = {c["id"] for c in client.get(f"{BASE}/cats").json()}
        r = client.post(
            f"{BASE}/cats/register",
            headers=reg_h,
            files=[("photos", (Path(photo_paths[0]).name, Path(photo_paths[0]).read_bytes(), "image/jpeg"))],
            data={
                "name": "Pending Pete",
                "likes_petting": "true",
                "accepts_treats": "true",
                "age_years": "2",
                "indoor_outdoor": "indoor",
            },
        )
        reg_body = r.json()
        check(
            "register lands pending",
            r.status_code == 201 and reg_body.get("status") == "pending",
            f"status={r.status_code} body={reg_body}",
        )
        after = {c["id"] for c in client.get(f"{BASE}/cats").json()}
        check(
            "unreviewed registration is not in the public catalogue",
            after == before,
            f"new ids={after - before}",
        )

        r = client.post(
            f"{BASE}/moderation/claims/{reg_body['claim_id']}/approve", headers=admin_h
        )
        new_cat_id = r.json().get("cat_id") if r.status_code == 200 else None
        check(
            "approving a registration creates the cat",
            new_cat_id is not None,
            f"status={r.status_code} body={r.text[:200]}",
        )
        if new_cat_id:
            d = client.get(f"{BASE}/cats/{new_cat_id}").json()
            check(
                "created cat carries the name and owner",
                d.get("name") == "Pending Pete" and d.get("owner") is not None,
                f"name={d.get('name')} owner={d.get('owner')}",
            )

    failures = [r for r in results if not r[1]]
    print()
    print("ALL PASS" if not failures else f"{len(failures)} FAILURES")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
