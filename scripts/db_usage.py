"""What is using the production database, and how to get it back.

Render suspended `uni_helper_db` for exceeding its 1 GB storage limit. This
answers "what is taking so much storage" with numbers rather than a guess,
and can reclaim the space.

    # what is using the space
    python scripts/db_usage.py --url "postgresql://..."

    # what a cleanup WOULD free, without touching anything
    python scripts/db_usage.py --url "..." --plan

    # do it (asks before writing)
    python scripts/db_usage.py --url "..." --apply

The external connection string is on the Render dashboard under the
database, "Connections" -> "External Database URL". The URL can also come
from DATABASE_URL in the environment, but note that CLAUDE.md's rule still
stands: never point the local app at production. This script only reads
unless --apply is passed.

WHERE THE SPACE ACTUALLY WENT, measured 2026-09-25:

    academic_update   786.0 MB   96.1%   2,581,147 rows
    course_content     20.8 MB    2.5%      25,682 rows
    everything else     2.5 MB    0.4%

The first guess was `course_content.content_text` -- the scraped body of
every item, capped at 120,000 characters. It is 20.8 MB in total and the
largest single body in the database is a 20 KB outline. That theory was
wrong, and a cleanup built on it would have freed 2.5% of the problem.
Measure before believing a diagnosis; `pg_stat_user_tables` was also lying
about the row count (4,758 against a real 2,581,147) because ANALYZE had
never run.

`academic_update` holds 2,581,147 rows across only 33,927 distinct
(user, item, type) combinations -- every row written about seventy-six
times. `_import_content` deletes a course's `CourseContent` rows when the
payload carries `replace_courses`, then re-imports each item; the
`existing` lookup therefore always misses, the `content_hash` comparison
that exists to detect "unchanged" is unreachable, and every item writes a
fresh update row on every sync. Lectures and outlines write two.

The cleanup below deletes only the duplicates. Nothing a screen shows is
lost: `/api/updates` is called by nothing, the course workspace reads the
newest twelve for a course (twelve copies of one item today, twelve
different items afterwards), the Canvas announcement check only needs one
row per uid to exist, and the dashboard's "Recent updates" panel does not
read this table at all -- `_recent_updates` reads the content rows.

Deleting the duplicates without also fixing the insert buys about three
months: the table was growing by 150,000-220,000 rows a day.
"""
import argparse
import os
import sys

try:
    import psycopg2
except ImportError:
    sys.exit("psycopg2 is missing. Run: venv/Scripts/python.exe -m pip install psycopg2-binary")


def connect(url):
    # Render hands out postgres:// which psycopg2 accepts, and sslmode is
    # required from outside their network.
    if "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return psycopg2.connect(url)


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:,.1f} {unit}" if unit != "B" else f"{n:,.0f} B"
        n /= 1024.0


# Rows that are not the newest for their (user, item, type). The window
# function does one pass and a sort; the equivalent NOT EXISTS self-join is
# a nested loop over 2.5M rows and does not finish in reasonable time.
DUPLICATE_IDS = """
    SELECT id FROM (
        SELECT id, row_number() OVER (
            PARTITION BY user_id, related_uid, update_type
            ORDER BY created_at DESC, id DESC) AS rn
        FROM academic_update
    ) ranked WHERE rn > 1
"""


