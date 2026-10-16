import logging
import signal
import subprocess
import time
from pathlib import Path

import yaml
from mkdocs.commands.build import build
from mkdocs.config import load_config

CONFIG_PATH = Path("/etc/wiki.yaml")
DEFAULT_POLL_INTERVAL_SECONDS = 10

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("wiki-runner")

stop_requested = False


def handle_shutdown(signum, _frame):
    global stop_requested
    logger.info("Received %s; will stop after the current operation",
                signal.Signals(signum).name)
    stop_requested = True


def git(checkout_dir: Path, *args: str) -> str:
    """Run Git without logging its arguments, which could contain credentials."""
    result = subprocess.run(
        ["git", "-C", str(checkout_dir), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        # Avoid logging raw stderr: it may contain a credential-bearing URL.
        raise RuntimeError(f"Git command failed with exit code {result.returncode}")
    return result.stdout.strip()


def sync_repository(repo: dict, repo_dir: Path) -> tuple[Path, str]:
    name = repo["name"]
    checkout_dir = repo_dir / name

    # Keep repository names as direct child directory names.
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ValueError(f"Invalid repository name: {name!r}")

    if not checkout_dir.exists():
        checkout_dir.mkdir(parents=True)
        git(checkout_dir, "init", "--quiet")
    elif not (checkout_dir / ".git").exists():
        if any(checkout_dir.iterdir()):
            raise RuntimeError(
                f"{checkout_dir} exists but is not an empty Git checkout"
            )
        git(checkout_dir, "init", "--quiet")

    remote = subprocess.run(
        ["git", "-C", str(checkout_dir), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=False,
    )

    if remote.returncode == 0:
        git(checkout_dir, "remote", "set-url", "origin", repo["git"])
    else:
        git(checkout_dir, "remote", "add", "origin", repo["git"])

    # Fetch only the configured ref and its tip commit.
    git(
        checkout_dir,
        "fetch",
        "--depth=1",
        "--force",
        "--no-tags",
        "origin",
        repo["revision"],
    )
    revision = git(checkout_dir, "rev-parse", "FETCH_HEAD^{commit}")
    return checkout_dir, revision


def build_repository(
    repo: dict,
    checkout_dir: Path,
    revision: str,
    base_url: str,
    site_dir: Path,
) -> None:
    name = repo["name"]
    docs_dir = (checkout_dir / repo["docs"]).resolve()
    checkout_root = checkout_dir.resolve()

    if not docs_dir.is_relative_to(checkout_root):
        raise ValueError(f"Docs path for {name} must be inside its checkout")

    mkdocs_config = docs_dir / "mkdocs.yml"
    if not mkdocs_config.is_file():
        raise FileNotFoundError(f"Missing MkDocs config: {mkdocs_config}")

    # Reset tracked changes and remove untracked/ignored checkout contents,
    # so the working tree matches the fetched commit.
    git(checkout_dir, "checkout", "--force", "--detach", revision)
    git(checkout_dir, "clean", "-fdx")

    config = load_config(
        config_file=str(mkdocs_config),
        site_url=f"{base_url.rstrip('/')}/{name}/",
        site_dir=str(site_dir / name),
    )
    try:
        build(config)
    except Exception as exc:
        raise RuntimeError(
            f"MkDocs build failed for {name} at revision {revision}: {exc}"
        ) from exc

    logger.info("Built %s at %s", name, revision)


def start_nginx() -> subprocess.Popen:
    subprocess.run(["nginx", "-t"], check=True)
    proc = subprocess.Popen(["nginx", "-g", "daemon off;"])

    # Detect an immediate startup failure.
    time.sleep(0.2)
    if proc.poll() is not None:
        raise RuntimeError(f"Nginx exited during startup (status {proc.returncode})")

    logger.info("Nginx started (pid=%s)", proc.pid)
    return proc


def stop_nginx(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return

    logger.info("Requesting graceful Nginx shutdown")
    proc.send_signal(signal.SIGQUIT)

    try:
        proc.wait(timeout=30)
        logger.info("Nginx stopped")
    except subprocess.TimeoutExpired:
        logger.warning("Nginx did not stop within 30s; sending SIGTERM")
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            logger.error("Nginx did not stop; sending SIGKILL")
            proc.kill()
            proc.wait()


def main() -> None:
    global stop_requested
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    with CONFIG_PATH.open(encoding="utf-8") as file:
        settings = yaml.safe_load(file)

    repo_dir = Path(settings["repo_dir"])
    site_dir = Path(settings["site_dir"])
    repo_dir.mkdir(parents=True, exist_ok=True)
    site_dir.mkdir(parents=True, exist_ok=True)

    base_url = settings["site_url"].rstrip("/")
    poll_interval = settings.get(
        "poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS
    )
    last_built: dict[str, str] = {}

    nginx_proc = start_nginx()
    logger.info("Wiki runner started; polling every %s seconds", poll_interval)

    try:
        while not stop_requested:
            for repo in settings["repositories"]:
                if stop_requested:
                    break

                name = repo["name"]
                try:
                    checkout_dir, revision = sync_repository(repo, repo_dir)

                    # Build on initial start, or when the fetched commit changes.
                    if last_built.get(name) == revision:
                        continue

                    build_repository(
                        repo=repo,
                        checkout_dir=checkout_dir,
                        revision=revision,
                        base_url=base_url,
                        site_dir=site_dir,
                    )
                    # Only mark successful builds, so failures are retried.
                    last_built[name] = revision

                except Exception:
                    logger.exception("Sync/build failed for %s", name)

            if nginx_proc.poll() is not None:
                raise RuntimeError(
                    f"Nginx exited unexpectedly (status {nginx_proc.returncode})"
                )

            if not stop_requested:
                time.sleep(poll_interval)
    finally:
        stop_nginx(nginx_proc)
        logger.info("Wiki runner stopped")


if __name__ == "__main__":
    main()