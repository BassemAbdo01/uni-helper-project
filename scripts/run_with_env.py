"""Start app.py with .env loaded -- app.py deliberately does not read .env
itself, and the preview launcher cannot source it. Local use only."""
import os, runpy, sys
here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(here, ".env"), encoding="utf-8") as fh:
    for line in fh:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
sys.argv = ["app.py"]
os.chdir(here)
runpy.run_path(os.path.join(here, "app.py"), run_name="__main__")
