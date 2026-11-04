import importlib.metadata
import platform
import sys
from pathlib import Path

from fixtures import digest


def source_digest(directory):
    directory = Path(directory)
    return digest(
        {
            str(path.relative_to(directory)): digest(path.read_bytes())
            for path in sorted(directory.rglob("*"))
            if path.is_file()
            and (
                path.suffix in (".py", ".txt", ".in")
                or path == directory / "shards.json"
            )
            and "__pycache__" not in path.parts
        }
    )


def environment():
    import posthog

    entry = Path(posthog.__file__).resolve()
    if not entry.is_relative_to(Path(sys.prefix)):
        raise RuntimeError(f"Expected installed SDK inside {sys.prefix}, got {entry}")
    packages = {
        dist.metadata["Name"].lower().replace("_", "-"): dist.version
        for dist in importlib.metadata.distributions()
    }
    return {
        "runtime": "cpython",
        "runtime_version": platform.python_version(),
        "implementation": sys.version,
        "os": sys.platform,
        "arch": platform.machine(),
        "kernel": platform.release(),
        "cpu": platform.processor(),
        "entry": str(entry),
        "build_digest": source_digest(entry.parent),
        "packages": packages,
        "sdk_version": importlib.metadata.version("posthog"),
        "measurement_tools": {name: packages[name] for name in ("memray", "psutil")},
    }