def report(cur):
    print("\n=== Whole database ===")
    cur.execute("SELECT pg_database_size(current_database())")
    total = cur.fetchone()[0]
    print(f"  {human(total)}")

    print("\n=== By table (largest first) ===")
    # n_live_tup comes from the stats collector and is a lie on a table that
    # has never been ANALYZEd -- it read 4,758 against a real 2,581,147 here,
    # which is how the wrong table got blamed. Count the big ones for real.
    cur.execute("""
        SELECT c.relname,
               pg_total_relation_size(c.oid)  AS total,
               pg_relation_size(c.oid)        AS heap,
               pg_indexes_size(c.oid)         AS idx
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'r'
        ORDER BY pg_total_relation_size(c.oid) DESC
    """)
    rows = cur.fetchall()
    print(f"  {'table':26} {'total':>10} {'heap':>10} {'indexes':>10} {'rows':>12}  share")
    for name, tot, heap, idx in rows:
        count = ""
        if tot > 1024 * 1024:                      # only count the ones that matter
            cur.execute('SELECT COUNT(*) FROM "%s"' % name)
            count = f"{cur.fetchone()[0]:,}"
        share = 100 * tot / max(total, 1)
        print(f"  {name:26} {human(tot):>10} {human(heap):>10} {human(idx):>10} {count:>12}  {share:5.1f}%")


def plan(cur):
    print("\n=== What the cleanup would remove ===")
    cur.execute("SELECT COUNT(*) FROM academic_update")
    before = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM (%s) d" % DUPLICATE_IDS)
    dupes = cur.fetchone()[0]
    cur.execute("""SELECT COUNT(*) FROM (
                     SELECT 1 FROM academic_update
                     GROUP BY user_id, related_uid, update_type) x""")
    keep = cur.fetchone()[0]
    cur.execute("SELECT pg_total_relation_size('academic_update')")
    size = cur.fetchone()[0]

    print(f"  academic_update rows now          {before:>12,}")
    print(f"  duplicates to delete              {dupes:>12,}   ({100 * dupes / max(before, 1):.1f}%)")
    print(f"  rows kept (newest per item+type)  {keep:>12,}")
    print(f"\n  The table occupies {human(size)}. After the delete and a VACUUM FULL")
    print(f"  it should be roughly {human(size * keep / max(before, 1))}.")
    print("\n  Nothing else is touched. Every other table, and every column on every")
    print("  row that is kept, is left exactly as it is.")
    return dupes


def apply(conn, cur, batch):
    """Delete in batches, committing each one.

    One transaction holding 2.5M deletes builds a single enormous WAL record
    and keeps every dead tuple locked until it commits -- on a database that
    just ran out of disk, that is the one shape to avoid. Batches keep each
    transaction small and let autovacuum begin reclaiming as we go.
    """
    total = 0
    while True:
        cur.execute("DELETE FROM academic_update WHERE id IN (SELECT id FROM (%s) d LIMIT %d)"
                    % (DUPLICATE_IDS, int(batch)))
        n = cur.rowcount
        conn.commit()
        if n == 0:
            break
        total += n
        print(f"  deleted {n:,} (running total {total:,})", flush=True)
    cur.execute("SELECT COUNT(*) FROM academic_update")
    print(f"\nDone. {total:,} duplicate rows deleted; {cur.fetchone()[0]:,} rows remain.")
    print("\nNow reclaim the disk. VACUUM FULL rewrites the table into a new file, so")
    print("it needs free space about the size of what SURVIVES, not of the original")
    print("table -- which after this delete is small:")
    print("    VACUUM (FULL, ANALYZE) academic_update;")
    print("Then run this script again for the 'after' number.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("DATABASE_URL", ""), help="Postgres URL (Render: External Database URL)")
    ap.add_argument("--plan", action="store_true", help="show what a cleanup would remove, change nothing")
    ap.add_argument("--apply", action="store_true", help="run the cleanup")
    ap.add_argument("--batch", type=int, default=100000, help="rows per delete batch (default 100000)")
    args = ap.parse_args()
    if not args.url:
        sys.exit("Give --url, or set DATABASE_URL.")

    conn = connect(args.url)
    conn.set_session(readonly=not args.apply, autocommit=False)
    cur = conn.cursor()
    report(cur)
    if args.plan or args.apply:
        plan(cur)
    if args.apply:
        print("\nThis permanently deletes the rows counted above.")
        if input("Type YES to continue: ").strip() != "YES":
            sys.exit("Nothing was changed.")
        apply(conn, cur, args.batch)
    conn.close()


if __name__ == "__main__":
    main()
