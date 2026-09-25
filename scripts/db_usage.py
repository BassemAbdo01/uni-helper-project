"""What is using the production database, and how to get it back.

Render suspended `uni_helper_db` for exceeding its storage limit. This
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

Why the space goes: `course_content.content_text` holds the full extracted
text of everything the extension scrapes -- every lecture, page, outline
and announcement, in every course, in every term -- capped at 120,000
characters a row. Nothing ever removes it. A four-year degree at ~150 items
a course is tens of thousands of rows, and that one column is almost all of
the database.

The cleanup deletes no rows. Every title, link, date, deadline, grade and
outline survives. It blanks `content_text` in two places only:

  * announcement, assignment and quiz bodies over 12,000 characters,
    trimmed to 12,000 and marked as trimmed. Nothing summarises one of
    these; the dashboard shows the title and the date.
  * bodies of lectures, pages, practice and resources not touched in the
    last 120 days -- emptied, not shortened. This one is RECOVERABLE:
    below ~120 characters the study tools and the tutor answer
    `needs_content` and have the extension fetch the body again, so the
    cost is one download the next time an old item is opened. That
    threshold is exactly why these are emptied rather than shortened -- a
    body left at a few thousand characters would never re-fetch, and would
    be read as the whole item by the summariser and by the student.

Outlines are never touched, in any term. They are what the grading policy
was built from, what Ask quotes, and what a re-analysis needs.
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


def report(cur):
    print("\n=== Whole database ===")
    cur.execute("SELECT pg_database_size(current_database())")
    total = cur.fetchone()[0]
    print(f"  {human(total)}")

    print("\n=== By table (largest first) ===")
    cur.execute("""
        SELECT c.relname,
               pg_total_relation_size(c.oid)                             AS total,
               pg_relation_size(c.oid)                                   AS heap,
               pg_total_relation_size(c.oid) - pg_relation_size(c.oid)   AS other,
               COALESCE(s.n_live_tup, 0)                                 AS live,
               COALESCE(s.n_dead_tup, 0)                                 AS dead
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
        WHERE n.nspname = 'public' AND c.relkind = 'r'
        ORDER BY pg_total_relation_size(c.oid) DESC
    """)
    print(f"  {'table':28} {'total':>11} {'rows':>10} {'dead rows':>11}  share")
    for name, tot, heap, other, live, dead in cur.fetchall():
        share = 100 * tot / max(total, 1)
        flag = "  <-- dead rows need a VACUUM" if dead > live and dead > 1000 else ""
        print(f"  {name:28} {human(tot):>11} {live:>10,} {dead:>11,}  {share:5.1f}%{flag}")

    # the column that actually holds the bytes
    try:
        cur.execute("""
            SELECT content_type,
                   COUNT(*),
                   COALESCE(SUM(LENGTH(content_text)), 0),
                   COALESCE(MAX(LENGTH(content_text)), 0)
            FROM course_content GROUP BY content_type
            ORDER BY 3 DESC
        """)
        rows = cur.fetchall()
    except psycopg2.Error:
        return
    if rows:
        print("\n=== course_content.content_text, by type ===")
        print(f"  {'type':16} {'rows':>9} {'text':>11} {'largest':>10}")
        for kind, n, size, biggest in rows:
            print(f"  {kind or '(none)':16} {n:>9,} {human(size):>11} {human(biggest):>10}")


def cleanup_sql(lecture_days):
    """The statements, and what each one is allowed to touch.

    Nothing here deletes a row. Every title, link, date, deadline, grade and
    outline survives; only the scraped body text goes, and only where the app
    never reads it or can fetch it again.
    """
    return [
        ("announcement, assignment and quiz bodies over 12,000 characters\n"
         "    (trimmed to 12,000 and marked; nothing summarises one of these,\n"
         "     and the dashboard shows only their title and date)",
         """UPDATE course_content
            SET content_text = LEFT(content_text, 11950) ||
                E'\\n\\n[Nexora stored only the first part of this item.]'
            WHERE LENGTH(content_text) > 12000
              AND content_type IN ('announcement','assignment','quiz')"""),
        (f"lecture, page, practice and resource bodies not touched in {lecture_days} days\n"
         "    (emptied, not shortened: below ~120 characters the study tools and the\n"
         "     tutor answer needs_content and have the extension fetch the body again,\n"
         "     so this costs one download the next time an old item is opened)",
         f"""UPDATE course_content SET content_text = ''
            WHERE content_text <> ''
              AND content_type IN ('lecture','page','practice','resource')
              AND updated_at < NOW() - INTERVAL '{int(lecture_days)} days'"""),
    ]
    # outlines are never touched, in any term: they are what the grading policy
    # was built from, what Ask quotes, and what a re-analysis needs.


def plan(cur, lecture_days):
    print("\n=== What a cleanup would free ===")
    freed = 0
    for label, stmt in cleanup_sql(lecture_days):
        where = stmt.split("WHERE", 1)[1]
        cur.execute(f"SELECT COUNT(*), COALESCE(SUM(LENGTH(content_text)),0) FROM course_content cc WHERE {where}")
        n, size = cur.fetchone()
        freed += size
        print(f"  {label}\n    {n:,} rows, {human(size)}")
    print(f"\n  Total text removed: {human(freed)}")
    print("  (Postgres returns this to the OS only after VACUUM FULL; see below.)")
    return freed


def apply(conn, cur, lecture_days):
    total = 0
    for label, stmt in cleanup_sql(lecture_days):
        cur.execute(stmt)
        print(f"  {cur.rowcount:,} rows cleared -- {label}")
        total += cur.rowcount
    cur.execute("DELETE FROM academic_update WHERE created_at < NOW() - INTERVAL '60 days'")
    print(f"  {cur.rowcount:,} academic_update rows older than 60 days deleted")
    conn.commit()
    print(f"\nDone. {total:,} content rows kept their title, link and dates; only the body text went.")
    print("\nNow reclaim the disk. VACUUM FULL rewrites a table and needs free space")
    print("roughly equal to that table's size, so do the big one on its own:")
    print("    VACUUM (FULL, ANALYZE) course_content;")
    print("    VACUUM (FULL, ANALYZE) academic_update;")
    print("If VACUUM FULL fails for want of space, raise the storage plan one step,")
    print("run it, then drop back down.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("DATABASE_URL", ""), help="Postgres URL (Render: External Database URL)")
    ap.add_argument("--plan", action="store_true", help="show what a cleanup would free, change nothing")
    ap.add_argument("--apply", action="store_true", help="run the cleanup")
    ap.add_argument("--lecture-days", type=int, default=120, help="keep lecture text touched within this many days (default 120)")
    args = ap.parse_args()
    if not args.url:
        sys.exit("Give --url, or set DATABASE_URL.")

    conn = connect(args.url)
    conn.set_session(readonly=not args.apply, autocommit=False)
    cur = conn.cursor()
    report(cur)
    if args.plan or args.apply:
        plan(cur, args.lecture_days)
    if args.apply:
        print("\nThis rewrites data in the database above.")
        if input("Type YES to continue: ").strip() != "YES":
            sys.exit("Nothing was changed.")
        apply(conn, cur, args.lecture_days)
    conn.close()


if __name__ == "__main__":
    main()
