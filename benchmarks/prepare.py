"""Build an isolated, non-editable SDK installation with fixed benchmark tools."""

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

PACKAGES = "import importlib.metadata as m, json; print(json.dumps({d.metadata['Name'].lower().replace('_','-'): d.version for d in m.distributions()}))"


def prepare(target, python_version, uv):
    target = target.resolve()
    environment = target / ".venv-benchmark"
    env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(environment)}
    subprocess.run(
        [
            uv,
            "sync",
            "--project",
            str(target),
            "--frozen",
            "--no-dev",
            "--no-editable",
            "--compile-bytecode",
            "--reinstall-package",
            "posthog",
            "--python",
            python_version,
        ],
        env=env,
        check=True,
    )
    python = environment / "bin/python"
    # Inspect from outside the source tree; the worker must import the built wheel.
    with tempfile.TemporaryDirectory(prefix="posthog-benchmark-prepare-") as cwd:
        before = json.loads(
            subprocess.check_output([python, "-I", "-c", PACKAGES], cwd=cwd)
        )
        subprocess.run(
            [
                uv,
                "pip",
                "install",
                "--python",
                str(python),
                "--no-deps",
                "--require-hashes",
                "--compile-bytecode",
                "-r",
                str(Path(__file__).with_name("requirements.txt")),
            ],
            check=True,
            cwd=cwd,
        )
        after = json.loads(
            subprocess.check_output([python, "-I", "-c", PACKAGES], cwd=cwd)
        )
        changed = {
            name: (version, after.get(name))
            for name, version in before.items()
            if after.get(name) != version
        }
        if changed:
            raise RuntimeError(
                f"Benchmark tooling changed locked SDK dependencies: {changed}"
            )
        subprocess.run(
            [uv, "pip", "check", "--python", str(python)], check=True, cwd=cwd
        )
    return python


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path)
    parser.add_argument("--python", default="3.12.12")
    parser.add_argument("--uv", default="uv")
    args = parser.parse_args()
    print(prepare(args.target, args.python, args.uv))
