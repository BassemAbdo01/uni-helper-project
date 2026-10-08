# Gunicorn auto-loads this file from the working directory unless a different
# config path is passed explicitly, so this takes effect with the existing
# `gunicorn app:app` start command -- no Render dashboard change needed.
#
# The course-outline analysis pipeline (/api/course/<code>/outline/analyze)
# makes several sequential Gemini API calls (fact extraction, policy
# compilation, adversarial audit), each with its own internal timeout of up
# to 90-120s and up to 3 retries. That can legitimately take several minutes
# for a large outline. Gunicorn's default worker timeout is 30s, so the
# worker was being killed mid-request while it was still waiting on a normal,
# in-progress Gemini response -- not an actual crash or real OOM.
import os

timeout = 300
graceful_timeout = 300

# The default sync worker serves exactly one request from start to finish, and
# an outline analysis measures 37-39 seconds of that spent waiting on Google
# rather than computing anything. With sync workers, N students analysing
# outlines at once occupies N workers, and every other request -- the dashboard,
# the landing page, logging in -- queues behind workers that are idle but
# unavailable. That is the shape of the first week of term, when everyone
# uploads their outlines at once.
#
# gthread workers hand each request to a thread, so a worker waiting on a
# network read can serve other requests meanwhile. The analysis is still slow
# for the student who asked for it; it just stops being slow for everybody else.
#
# This is a mitigation, not the real fix. The analysis should run outside the
# request entirely, with the browser polling for a result -- the front end
# already says "this can take a minute or two" and polls. Until then, this
# removes the site-wide stall.
worker_class = "gthread"

# Render's free instance is memory-constrained, so prefer threads over
# processes: threads share the interpreter, and this workload is I/O-bound
# waiting on an HTTP response, which releases the GIL.
workers = int(os.environ.get("WEB_CONCURRENCY", "2"))
threads = int(os.environ.get("GUNICORN_THREADS", "8"))

# Import the application ONCE in the master and fork the workers from it.
#
# Measured with `python -X importtime -c "import app"`: importing this module
# costs ~1.75s on a developer machine, and Render's Starter plan is half a
# vCPU, so it is several seconds there. Without preload every worker pays that
# separately at boot, and so does every replacement worker that max_requests
# recycles -- which is during ordinary browsing, not just after a deploy. With
# preload a recycled worker is a fork, which is nearly free.
#
# It also means the module-level `db.create_all()` / `ensure_schema_columns()`
# block runs once rather than once per worker.
preload_app = True


def post_fork(server, worker):
    """Give each worker its own database connections.

    The engine's pool is created when the module is imported, which with
    preload_app now happens in the MASTER -- so every forked worker inherits
    the same live sockets and they corrupt each other's traffic. Disposing
    after the fork makes each worker open its own; it does not reconnect
    eagerly, so this costs nothing at boot.
    """
    # `app.app_context()` is required: Flask-SQLAlchemy 3 resolves `db.engine`
    # through the current app, and a gunicorn hook runs outside any context, so
    # reaching for it raises "Working outside of application context" and the
    # worker dies at fork. Every worker died, the health check failed and the
    # deploy was rejected -- Render kept the previous version serving, which is
    # why this lives in its own commit.
    from app import app, db
    with app.app_context():
        db.engine.dispose()


# Recycle workers periodically so a slow leak in a long-lived thread cannot
# accumulate across days of uptime. The jitter stops every worker restarting
# at the same moment.
max_requests = 800
max_requests_jitter = 120

accesslog = "-"
errorlog = "-"
