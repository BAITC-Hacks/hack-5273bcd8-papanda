"""One command builds the UI if needed and runs the single application server."""
import os
from pathlib import Path
import shutil
import subprocess


def main():
    from dotenv import load_dotenv
    import uvicorn
    root = Path(__file__).resolve().parent.parent
    load_dotenv(root / ".env")
    web = root / "web"
    if not (web / "dist/index.html").exists():
        npm = shutil.which("npm.cmd") or shutil.which("npm")
        if not npm:
            raise SystemExit("Node.js/npm required for first UI build. Install Node.js 20+.")
        subprocess.run([npm, "ci"], cwd=web, check=True)
        subprocess.run([npm, "run", "build"], cwd=web, check=True)
    uvicorn.run("app.main:app", host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8000")))


if __name__ == "__main__":
    main()
